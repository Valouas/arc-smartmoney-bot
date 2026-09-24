import json
from contextlib import closing
from unittest.mock import Mock

import pytest

from dune_discovery import DuneDiscovery
from dune_history import COVERAGE, SQL, collect, fetch_window, import_window, windows
from external_http import ExternalError, digest
from external_wallet_store import ExternalStore
from test_external_providers import cfg, row, dune_page, W, NOW
from test_external_pipeline import local


def window():
    return {'start':'2023-11-01','end':'2023-12-01','status':'SUBMITTED','execution_id':'run_one'}


CUTOFF='2023-11-15 00:00:00'


def history_row(**kw):
    return row(buy_count=1,sell_count=0,trade_count=1,distinct_tokens=1,
        best_observed_multiple=10,winner_tokens=1,coverage=COVERAGE,
        window_start='2023-11-01',window_end='2023-12-01',observation_cutoff=CUTOFF,**kw)


def api_for(rows=None):
    api=Mock()
    api.http.get.return_value=dune_page([history_row()] if rows is None else rows)
    return api


def test_half_open_month_windows_cover_every_date_once():
    periods=windows('2026-05-27','2026-09-21')
    assert len(periods)==5 and periods[0]['start']=='2026-05-27' and periods[-1]['end']=='2026-09-21'
    assert all(a['end']==b['start'] for a,b in zip(periods,periods[1:]))
    with pytest.raises(ValueError):windows('2026-09-21','2026-09-21')


def test_exhaustive_fetch_goes_beyond_500_and_pins_execution(cfg):
    rows=[row('0x'+format(i,'040x')) for i in range(1,1002)]
    api=Mock()
    def page(path,params):
        assert path=='execution/run_one/results'
        n=params['offset'];batch=rows[n:n+params['limit']]
        return dune_page(batch,total=len(rows),nxt=n+len(batch) if n+len(batch)<len(rows) else None)
    api.get.side_effect=page
    result=DuneDiscovery(cfg,api).fetch(NOW,execution_id='run_one',exhaustive=True)
    assert len(result['rows'])==1001 and not result['selection_limited']
    assert api.get.call_count==11


@pytest.mark.parametrize('field,value',[('best_observed_multiple',9.99),('winner_tokens',0),
    ('observation_cutoff','2023-11-16 00:00:00'),('window_end','2023-11-02'),
    ('coverage','unreviewed'),('first_seen_at',0)])
def test_history_scope_and_x10_evidence_are_required(cfg,field,value):
    r=history_row();r[field]=value
    with pytest.raises(ExternalError):fetch_window(api_for([r]),cfg,window(),CUTOFF)


def test_one_buy_at_exactly_x10_is_candidate_only_and_import_is_idempotent(local,cfg):
    result=fetch_window(api_for(),cfg,window(),CUTOFF)
    with closing(ExternalStore(local.path)) as store:
        import_window(store,result,cfg);import_window(store,result,cfg)
        c=store.db.execute('SELECT * FROM discovery_candidates').fetchone()
        assert c['status']=='EXTERNAL_CANDIDATE' and c['source_winners']==0
        assert store.db.execute("SELECT COUNT(*) FROM external_wallet_metrics WHERE provider='dune_x10'").fetchone()[0]==1
        for table in ('events','promoted_wallets','candidate_sources','scanner_state'):
            assert store.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
        # A later window for the same wallet preserves the first discovery and
        # both pieces of evidence; it does not create a second identity.
        before=tuple(c);result['execution_id']='run_two'
        import_window(store,result,cfg)
        assert tuple(store.db.execute('SELECT * FROM discovery_candidates').fetchone())==before
        assert store.db.execute('SELECT COUNT(*) FROM external_wallet_metrics').fetchone()[0]==2


def make_job(w):
    return {'windows':[w],'cutoff':CUTOFF,'sql_hash':digest(SQL.read_text(encoding='utf-8')),
            'usage_start':{'period':'2023-11-01','used':0},'max_credits':500}


def test_uncertain_execution_is_not_submitted_twice(local,cfg,tmp_path):
    w=window();w['status']='NEW';w.pop('execution_id')
    job=make_job(w);api=Mock()
    api.http.get.return_value={'query_sql':SQL.read_text(encoding='utf-8')}
    api.usage.return_value={'period':'2023-11-01','used':0,'included':2500}
    api.post.side_effect=ExternalError('submission_uncertain')
    state=tmp_path/'job.json'
    with closing(ExternalStore(local.path)) as store:
        with pytest.raises(ExternalError,match='submission_uncertain'):collect(job,state,api,cfg,store)
        recovered=json.loads(state.read_text())
        assert recovered['windows'][0]['status']=='SUBMITTING'
        with pytest.raises(ExternalError,match='execution_requires_review'):collect(recovered,state,api,cfg,store)
    assert api.post.call_count==1


def test_bad_last_page_cannot_complete_or_import_window(local,cfg,tmp_path):
    w=window();job=make_job(w);api=Mock()
    bad=dune_page([history_row()],total=2,nxt=1)
    api.http.get.side_effect=[{'query_sql':SQL.read_text(encoding='utf-8')},
        {'execution_id':'run_one','query_id':int(cfg.dune_query_id),'state':'QUERY_STATE_COMPLETED','is_execution_finished':True},
        bad,ExternalError('http_502')]
    with closing(ExternalStore(local.path)) as store:
        with pytest.raises(ExternalError,match='http_502'):collect(job,tmp_path/'job',api,cfg,store,sleep=Mock())
        assert store.db.execute('SELECT COUNT(*) FROM external_wallet_candidates').fetchone()[0]==0
        assert store.db.execute('SELECT COUNT(*) FROM discovery_candidates').fetchone()[0]==0
    assert w['status']=='SUBMITTED';api.post.assert_not_called()


def test_fetched_window_resumes_import_without_new_execution(local,cfg,tmp_path):
    result=fetch_window(api_for(),cfg,window(),CUTOFF)
    result_file=tmp_path/'result.json';result_file.write_text(json.dumps(result))
    w={**window(),'status':'FETCHED','result_path':str(result_file)};job=make_job(w)
    api=Mock();api.http.get.return_value={'query_sql':SQL.read_text(encoding='utf-8')}
    api.usage.return_value={'period':'2023-11-01','used':10,'included':2500}
    with closing(ExternalStore(local.path)) as store:
        collect(job,tmp_path/'job',api,cfg,store)
        assert store.db.execute('SELECT COUNT(*) FROM discovery_candidates').fetchone()[0]==1
    assert job['status']=='COMPLETE';api.post.assert_not_called()


def test_budget_prevents_new_sql_submission(local,cfg,tmp_path):
    w=window();w['status']='NEW';job=make_job(w)
    api=Mock();api.http.get.return_value={'query_sql':SQL.read_text(encoding='utf-8')}
    api.usage.return_value={'period':'2023-11-01','used':500,'included':2500}
    with closing(ExternalStore(local.path)) as store:
        with pytest.raises(ExternalError,match='research_credit_budget'):collect(job,tmp_path/'job',api,cfg,store)
    api.post.assert_not_called();assert w['status']=='NEW'


def test_duplicate_wallet_across_pages_refuses_entire_window(cfg):
    api=Mock();api.http.get.side_effect=[dune_page([history_row()],total=2,nxt=1),dune_page([history_row()],total=2)]
    with pytest.raises(ExternalError,match='duplicate_wallet'):fetch_window(api,cfg,window(),CUTOFF)


def test_zero_wallet_period_is_complete_only_with_valid_empty_result(cfg):
    api=Mock();api.http.get.return_value=dune_page([],total=0)
    result=fetch_window(api,cfg,window(),CUTOFF)
    assert result['rows']==[] and not result['selection_limited']
    api.http.get.return_value=dune_page([],total=1)
    with pytest.raises(ExternalError,match='invalid_pagination'):fetch_window(api,cfg,window(),CUTOFF)


def test_history_keeps_existing_candidate_status_and_dates(local,cfg):
    with local.db:
        local.db.execute("INSERT INTO discovery_candidates(wallet,first_discovered_at,last_discovered_at,status) VALUES(?,?,?,'WATCH')",(W,NOW-500,NOW-100))
    before=tuple(local.db.execute('SELECT * FROM discovery_candidates').fetchone())
    with closing(ExternalStore(local.path)) as store:
        import_window(store,fetch_window(api_for(),cfg,window(),CUTOFF),cfg)
    assert tuple(local.db.execute('SELECT * FROM discovery_candidates').fetchone())==before
