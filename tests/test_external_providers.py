import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from config import Settings
from dune_discovery import DuneDiscovery, normalize_row
from external_http import ExternalHTTP, ExternalError, stamp
from zerion_client import ZerionClient, normalize_pnl, CORE

W='0x'+'1'*40
W2='0x'+'2'*40
NOW=1700000000


@pytest.fixture
def cfg(tmp_path):
    return replace(Settings(),trusted_dex_registry=str(tmp_path/'registry.json'),
        external_discovery_enabled=True,dune_enabled=True,dune_api_key='DUNE_SECRET',dune_query_id='12',
        zerion_enabled=True,zerion_api_key='ZERION_SECRET')


def row(wallet=W,**changes):
    return {'wallet':wallet,'chain_id':5042,'first_seen_at':NOW-100,'last_seen_at':NOW-10,
            'buy_count':4,'sell_count':3,'trade_count':7,'distinct_tokens':3,'volume_usd':1200,**changes}


def dune_page(rows=None,total=1,nxt=None,**changes):
    rows=[row()] if rows is None else rows
    return {'query_id':12,'execution_id':'run_one','is_execution_finished':True,
            'state':'QUERY_STATE_COMPLETED','execution_ended_at':NOW-1,
            'result':{'rows':rows,'metadata':{'row_count':len(rows),'total_row_count':total}},
            'next_offset':nxt,**changes}


def chains(external='0x13b2',slug='arc-mainnet-resolved'):
    return {'links':{'self':'https://api.zerion.io/v1/chains/'},
            'data':[{'type':'chains','id':slug,'attributes':{'external_id':external}}]}


def pnl(wallet=W,**changes):
    return {'links':{'self':'https://api.zerion.io/v1/wallets/'+wallet+'/pnl'},
            'data':{'type':'pnl','id':wallet,'attributes':{
                'total_gain':120,'realized_gain':100,'unrealized_gain':20,
                'relative_total_gain_percentage':12,'total_fee':5,'net_invested':1000,**changes}}}


class Response:
    def __init__(self,data=None,status=200,headers=None,body=None):
        self.status_code=status;self.headers={'Content-Type':'application/json',**(headers or {})}
        self.body=json.dumps(data).encode() if body is None else body
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,size):yield self.body


@pytest.mark.parametrize('provider',['dune','zerion'])
@pytest.mark.parametrize('failure',[429,500,502,503,504,'timeout','connection'])
def test_bounded_retry_then_success_and_safe_auth(provider,failure):
    session=Mock();sleep=Mock()
    first=(requests.Timeout('SECRET') if failure=='timeout' else requests.ConnectionError('SECRET')
           if failure=='connection' else Response(status=failure,headers={'Retry-After':'2'}))
    session.get.side_effect=[first,Response({'ok':True})]
    http=ExternalHTTP(provider,'SECRET',session,sleep=sleep)
    assert http.get('chains/')=={'ok':True}
    assert session.get.call_count==2 and sleep.call_count==1
    kwargs=session.get.call_args.kwargs
    assert kwargs['timeout']==(5,20) and kwargs['allow_redirects'] is False
    if provider=='zerion':assert kwargs['auth']==('SECRET','')
    else:assert kwargs['headers']['X-Dune-API-Key']=='SECRET'
    if type(failure) is int:assert sleep.call_args.args[0]>=2


def test_long_retry_after_deferred_instead_of_sleeping_or_retrying_early():
    session=Mock();session.get.return_value=Response(status=429,headers={'Retry-After':'3600'})
    sleep=Mock();http=ExternalHTTP('zerion','SECRET',session,sleep=sleep)
    with pytest.raises(ExternalError) as result:http.get('chains/')
    assert result.value.retry_after==3600 and session.get.call_count==1
    sleep.assert_not_called()


def test_zerion_pacing_covers_chain_lookup_wallets_and_retries():
    now=[0.0]; times=[]
    def sleep(seconds): now[0]+=seconds
    responses=iter([Response({'ok':1}),Response(status=429,headers={'Retry-After':'2'}),Response({'ok':2})])
    def get(*args,**kwargs):
        times.append(now[0]);return next(responses)
    session=Mock();session.get.side_effect=get
    http=ExternalHTTP('zerion','SECRET',session,sleep=sleep,min_interval=1,monotonic=lambda:now[0])
    http.get('chains/');http.get('wallets/'+W+'/pnl')
    assert len(times)==3 and all(b-a>=1 for a,b in zip(times,times[1:]))
    assert times[2]-times[1]>=2


@pytest.mark.parametrize('rate',[0,-1,float('nan'),float('inf'),11])
def test_invalid_zerion_rate_rejected(cfg,rate):
    with pytest.raises(ValueError):replace(cfg,zerion_requests_per_second=rate)


def test_external_poll_cannot_be_tight_loop(cfg):
    with pytest.raises(ValueError):replace(cfg,external_poll_seconds=0)


@pytest.mark.parametrize('status',[400,401,403,404,302])
def test_nonretryable_status_and_no_secret_leak(status):
    session=Mock();session.get.return_value=Response(status=status,body=b'SECRET')
    with pytest.raises(ExternalError) as result:ExternalHTTP('dune','SECRET',session).get('query/12/results')
    assert session.get.call_count==1 and 'SECRET' not in str(result.value)


def test_three_failures_never_become_empty_valid_data():
    session=Mock();session.get.return_value=Response(status=503)
    with pytest.raises(ExternalError,match='http_503'):
        ExternalHTTP('zerion','SECRET',session,sleep=Mock()).get('chains/')
    assert session.get.call_count==3


@pytest.mark.parametrize('body',[b'{bad',b'[]',b'{"x":NaN}',b'{"x":1e999}',b'\xff'])
def test_malformed_json_fails_closed_without_retry(body):
    session=Mock();session.get.return_value=Response(body=body)
    with pytest.raises(ExternalError):ExternalHTTP('dune','SECRET',session).get('query/12/results')
    assert session.get.call_count==1


def test_response_size_bound():
    session=Mock();session.get.return_value=Response(body=b'a'*100)
    http=ExternalHTTP('dune','SECRET',session);http.max_bytes=50
    with pytest.raises(ExternalError,match='payload_limit'):http.get('query/12/results')


def test_dune_valid_and_pinned_execution_pagination(cfg):
    http=Mock();http.get.side_effect=[dune_page(total=2,nxt=1),dune_page([row(W2)],total=2)]
    result=DuneDiscovery(cfg,http).fetch(NOW)
    assert len(result['rows'])==2 and result['rows'][0]['realized_pnl'] is None
    assert http.get.call_args_list[0].args[0]=='query/12/results'
    assert http.get.call_args_list[1].args[0]=='execution/run_one/results'


@pytest.mark.parametrize('changes',[{'wallet':'bad'},{'wallet':'0x'+'0'*40},{'chain_id':1},
    {'chain_id':None},{'last_seen_at':NOW+1},{'first_seen_at':NOW},{'volume_usd':float('nan')},
    {'buy_count':10},{'sell_count':True}])
def test_dune_bad_rows_rejected(cfg,changes):
    http=Mock();http.get.return_value=dune_page([row(**changes)])
    result=DuneDiscovery(cfg,http).fetch(NOW)
    assert result['rows']==[] and sum(result['rejected'].values())==1


def test_dune_infrastructure_is_not_a_candidate(cfg):
    from pathlib import Path
    Path(cfg.trusted_dex_registry).write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    http=Mock();http.get.return_value=dune_page()
    assert DuneDiscovery(cfg,http).fetch(NOW)['rejected']=={'infrastructure_excluded':1}


def test_dune_limit_is_explicit_without_fetching_excess_rows(cfg):
    http=Mock();http.get.return_value=dune_page(total=1000,nxt=1)
    result=DuneDiscovery(replace(cfg,dune_max_candidates=1),http).fetch(NOW)
    assert result['selection_limited'] and len(result['rows'])==1 and http.get.call_count==1


@pytest.mark.parametrize('changes',[{'next_offset':0},{'next_offset':None},{'execution_id':'../unsafe'},
    {'state':'QUERY_STATE_EXECUTING'},{'query_id':13},{'is_execution_finished':False}])
def test_bad_dune_page_rejected(cfg,changes):
    http=Mock();http.get.return_value=dune_page(total=2,nxt=1,**changes) if 'next_offset' not in changes else dune_page(total=2,**changes)
    with pytest.raises(ExternalError):DuneDiscovery(cfg,http).fetch(NOW)


def test_dune_changed_execution_or_late_page_failure_never_returns_partial_result(cfg):
    http=Mock();http.get.side_effect=[dune_page(total=2,nxt=1),dune_page([row(W2)],total=2,execution_id='different')]
    with pytest.raises(ExternalError):DuneDiscovery(cfg,http).fetch(NOW)


@pytest.mark.parametrize('resource_type', ['pnl', 'wallet_pnl'])
def test_zerion_resolves_numeric_chain_and_requests_only_arc(cfg,resource_type):
    payload=pnl();payload['data']['type']=resource_type
    http=Mock();http.get.side_effect=[chains(),payload]
    data=ZerionClient(cfg,http).pnl(W,NOW)
    assert data['chain']=='arc-mainnet-resolved' and data['data_complete']
    assert http.get.call_args.args[1]=={'currency':'usd','filter[chain_ids]':'arc-mainnet-resolved'}
    assert data['metrics']['cost_basis'] is None and data['metrics']['received'] is None


@pytest.mark.parametrize('registry',[chains('1','arc'),chains('5042002','arc'),
    {'links':{},'data':[]},{'links':{},'data':chains()['data']*2}])
def test_zerion_never_guesses_arc_slug(cfg,registry):
    http=Mock();http.get.return_value=registry
    with pytest.raises(ExternalError):ZerionClient(cfg,http).pnl(W,NOW)
    assert http.get.call_count==1


def test_missing_metrics_stay_none_and_incomplete():
    data=pnl(total_gain=None)
    parsed=normalize_pnl(data,W,'arc',NOW)
    assert parsed['metrics']['total_gain'] is None and not parsed['data_complete']


def test_zerion_aliases_and_asset_breakdown():
    data=pnl(relative_realized_gain_percentage=7,received_external=123,
             breakdown={'by_implementation':{'arc:'+W2:{'total_gain':10}}})
    parsed=normalize_pnl(data,W,'arc',NOW)
    assert parsed['metrics']['realized_gain_percentage']==7 and parsed['metrics']['received']==123
    assert parsed['breakdown']['by_implementation']['arc:'+W2]['total_fee'] is None


@pytest.mark.parametrize('change',['other_chain','wrong_wallet','malformed','nan','scope','excluded'])
@pytest.mark.parametrize('resource_type', ['pnl', 'wallet_pnl'])
def test_zerion_scope_malformed_and_exclusions(change,resource_type):
    data=pnl()
    data['data']['type']=resource_type
    if change=='other_chain':data['data']['attributes']['breakdown']={'by_implementation':{'ethereum:'+W:{'total_gain':1}}}
    if change=='wrong_wallet':data['data']['id']=W2
    if change=='malformed':data['data']['type']='portfolio'
    if change=='nan':data['data']['attributes']['total_gain']=float('inf')
    if change=='scope':data['links']['self']+='?filter%5Bchain_ids%5D=ethereum'
    if change=='excluded':
        data['meta']={'excluded_fungible_ids':['unknown']}
        assert not normalize_pnl(data,W,'arc',NOW)['data_complete'];return
    with pytest.raises(ExternalError):normalize_pnl(data,W,'arc',NOW)


def test_timestamp_timezone_is_required_and_precision_supported():
    assert stamp('2024-12-20T11:04:18.724658237Z')>0
    with pytest.raises(ExternalError):stamp('2024-12-20T11:04:18')


def test_config_secrets_hidden_and_limits_bounded(cfg):
    assert 'DUNE_SECRET' not in repr(cfg) and 'ZERION_SECRET' not in repr(cfg)
    with pytest.raises(ValueError):replace(cfg,dune_max_candidates=1000000)
    with pytest.raises(ValueError):replace(cfg,zerion_refresh_seconds=1)


def test_http_date_retry_after_is_respected():
    from external_http import retry_seconds
    assert retry_seconds('Tue, 14 Nov 2023 22:13:30 GMT',1700000000)==10


def test_smart_account_is_not_excluded_just_for_being_a_contract():
    # No eth_getCode heuristic; only reviewed infrastructure addresses are excluded.
    assert normalize_row(row(),NOW,set())['wallet']==W


def test_dune_partial_result_refused_even_when_execution_finished(cfg):
    data=dune_page();data['result']['metadata']['truncated']=True
    http=Mock();http.get.return_value=data
    with pytest.raises(ExternalError,match='partial_result'):DuneDiscovery(cfg,http).fetch(NOW)
