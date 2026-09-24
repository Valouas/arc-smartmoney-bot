from contextlib import closing
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from external_http import ExternalError
from external_wallet_report import combined_report
from external_wallet_store import ExternalStore
from nansen_audit import collect, comparison, import_result, select_sample
from nansen_client import DETAIL, SUMMARY, NansenClient, NansenHTTP, detail_page, summary
from test_external_pipeline import local
from test_external_providers import Response, W, W2, NOW, cfg

DATE = {'from':'2026-07-15T00:00:00Z','to':'2026-09-20T22:00:00Z'}
BODY = {'chain':'arc','address':W,'date':DATE}


def overview(**changes):
    return {'pagination':{'page':1,'per_page':100,'is_last_page':True},
            'top5_tokens':[{'chain':'arc','token_address':W2}],
            'realized_pnl_usd':1000,'realized_pnl_percent':2,'win_rate':.25,
            'traded_token_count':2,'traded_times':10,**changes}


def position(token=W2, **changes):
    return {'token_address':token,'token_symbol':'USDC','pnl_usd_realised':1000,
            'pnl_usd_unrealised':-900,'token_price':1,'holding_amount':100,
            'holding_usd':100,'nof_buys':'2','nof_sells':'1',**changes}


def page(rows=None, index=1, last=True):
    return {'pagination':{'page':index,'per_page':1000,'is_last_page':last},
            'data':[position()] if rows is None else rows}


def state():
    return {'date':DATE,'max_requests':60,'attempts':0,'credits_used_observed':0,
            'credits_remaining':70,'cache':{},'results':{},'errors':{},'selected':[
                {'wallet':W,'zerion_metrics':{'realized_gain':1100,'unrealized_gain':-800,'total_gain':300}}]}


def response(data=None, **headers):
    return Response(overview() if data is None else data,headers={
        'X-Nansen-Credits-Cost':'1','X-Nansen-Credits-Used':'1',
        'X-Nansen-Credits-Remaining':'69',**headers})


def client():
    http=Mock();http.post.side_effect=[overview(),page(),page([position(W)])]
    return NansenClient(http)


def test_closed_and_open_losses_preserved_ratios_and_no_authority():
    c=client(); result=c.pnl(W,DATE)
    assert result['metrics']=={'realized_gain':1000,'unrealized_gain':-900,'total_gain':100}
    assert result['summary']['realized_pnl_percent']==2
    assert result['positions']['open'][0]['nof_buys']==2
    # A token calling itself USDC is still identified by address, never valued at $1 by ticker.
    assert result['positions']['realized'][0]['token_address']==W2
    assert not result['data_complete'] and not result['live_signal_authority']
    assert c.http.post.call_args_list[1].args[1]['filters']=={'show_realized':True}
    assert c.http.post.call_args_list[2].args[1]['filters']=={'show_realized':False}


def test_live_arc_fixture_preserves_unpriced_open_loss_and_uncertainty():
    fixture=json.loads((Path(__file__).parent/'fixtures/nansen_arc_pnl.json').read_text(encoding='utf-8'))
    c=client();c.http.post.side_effect=[fixture[k] for k in ('summary','realized','open')]
    result=c.pnl(W,DATE)
    assert result['metrics']['realized_gain']==pytest.approx(17388.081262887827)
    assert result['metrics']['unrealized_gain']==pytest.approx(-109.96023600318313)
    assert result['metrics']['total_gain']==pytest.approx(17278.121026884644)
    assert len(result['positions']['realized'])==15 and len(result['positions']['open'])==1
    assert 'summary_detail_token_count_mismatch' in result['flags']
    assert 'open_position_without_positive_price' in result['flags']
    assert not result['data_complete']


def test_offline_report_does_not_need_key_or_make_requests(tmp_path,monkeypatch,capsys):
    from nansen_audit import main
    s=state();s['status']='DEFERRED'
    path=tmp_path/'trial.json';path.write_text(json.dumps(s))
    factory=Mock(side_effect=AssertionError('No network or database allowed'))
    monkeypatch.setattr('nansen_audit.NansenHTTP',factory)
    monkeypatch.setattr('nansen_audit.ExternalStore',factory)
    assert main(['--report','--state',str(path)])==0
    assert 'INCOMPLETE' in capsys.readouterr().out
    factory.assert_not_called()


def test_provider_sign_disagreement_explicit_never_changes_authority():
    s=state();s['status']='COMPLETE';s['results'][W]=client().pnl(W,DATE)
    s['selected'][0]['zerion_metrics']['total_gain']=-300
    row=comparison(s)['wallets'][0]
    assert 'opposite_pnl_signs' in row['comparison_flags']
    assert not row['live_signal_authority']


def test_missing_open_pnl_never_becomes_zero():
    c=client();c.http.post.side_effect=[overview(),page(),page([position(W,pnl_usd_unrealised=None)])]
    result=c.pnl(W,DATE)
    assert result['metrics']['unrealized_gain'] is result['metrics']['total_gain'] is None
    assert 'missing_pnl_fields' in result['flags']


def test_zero_priced_open_position_and_summary_mismatch_are_visible():
    c=client();c.http.post.side_effect=[overview(traded_token_count=3,realized_pnl_usd=1100),page(),
                                     page([position(W,token_price=0,holding_usd=0)])]
    result=c.pnl(W,DATE)
    assert {'summary_detail_mismatch','summary_detail_token_count_mismatch',
            'open_position_without_positive_price'}<=set(result['flags'])


def test_valid_empty_views_have_zero_pnl():
    c=client();c.http.post.side_effect=[overview(traded_token_count=0,realized_pnl_usd=0),page([]),page([])]
    assert c.pnl(W,DATE)['metrics']['total_gain']==0


@pytest.mark.parametrize('bad',[{}, {'page':1,'per_page':1000},
    {'page':2,'per_page':1000,'is_last_page':True},
    {'page':1,'per_page':1000,'is_last_page':'true'}])
def test_malformed_pagination_fails_closed(bad):
    value=page();value['pagination']=bad
    with pytest.raises(ExternalError):detail_page(value,1)


@pytest.mark.parametrize('change',[{'token_address':'bad'}, {'chain':'ethereum'},
    {'nof_buys':True}, {'pnl_usd_realised':float('nan')}])
def test_malformed_position_rejected(change):
    with pytest.raises(ExternalError):detail_page(page([position(**change)]),1)


def test_pagination_duplicate_cannot_double_count_profit():
    c=client();c.http.post.side_effect=[overview(),page(last=False),page(index=2)]
    with pytest.raises(ExternalError,match='duplicate_nansen_token'):c.pnl(W,DATE)


def test_partial_page_cannot_become_complete():
    c=client();c.http.post.side_effect=[overview(),page(last=False),ExternalError('nansen_timeout')]
    with pytest.raises(ExternalError,match='timeout'):c.pnl(W,DATE)


@pytest.mark.parametrize('failure',[429,502,'timeout','connection'])
def test_transport_retry_then_success_and_cache(failure):
    session=Mock();s=state();persist=Mock();sleep=Mock()
    first=requests.Timeout('secret') if failure=='timeout' else requests.ConnectionError('secret') if failure=='connection' else Response(status=failure,headers={'Retry-After':'2'})
    session.post.side_effect=[first,response()]
    http=NansenHTTP('secret',s,persist,session,sleep=sleep,clock=lambda:100)
    assert http.post(SUMMARY,BODY)==overview()
    assert http.post(SUMMARY,BODY)==overview()
    assert session.post.call_count==s['attempts']==2 and s['credits_used_observed']==1
    assert session.post.call_args.kwargs['allow_redirects'] is False
    assert session.post.call_args.kwargs['headers']['apikey']=='secret'
    assert persist.call_count>=3


def test_three_timeouts_fail_closed_and_retain_budget():
    s=state();session=Mock();session.post.side_effect=requests.Timeout('secret')
    http=NansenHTTP('secret',s,Mock(),session,sleep=Mock(),clock=lambda:100)
    with pytest.raises(ExternalError,match='nansen_timeout'):http.post(SUMMARY,BODY)
    assert s['attempts']==3 and not s['cache'] and s['retry_at']==160
    with pytest.raises(ExternalError,match='deferred'):http.post(SUMMARY,BODY)
    assert session.post.call_count==3


@pytest.mark.parametrize('status',[400,401,403,402])
def test_no_retry_or_payment_for_permanent_errors(status):
    s=state();session=Mock();session.post.return_value=Response(status=status)
    http=NansenHTTP('secret',s,Mock(),session)
    with pytest.raises(ExternalError,match=str(status)):http.post(SUMMARY,BODY)
    assert session.post.call_count==1


@pytest.mark.parametrize('change,category',[({'attempts':60},'request_budget'),
    ({'credits_remaining':5},'credit_reserve'),({'halt':'nansen_billing_unverified'},'billing_unverified')])
def test_budget_reserve_and_halt_prevent_network(change,category):
    s=state();s.update(change);session=Mock()
    with pytest.raises(ExternalError,match=category):NansenHTTP('key',s,Mock(),session).post(SUMMARY,BODY)
    session.post.assert_not_called()


def test_changed_price_halts_and_no_payment_routes():
    s=state();session=Mock();session.post.return_value=response(**{'X-Nansen-Credits-Cost':'2'})
    http=NansenHTTP('key',s,Mock(),session)
    with pytest.raises(ExternalError,match='billing_unverified'):http.post(SUMMARY,BODY)
    assert s['halt']=='nansen_billing_unverified'
    with pytest.raises(ExternalError,match='invalid_nansen_route'):http.post('purchase',BODY)
    assert session.post.call_count==1


def test_long_quota_reset_is_persisted_not_busy_retried():
    s=state();session=Mock();session.post.return_value=Response(status=429,headers={'Retry-After':'3600'})
    http=NansenHTTP('key',s,Mock(),session,sleep=Mock(),clock=lambda:100)
    with pytest.raises(ExternalError):http.post(SUMMARY,BODY)
    assert s['retry_at']==3700 and session.post.call_count==1
    http.sleep.assert_not_called()


def test_import_resume_additive_no_authority_or_history_changes(local,cfg):
    with local.db:
        local.db.execute("INSERT INTO discovery_candidates(wallet,first_discovered_at,last_discovered_at,status,source_winners) VALUES(?,?,?,'WATCH',1)",(W,NOW,NOW))
        local.db.execute("INSERT INTO scanner_state VALUES('next_block','100')")
    before=tuple(local.db.execute('SELECT * FROM discovery_candidates').fetchone())
    s=state();c=client()
    with closing(ExternalStore(local.path)) as store:
        collect(s,Mock(),c,store,set())
        collect(s,Mock(),c,store,set())
        assert c.http.post.call_count==3
        assert store.db.execute("SELECT COUNT(*) FROM external_wallet_metrics WHERE provider='nansen'").fetchone()[0]==1
        assert tuple(store.db.execute('SELECT * FROM discovery_candidates').fetchone())==before
        assert store.db.execute("SELECT value FROM scanner_state WHERE key='next_block'").fetchone()[0]==100
        for table in ('promoted_wallets','events','research_trades','wallet_profiles'):
            assert store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    report=combined_report(local.path,cfg)
    assert report[0]['nansen_metrics']['total_gain']==100
    assert not report[0]['live_signal_authority'] and not report[0]['nansen_data_complete']
    assert comparison(s)['wallets'][0]['differences_nansen_minus_zerion']['total_gain']==-200


def test_failed_open_view_never_imports_partial_pnl(local):
    s=state();c=client();c.http.post.side_effect=[overview(),page(),ExternalError('nansen_timeout')]
    with closing(ExternalStore(local.path)) as store:
        collect(s,Mock(),c,store,set())
        assert store.db.execute('SELECT COUNT(*) FROM external_wallet_metrics').fetchone()[0]==0
    assert s['status']=='DEFERRED' and not s['results']


def test_infrastructure_excluded_before_requests(local):
    s=state();c=Mock()
    with closing(ExternalStore(local.path)) as store:collect(s,Mock(),c,store,{W})
    c.pnl.assert_not_called()
    assert not s['results'] and s['errors'][W]=='infrastructure_excluded'


def test_trial_includes_losses_rare_wallets_and_does_not_filter_contracts():
    rows=[{'wallet':'0x'+format(i+1,'040x'),'infrastructure_excluded':False,
           'zerion_metrics':{'total_gain':i*100,'unrealized_gain':-i},
           'dune_activity':{'buy_count':30-i},'is_contract':True} for i in range(30)]
    rows[0]['zerion_metrics']['total_gain']=-1000
    rows[5]['infrastructure_excluded']=True
    chosen=select_sample(rows)
    assert len(chosen)==20 and len({r['wallet'] for r in chosen})==20
    assert rows[0] in chosen and rows[-1] in chosen and rows[5] not in chosen
