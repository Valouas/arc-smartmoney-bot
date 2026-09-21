import json
from contextlib import closing
from dataclasses import replace
from unittest.mock import Mock

import pytest

from external_http import ExternalError,digest
from external_wallet_store import ExternalStore
from nansen_bulk import step,replay_imports,run
from nansen_client import NansenHTTP,SUMMARY,DETAIL
from nansen_queue import Campaign
from run_all import roles_for
from test_external_pipeline import local
from test_external_providers import cfg,W,W2
from test_nansen import overview,page,position,response as trial_response,DATE


def response(data=None,**headers):
    return trial_response(data,**{'X-Nansen-Credits-Remaining':'20000',**headers})


def rows():
    return [{'wallet':w,'infrastructure_excluded':False,'zerion_metrics':{'total_gain':v}}
            for w,v in [(W,100),(W2,-500)]]


def queue_at(path,cap=20000,trial=None):
    q=Campaign(path);q.initialize(rows(),trial or {'date':DATE,'cache':{}},cap);return q


def transport(q,session=None):
    return NansenHTTP('dummy',q.state,q.persist,session or Mock(),sleep=Mock())


def test_summaries_all_before_details_no_authority_and_negative_wallet_kept(local,tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        session=Mock();session.post.side_effect=[response(),response(overview(realized_pnl_usd=-500)),
            response(page()),response(page([position(W)])),response(page()),response(page([]))]
        h=transport(q,session)
        for _ in range(6):step(q,h,store)
        assert q.progress()['summaries']==q.progress()['details']==2
        assert [c.args[0].rsplit('/',1)[-1] for c in session.post.call_args_list[:2]]==['pnl-summary']*2
        assert session.post.call_count==6
        assert store.db.execute("SELECT count(*) FROM external_wallet_metrics WHERE provider='nansen_summary'").fetchone()[0]==2
        assert store.db.execute("SELECT count(*) FROM external_wallet_metrics WHERE provider='nansen'").fetchone()[0]==2
        for t in ('discovery_candidates','promoted_wallets','wallet_profiles','events','research_trades','scanner_state'):
            assert store.db.execute('SELECT count(*) FROM '+t).fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM external_wallet_metrics WHERE data_complete!=0').fetchone()[0]==0
        assert q.state['status']=='COMPLETE'
        q.exclude({W})  # Excluding an already collected wallet must not double-count it.
        assert not step(q,h,store) and q.state['status']=='COMPLETE'


def test_summary_report_is_explicitly_realized_only_and_no_authority(local,tmp_path,cfg):
    from external_wallet_report import combined_report
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        session=Mock();session.post.return_value=response()
        step(q,transport(q,session),store)
    row=combined_report(local.path,cfg)[0]
    assert row['nansen_summary']['realized_pnl_usd']==1000
    assert row['nansen_metrics'] is None
    assert row['nansen_summary_date']==DATE and not row['live_signal_authority']


def test_campaign_cache_participates_in_consistent_backups(tmp_path):
    from automatic_backup import SOURCES,backup_database
    assert any(p.name=='nansen_bulk.sqlite3' for p in SOURCES)
    path=tmp_path/'nansen_bulk.sqlite3';target=tmp_path/'backup.sqlite3'
    with closing(queue_at(path)) as q:
        q.state['attempts']=123;q.persist()
        q.state['cache']['saved']={'response':{'some':'page'}}
        assert backup_database(path,target,10)['quick_check']=='ok'
    with closing(Campaign(target)) as q:
        assert q.state['attempts']==123 and 'saved' in q.state['cache']


def test_trial_pages_reused_without_charging_and_frozen_scope(tmp_path):
    body={'chain':'arc','address':W,'date':DATE};key=digest([SUMMARY,body])
    cached={'route':SUMMARY,'request':body,'response':overview(),'received_at':1700000000}
    p=tmp_path/'queue.sqlite3'
    with closing(queue_at(p,trial={'date':DATE,'cache':{key:cached}})) as q:
        h=transport(q)
        assert h.post(SUMMARY,body)==overview()
        assert q.state['attempts']==0 and q.state['credits_remaining'] is None
        h.session.post.assert_not_called()
    with closing(queue_at(p)) as q:
        assert q.state['date']==DATE and key in q.state['cache']
        with pytest.raises(ExternalError,match='budget_changed'):q.initialize([],{},21000)


def test_budget_persists_across_restarts_no_repayment(local,tmp_path):
    path=tmp_path/'queue.sqlite3'
    with closing(queue_at(path,cap=1)) as q,closing(ExternalStore(local.path)) as store:
        session=Mock();session.post.return_value=response()
        assert step(q,transport(q,session),store)
    with closing(queue_at(path,cap=1)) as q,closing(ExternalStore(local.path)) as store:
        session=Mock()
        assert not step(q,transport(q,session),store)
        assert q.state['attempts']==1 and q.state['status']=='BUDGET_STOP'
        session.post.assert_not_called()
        assert q.progress()['summaries']==1


def test_reserve_stops_on_fresh_provider_balance(local,tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        session=Mock();session.post.return_value=response(**{'X-Nansen-Credits-Remaining':'100'})
        h=transport(q,session)
        step(q,h,store);step(q,h,store)
        assert q.state['status']=='BUDGET_STOP' and session.post.call_count==1


def test_queue_commit_before_import_can_be_replayed_without_network(local,tmp_path):
    from test_nansen import client
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        p=client().pnl(W,DATE);p['retrieved_at']=1700000000
        q.save_result(W,p)
        replay_imports(q,store);replay_imports(q,store)
        assert store.db.execute('SELECT count(*) FROM external_wallet_metrics').fetchone()[0]==1


def test_infrastructure_exclusion_is_rechecked_without_deleting_history(local,tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        q.exclude({W});assert q.next_job('SUMMARIES')['wallet']==W2
        assert q.progress()['total']==2 and q.progress()['excluded']==1
        assert q.db.execute('SELECT wallet FROM jobs WHERE wallet=?',(W,)).fetchone()


def test_cache_is_separate_from_small_metadata_even_for_many_pages(tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q:
        for i in range(100):q.state['cache'][str(i)]={'response':{'large':'x'*10000}}
        q.persist()
        meta=q.db.execute('SELECT payload FROM campaign').fetchone()[0]
        assert len(meta)<2000 and 'cache' not in json.loads(meta)
        assert q.db.execute('SELECT count(*) FROM pages').fetchone()[0]==100


def test_repeated_schema_errors_stop_instead_of_consuming_whole_budget(local,tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        with q.db:q.db.execute('INSERT INTO jobs(wallet,prior) VALUES(?,0)',('0x'+'3'*40,))
        h=Mock();h.post.side_effect=ExternalError('invalid_nansen_response')
        for _ in range(3):step(q,h,store)
        assert q.state['halt']=='repeated_nansen_schema_errors'
        assert not step(q,h,store) and h.post.call_count==3
        assert q.progress()['errors']==3


def test_transient_deferral_retains_job_and_no_early_requests(local,tmp_path):
    with closing(queue_at(tmp_path/'queue.sqlite3')) as q,closing(ExternalStore(local.path)) as store:
        h=Mock();h.post.side_effect=ExternalError('nansen_timeout',3600)
        assert not step(q,h,store)
        assert not step(q,h,store)
        assert q.state['status']=='RETRY_WAIT' and h.post.call_count==1
        assert q.progress()['summaries']==0 and q.progress()['total']==2


def test_disabled_run_does_not_create_database_or_call_api(cfg,tmp_path):
    path=tmp_path/'queue.sqlite3';factory=Mock()
    assert run(replace(cfg,nansen_bulk_enabled=False),once=True,campaign_path=path,http_factory=factory)==0
    assert not path.exists();factory.assert_not_called()


def test_supervisor_includes_bulk_only_when_enabled_paper_remains_paused(cfg):
    configured=replace(cfg,paper_copy_paused=True,nansen_bulk_enabled=True)
    assert 'nansen_bulk' in roles_for(configured) and 'paper_copy' not in roles_for(configured)
    assert 'nansen_bulk' not in roles_for(replace(configured,nansen_bulk_enabled=False))


def test_invalid_trial_cache_rolls_back_seed_jobs(tmp_path):
    path=tmp_path/'queue.sqlite3'
    with closing(Campaign(path)) as q:
        with pytest.raises(ExternalError):
            q.initialize(rows(),{'date':DATE,'cache':{'bad':{'route':SUMMARY,'request':{},'response':{}}}},20000)
    with closing(Campaign(path)) as q:
        assert q.state is None and q.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


def test_dashboard_nansen_status_whitelists_fields_and_marks_stale(tmp_path):
    import dashboard
    path=tmp_path/'data';path.mkdir()
    (path/'nansen_bulk_status.json').write_text(json.dumps({'status':'SUMMARIES','updated_at':100,
        'total':7553,'summaries':100,'api_key':'NEVER_EXPOSE'}))
    n=dashboard.snapshot(tmp_path,now=105)['nansen']
    assert n['fresh'] and n['summaries']==100 and 'api_key' not in n
    assert not dashboard.snapshot(tmp_path,now=200)['nansen']['fresh']


def test_live_dashboard_freshness_uses_time_when_panel_is_read(tmp_path,monkeypatch):
    import dashboard
    path=tmp_path/'data';path.mkdir()
    (path/'nansen_bulk_status.json').write_text(json.dumps({'status':'SUMMARIES','updated_at':105}))
    monkeypatch.setattr(dashboard.time,'time',Mock(side_effect=[100,106,106]))
    assert dashboard.snapshot(tmp_path)['nansen']['fresh']
