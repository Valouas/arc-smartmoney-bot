import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from external_http import ExternalError
from external_wallet_pipeline import cycle, run
from external_wallet_report import combined_report
from external_wallet_store import ExternalStore
from historical_snapshots import decoding_context
from historical_priority import PriorityCollector
from paper_copy.webhook import watch_addresses
from run_all import roles_for
from storage import Storage
from test_historical_analysis import fake_collector
from test_external_providers import cfg, row, dune_page, W, W2, NOW, pnl, chains
from dune_discovery import DuneDiscovery
from zerion_client import ZerionClient, normalize_pnl


@pytest.fixture
def local(tmp_path):
    storage=Storage(tmp_path/'arc.sqlite3')
    yield storage
    storage.db.close()


def dune_client(cfg, rows=None):
    http=Mock();http.get.return_value=dune_page(rows)
    return DuneDiscovery(cfg,http)


def zerion_client(cfg):
    http=Mock();http.get.side_effect=[chains(),pnl()]
    return ZerionClient(cfg,http)


def test_end_to_end_observations_queue_local_audit_but_no_authority(local,cfg,tmp_path):
    before=decoding_context(cfg)
    with closing(ExternalStore(local.path)) as store:
        result=cycle(store,cfg,now=NOW,dune_factory=dune_client,zerion_factory=zerion_client)
        assert result['dune_candidates']==result['zerion_wallets']==1 and not result['errors']
        candidate=store.db.execute('SELECT * FROM discovery_candidates').fetchone()
        assert candidate['status']=='EXTERNAL_CANDIDATE' and candidate['source_winners']==0
        assert candidate['first_discovered_at']==NOW # Never source first_seen_at.
        for table in ('candidate_sources','promoted_wallets','events','research_trades','scanner_state'):
            assert store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert store.db.execute('SELECT observed_at FROM external_wallet_candidates').fetchone()[0]==NOW
    rows=combined_report(local.path,cfg,NOW)
    assert len(rows)==1 and rows[0]['status']!='PAPER_SELECTED'
    assert rows[0]['zerion_metrics']['total_gain']==120
    assert rows[0]['live_signal_authority'] is False and rows[0]['reason_not_paper_selected']
    assert watch_addresses(cfg,selection_path=tmp_path/'none.json',paper_path=tmp_path/'none.sqlite3',now=NOW)==set()
    collector=fake_collector(local,cfg)
    due,_=PriorityCollector(collector).candidates(NOW)
    assert W in due
    assert decoding_context(cfg)==before


def test_existing_wallet_merges_without_overwriting_status_discovery_date_or_history(local,cfg):
    local.db.execute("INSERT INTO discovery_candidates(wallet,first_discovered_at,last_discovered_at,status,source_winners) VALUES(?,?,?,'WATCH',3)",(W,NOW-500,NOW-300));local.db.commit()
    previous=tuple(local.db.execute('SELECT * FROM discovery_candidates').fetchone())
    with closing(ExternalStore(local.path)) as store:
        cycle(store,replace(cfg,zerion_enabled=False),now=NOW,dune_factory=dune_client)
        assert tuple(store.db.execute('SELECT * FROM discovery_candidates').fetchone())==previous
        assert store.db.execute('SELECT COUNT(*) FROM discovery_candidates').fetchone()[0]==1
    assert len(combined_report(local.path,cfg,NOW))==1


def test_repeat_execution_idempotent_and_refresh_schedule_persistent(local,cfg):
    client=dune_client(cfg);factory=Mock(return_value=client)
    with closing(ExternalStore(local.path)) as store:
        config=replace(cfg,zerion_enabled=False)
        cycle(store,config,now=NOW,dune_factory=factory)
        cycle(store,config,now=NOW+1,dune_factory=factory)
        assert factory.call_count==1
    with closing(ExternalStore(local.path)) as store:
        cycle(store,config,now=NOW+cfg.dune_refresh_seconds,dune_factory=factory)
        assert store.db.execute('SELECT COUNT(*) FROM external_wallet_metrics').fetchone()[0]==1
        assert store.db.execute('SELECT observed_at FROM external_wallet_candidates').fetchone()[0]==NOW
        assert store.db.execute('SELECT retrieved_at FROM external_wallet_candidates').fetchone()[0]==NOW+cfg.dune_refresh_seconds


def test_late_dune_failure_commits_no_partial_candidates(local,cfg):
    client=dune_client(cfg)
    client.http.get.side_effect=[dune_page(total=2,nxt=1),ExternalError('http_502')]
    with closing(ExternalStore(local.path)) as store:
        result=cycle(store,replace(cfg,zerion_enabled=False),now=NOW,dune_factory=lambda cfg:client)
        assert result['errors']==['dune:http_502']
        assert store.db.execute('SELECT count(*) FROM external_wallet_candidates').fetchone()[0]==0
        assert store.db.execute('SELECT count(*) FROM discovery_candidates').fetchone()[0]==0


def test_error_retains_last_metric_but_marks_report_stale(local,cfg):
    with closing(ExternalStore(local.path)) as store:
        cycle(store,cfg,now=NOW,dune_factory=dune_client,zerion_factory=zerion_client)
        store.metric(W,None,NOW+1,cfg,ExternalError('timeout'))
    rows=combined_report(local.path,cfg,NOW+1)
    assert rows[0]['zerion_metrics']['total_gain']==120
    assert rows[0]['zerion_stale'] and rows[0]['zerion_error']=='timeout'
    assert rows[0]['status']!='PAPER_SELECTED'


def test_zerion_cycle_cap_and_retry_after_prevent_request_storm(local,cfg):
    cfg=replace(cfg,dune_enabled=False,zerion_max_wallets_per_cycle=1)
    for w in (W,W2):local.upsert_discovery_candidate(w,'0x'+'3'*40,1)
    client=Mock();client.pnl.side_effect=ExternalError('http_429',7200)
    with closing(ExternalStore(local.path)) as store:
        cycle(store,cfg,now=NOW,zerion_factory=lambda cfg:client)
        assert client.pnl.call_count==1
        assert not store.due('zerion','',NOW+7199)
        cycle(store,cfg,now=NOW+4000,zerion_factory=lambda cfg:client)
        assert client.pnl.call_count==1


def test_infrastructure_excluded_from_both_ingestion_and_enrichment(local,cfg):
    Path(cfg.trusted_dex_registry).write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    z=Mock()
    with closing(ExternalStore(local.path)) as store:
        result=cycle(store,cfg,now=NOW,dune_factory=dune_client,zerion_factory=z)
        assert result['dune_candidates']==0
        assert store.db.execute('SELECT count(*) FROM discovery_candidates').fetchone()[0]==0
        z.assert_not_called()


def test_disabled_run_does_not_create_database_or_call_network(tmp_path,cfg):
    path=tmp_path/'missing.sqlite3'
    assert run(once=True,cfg=replace(cfg,external_discovery_enabled=False),db_path=str(path))==0
    assert not path.exists()


def test_master_switch_does_not_disable_history_or_enable_alchemy_directly(cfg):
    old=roles_for(replace(cfg,external_discovery_enabled=False,historical_analysis_enabled=True))
    new=roles_for(replace(cfg,external_discovery_enabled=True,historical_analysis_enabled=True))
    assert set(new)-set(old)=={'external_wallet_pipeline'} and 'historical_analysis' in new


def test_external_data_cannot_forge_paper_contract_but_local_policy_can(local,cfg,tmp_path,monkeypatch):
    from test_paper_admission import review_row, document
    from test_historical_analysis import NOW as POLICY_NOW
    from paper_copy.selection import load_selection, SelectionError
    from test_alchemy_sync import client, hook, page
    review=review_row(cfg)
    assert review['paper_review_eligible']
    contract=tmp_path/'paper_review_selection.json'
    contract.write_text(json.dumps(document([review])))
    monkeypatch.setattr('storage.time.time',lambda:POLICY_NOW)
    local.upsert_discovery_candidate(W,'0x'+'3'*40,1)
    # Only the existing engine-generated/consumer-validated contract authorizes paper.
    rows=combined_report(local.path,cfg,POLICY_NOW,selection_path=contract)
    assert rows[0]['status']=='PAPER_SELECTED'
    watched=watch_addresses(cfg,selection_path=contract,paper_path=tmp_path/'missing.sqlite3',now=POLICY_NOW)
    assert watched=={W}
    sync_cfg=replace(cfg,alchemy_notify_auth_token='fixture',alchemy_webhook_id='wh_fixture',
        alchemy_webhook_expected_url='https://example.test/webhook/alchemy',alchemy_webhook_signing_key='fixture')
    sync=client(sync_cfg,hook(sync_cfg),page([]),{},page([W]))
    assert sync.sync(watched)['added_count']==1
    assert [c.args[0] for c in sync.session.request.call_args_list]==['GET','GET','PATCH','GET']
    contract.write_text(json.dumps({'wallets':[{'wallet':W,'total_gain':1000000,'status':'PAPER_SELECTED'}]}))
    with pytest.raises(SelectionError):load_selection(contract,cfg,POLICY_NOW)


def test_combined_report_is_read_only_and_missing_external_tables_supported(local,cfg,monkeypatch):
    monkeypatch.setattr('storage.time.time',lambda:NOW)
    local.upsert_discovery_candidate(W,'0x'+'3'*40,1)
    before=list(local.db.execute("SELECT sql FROM sqlite_master ORDER BY name"))
    rows=combined_report(local.path,cfg,NOW)
    assert rows[0]['zerion_metrics'] is None
    assert list(local.db.execute("SELECT sql FROM sqlite_master ORDER BY name"))==before


def test_wrong_database_is_not_migrated(tmp_path):
    path=tmp_path/'other.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE user_data(value TEXT)')
    with pytest.raises(ExternalError,match='wrong_local_database'):ExternalStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('user_data',)]


def test_observation_time_is_after_retrieval_not_source_history(local,cfg,monkeypatch):
    times=iter([NOW,NOW+20])
    monkeypatch.setattr('external_wallet_pipeline.time.time',lambda:next(times))
    with closing(ExternalStore(local.path)) as store:
        cycle(store,replace(cfg,zerion_enabled=False),dune_factory=dune_client)
        assert store.db.execute('SELECT observed_at FROM external_wallet_candidates').fetchone()[0]==NOW+20
        assert store.db.execute('SELECT first_discovered_at FROM discovery_candidates').fetchone()[0]==NOW+20


def test_dune_existing_result_age_is_not_hidden_by_new_download(local,cfg):
    config=replace(cfg,zerion_enabled=False)
    with closing(ExternalStore(local.path)) as store:
        cycle(store,config,now=NOW,dune_factory=dune_client)
        cycle(store,config,now=NOW+1000,dune_factory=dune_client)
    report=combined_report(local.path,cfg,NOW+1000)[0]
    assert report['dune_stale'] and report['dune_retrieved_at']==NOW+1000


def test_external_observation_is_not_visible_before_its_acceptance(local,cfg):
    with closing(ExternalStore(local.path)) as store:
        cycle(store,cfg,now=NOW,dune_factory=dune_client,zerion_factory=zerion_client)
    assert combined_report(local.path,cfg,NOW-1)==[]
    assert len(combined_report(local.path,cfg,NOW))==1


def test_recovered_provider_error_does_not_mark_fresh_metrics_stale(local,cfg):
    cfg=replace(cfg,dune_enabled=False)
    local.upsert_discovery_candidate(W,'0x'+'3'*40,1)
    with closing(ExternalStore(local.path)) as store:
        with store.db:store.record_attempt('zerion','',NOW-4000,3600,'http_429')
        cycle(store,cfg,now=NOW,zerion_factory=zerion_client)
        assert store.db.execute("SELECT error_category FROM external_fetch_state WHERE provider='zerion' AND wallet='' ").fetchone()[0] is None
