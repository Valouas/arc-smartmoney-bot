"""Reviewed infrastructure is evidence, never a trader; cleanup preserves history."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from web3 import Web3

import config
from auto_discovery import AutoDiscovery
from bot import ArcSmartMoneyBot
from cleanup_dex_infrastructure import cleanup, WALLET_TABLES
from cluster import ClusterBook
from decoder import classify_trade
from dex_registry import TrustedDexRegistry
from forward_metrics import build_forward_profile
from intelligence_book import IntelligenceBook
from models import WalletInfo
from process_lock import ProcessLock
from rpc import ArcRPC, USDC_ERC20
from storage import Storage
from wallet_policy import (ExcludedInfrastructureError, InfrastructureRegistryError,
                           infrastructure_addresses, is_dex_infrastructure)
from wallet_profiler import WalletProfiler
from watchlist_manager import WatchlistManager
from test_audit_regressions import W, T, H, event, transfer, receipt
from test_followup import make_bot

INFRA = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
CODE = b'reviewed-poolmanager-test-bytecode'


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / 'trusted_dex.json'
    path.write_text(json.dumps({'chain_id': 5042, 'emitters': [
        {'address': Web3.to_checksum_address(INFRA), 'runtime_code_hash': Web3.to_hex(Web3.keccak(CODE))}]}))
    cfg = replace(config.Settings(), trusted_dex_registry=str(path))
    monkeypatch.setattr(config, 'SETTINGS', cfg)
    return cfg


@pytest.fixture
def db(tmp_path):
    storage = Storage(tmp_path / 'bot.sqlite3')
    yield storage
    storage.db.close()


def discovery(db, cfg):
    obj = AutoDiscovery.__new__(AutoDiscovery)
    obj.db, obj.cfg = db, cfg
    return obj


def profile(wallet):
    return dict(wallet=wallet, generated_at=int(time.time()), smart_score=80, profile_class='SMART',
                recommended_weight=3, confirmation_eligible=True, launches_bought=10,
                opportunity_capture_rate=0.1, evaluable_trades=8, winners=3,
                profile_version=2, data_quality_ok=True, independent_winners=3)


def legacy_candidate(db, wallet):
    # Simulate rows written by the old version, bypassing the new API guard.
    db.db.execute("""INSERT INTO discovery_candidates
        (wallet,first_discovered_at,last_discovered_at,source_winners,best_early_rank,status,hydrated_at,history_complete)
        VALUES(?,1,1,3,1,'QUARANTINE',1,1)""", (wallet,))
    db.db.execute('INSERT INTO candidate_sources VALUES(?,?,1,1)', (wallet,T))
    db.db.execute('INSERT INTO promoted_wallets VALUES(?,1,90,\'SMART\',4,\'old\',1)', (wallet,))
    p = profile(wallet)
    db.db.execute('INSERT INTO wallet_profiles VALUES(?,1,90,\'SMART\',4,1,?)', (wallet,json.dumps(p)))
    db.commit()


def test_reviewed_emitter_still_confirms_swap_for_unlisted_contract_account(policy, db):
    rpc = ArcRPC.__new__(ArcRPC)
    rpc.dex_registry = TrustedDexRegistry(policy.trusted_dex_registry, 5042)
    rpc.w3 = SimpleNamespace(eth=SimpleNamespace(get_code=Mock(
        side_effect=lambda address, **kwargs: b'smart-account-code' if address.lower() == W else CODE)))
    rpc.token_meta = lambda _: {'decimals': 18, 'symbol': 'T'}
    assert rpc.get_code(W)  # The trader really is a contract in this fixture.
    assert rpc.swap_emitter_trusted(INFRA, 10)
    swap = receipt([transfer(USDC_ERC20,W,INFRA,100*10**6), transfer(T,INFRA,W,10**18)])
    swap['logs'][-1]['address'] = INFRA
    swap['blockNumber'] = 10
    assert classify_trade(rpc,swap,W,25)[0]['confirmation_eligible']
    assert classify_trade(rpc,swap,INFRA,25) == []
    assert db.upsert_discovery_candidate(W,T,1)
    assert db.save_promoted_wallet(W,80,'SMART',3,'smart account')
    db.db.execute('UPDATE discovery_candidates SET history_complete=1 WHERE wallet=?',(W,)); db.commit()
    assert db.wallet_eligible(W) and not db.wallet_eligible(INFRA)


@pytest.mark.parametrize('wallet', [INFRA, Web3.to_checksum_address(INFRA), INFRA.upper()])
def test_storage_cannot_create_candidate_profile_or_promotion(policy, db, wallet):
    assert is_dex_infrastructure(wallet)
    assert db.upsert_discovery_candidate(wallet,T,1) is False
    assert db.save_promoted_wallet(wallet,100,'SMART',4,'pool') is False
    assert db.save_wallet_profile(profile(wallet)) is False
    assert not db.wallet_eligible(wallet)
    for table in WALLET_TABLES:
        assert db.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_early_buyers_skip_infrastructure_before_network_and_rank(policy, db, monkeypatch):
    d = discovery(db,policy)
    d._all_transfer_rows = lambda _: [{'to':INFRA,'tx_hash':H}, {'to':W,'tx_hash':H}]
    d.rpc = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=Mock(return_value={}))),
                            get_receipt=Mock(return_value={}))
    decode = Mock(return_value=[{'side':'BUY','token':T,'confirmation_eligible':True}])
    monkeypatch.setattr('auto_discovery.classify_trade',decode)
    assert d.early_buyers(T) == [(W,1,H)]
    d.rpc.get_receipt.assert_called_once()
    assert decode.call_args.args[2] == W


def test_winner_insert_guard_even_with_old_buyer_list(policy, db):
    d = discovery(db,policy)
    db.db.execute('INSERT INTO discovery_winners VALUES(?,1,150,NULL)',(T,)); db.commit()
    d.early_buyers = lambda _: [(INFRA,1,H),(W,2,H)]
    assert d.discover_from_winners() == 1
    assert [r[0] for r in db.db.execute('SELECT wallet FROM discovery_candidates')] == [W]


def test_infrastructure_not_hydrated_or_counted_in_wallet_budget(policy, db):
    legacy_candidate(db,INFRA)
    db.upsert_discovery_candidate(W,T,2)
    db.db.execute('UPDATE discovery_candidates SET hydrated_at=NULL,history_complete=0'); db.commit()
    d = discovery(db,replace(policy,discovery_max_wallets_per_cycle=1))
    d.activity_hashes = Mock(side_effect=AssertionError('No RPC/history for infrastructure'))
    assert d.hydrate_wallet(INFRA) == 0
    d.activity_hashes.assert_not_called()
    d.hydrate_wallet = Mock(return_value=0)
    assert d.hydrate_candidates() == 1
    d.hydrate_wallet.assert_called_once_with(W)


def test_no_promotion_scoring_for_infrastructure_true_wallet_gates_unchanged(policy, db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy_candidate(db,INFRA)
    db.upsert_discovery_candidate(W,T,1)
    db.db.execute('UPDATE discovery_candidates SET first_discovered_at=1,hydrated_at=1,history_complete=1,source_winners=3 WHERE wallet=?',(W,)); db.commit()
    # The legacy promoted emitter must not consume the real wallet's one slot.
    d = discovery(db,replace(policy,promotion_max_wallets=1))
    d.profile_candidate = Mock(side_effect=lambda row: profile(row['wallet']))
    d._funding_collision = Mock(return_value=False)
    assert d.promote_candidates() == 1
    assert d.profile_candidate.call_count == 1
    assert d.profile_candidate.call_args.args[0]['wallet'] == W
    assert db.wallet_eligible(W)
    assert not db.wallet_eligible(INFRA)
    data = json.loads((tmp_path/'generated_watchlist.json').read_text())
    assert [r['address'] for r in data['wallets']] == [W]


def test_direct_profiling_entrypoints_refuse_infrastructure(policy, db):
    d = discovery(db,policy)
    p = WalletProfiler.__new__(WalletProfiler); p.db=db; p.cfg=policy
    for call in (lambda: d.profile_candidate({'wallet':INFRA}), lambda: p.profile_wallet(INFRA),
                 lambda: build_forward_profile(db,INFRA,policy)):
        with pytest.raises(ExcludedInfrastructureError): call()
    assert db.db.execute('SELECT count(*) FROM trade_outcomes').fetchone()[0] == 0


def test_wallet_profiler_ignores_historical_infrastructure_events(policy, db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.add_event(event(wallet=INFRA)); db.add_event(event(wallet=W,tx_hash='real'))
    p = WalletProfiler.__new__(WalletProfiler); p.db=db; p.cfg=policy
    p.market = Mock()
    monkeypatch.setattr('wallet_profiler.collect_outcomes',Mock(return_value=0))
    assert p.wallets() == [W]
    result = p.cycle()
    assert [r['wallet'] for r in result] == [W]
    assert list(json.loads(Path('data/wallet_intelligence.json').read_text())['profiles']) == [W]
    assert db.db.execute('SELECT count(*) FROM events').fetchone()[0] == 2


def test_seed_generated_watchlists_and_stale_intelligence_have_no_infrastructure_authority(policy, tmp_path):
    base, generated, intel = [tmp_path / name for name in ('seeds.json','generated.json','intel.json')]
    base.write_text(json.dumps({'wallets':[{'address':INFRA,'weight':4},{'address':W,'weight':3}]}))
    generated.write_text(json.dumps({'wallets':[{'address':Web3.to_checksum_address(INFRA),'weight':4}]}))
    before = base.read_bytes()
    watch = WatchlistManager(base,generated,0); watch.reload(True)
    assert set(watch.items) == {W} and base.read_bytes() == before
    intel.write_text(json.dumps({'profiles':{INFRA:profile(INFRA),W:profile(W)}}))
    book = IntelligenceBook(intel,0)
    assert book.effective(W,80,3)['eligible']
    assert book.get(INFRA) is None
    assert book.effective(INFRA,100,4) == {'score':0,'class':'UNVERIFIED','weight':0,'eligible':False}


def test_scanner_skips_old_runtime_emitter_and_never_handles_it(policy, db, tmp_path):
    old = event(wallet=INFRA,confirmation_eligible=True)
    db.save_runtime_event(old)
    b = make_bot(db,tmp_path); b.cfg=policy
    b.watch[INFRA] = WalletInfo(INFRA,'old emitter',4,'seed')
    b.rpc = SimpleNamespace(get_block=lambda *a,**k:{'timestamp':1001,'transactions':[]},
                            watched_participants=Mock(return_value={}))
    b.process_block(1)
    assert INFRA not in b.watch
    assert not b.clusters.events
    b.handle_event(old)
    assert not b.clusters.events
    assert db.db.execute('SELECT count(*) FROM events').fetchone()[0] == 0
    assert db.db.execute('SELECT count(*) FROM runtime_events').fetchone()[0] == 1


def test_cluster_drops_emitter_added_to_registry_mid_run(policy):
    book = ClusterBook(1200,600)
    # A stale in-memory queue from before the address was registered.
    book.events[T]['BUY'].append(event(wallet=INFRA,wallet_weight=4,confirmation_eligible=True))
    signal = book.add(event(wallet=W,wallet_weight=3,confirmation_eligible=True))
    assert signal.cluster_weight == 3 and signal.distinct_wallets == 1
    assert all(e.wallet == W for e in signal.events)
    with pytest.raises(ExcludedInfrastructureError): book.add(event(wallet=INFRA))


@pytest.mark.parametrize('code_hash', [None,'bad','0x'+'0'*64])
def test_listed_address_excluded_even_when_code_hash_cannot_grant_swap_trust(policy, code_hash):
    path = Path(policy.trusted_dex_registry)
    path.write_text(json.dumps({'chain_id':5042,'emitters':[{'address':INFRA,'runtime_code_hash':code_hash}]}))
    assert is_dex_infrastructure(INFRA,policy)
    assert not TrustedDexRegistry(path,5042).trusts(INFRA,CODE)


@pytest.mark.parametrize('payload', [[], {'chain_id':1,'emitters':[]}, {'chain_id':5042,'emitters':[None]},
                                   {'chain_id':5042,'emitters':[{'address':'bad'}]}])
def test_bad_registry_blocks_wallet_authority(policy, payload):
    Path(policy.trusted_dex_registry).write_text(json.dumps(payload))
    with pytest.raises(InfrastructureRegistryError): infrastructure_addresses(policy)


def test_new_reviewed_emitter_invalidates_cached_exclusion(policy, tmp_path):
    assert not is_dex_infrastructure(W,policy)
    path = Path(policy.trusted_dex_registry)
    data = json.loads(path.read_text())
    data['emitters'].append({'address':W,'runtime_code_hash':Web3.to_hex(Web3.keccak(CODE))})
    path.write_text(json.dumps(data))
    assert is_dex_infrastructure(W,policy)
    intel = tmp_path/'intel.json'
    intel.write_text(json.dumps({'profiles':{W:profile(W)}}))
    assert not IntelligenceBook(intel,0).effective(W,100,4)['eligible']


def seed_cleanup(db, policy, tmp_path):
    legacy_candidate(db,Web3.to_checksum_address(INFRA))
    legacy_candidate(db,W)
    for e in (event(wallet=INFRA),event(wallet=W,token=INFRA,tx_hash='real-trade')):
        db.add_event(e); db.add_research_trade(e); db.save_runtime_event(e)
    db.checkpoint(123)
    generated, intel = tmp_path/'generated.json', tmp_path/'intel.json'
    generated.write_text(json.dumps({'custom':'preserve','wallets':[
        {'address':INFRA,'label':'pool'},{'address':W,'label':'real','custom':42}]}))
    intel.write_text(json.dumps({'generated_at':42,'custom':'preserve','profiles':{
        Web3.to_checksum_address(INFRA):profile(INFRA),W:profile(W)}}))
    kwargs = dict(database=db.path,generated=generated,intelligence=intel,cfg=policy)
    return kwargs


def db_snapshot(db):
    tables = [r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {table:[tuple(r) for r in db.db.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}


def test_cleanup_preview_apply_backup_idempotence_and_preserved_history(policy, db, tmp_path):
    kwargs = seed_cleanup(db,policy,tmp_path)
    before = db_snapshot(db)
    generated_before = kwargs['generated'].read_bytes()
    intel_before = kwargs['intelligence'].read_bytes()
    preview = cleanup(**kwargs)
    assert preview['tables'] == {table:1 for table in WALLET_TABLES}
    assert sum(preview['files'].values()) == 2
    assert db_snapshot(db) == before
    assert kwargs['generated'].read_bytes() == generated_before
    applied = cleanup(**kwargs,apply=True)
    after = db_snapshot(db)
    for table, rows in before.items():
        if table not in WALLET_TABLES: assert after[table] == rows
        else:
            assert len(after[table]) == 1 and after[table][0] in rows
            assert db.db.execute(f'SELECT wallet FROM {table}').fetchone()[0] == W
    assert db.next_block() == 123
    assert json.loads(kwargs['generated'].read_text()) == {
        'custom':'preserve','wallets':[{'address':W,'label':'real','custom':42}]}
    intel = json.loads(kwargs['intelligence'].read_text())
    assert intel['profiles'] == {W:json.loads(intel_before)['profiles'][W]}
    assert intel['custom'] == 'preserve' and intel['generated_at'] == 42
    backup = Path(applied['backup'])
    assert (backup/'0-generated.json').read_bytes() == generated_before
    assert (backup/'1-intel.json').read_bytes() == intel_before
    with sqlite3.connect(backup/Path(db.path).name) as saved:
        assert saved.execute('SELECT count(*) FROM discovery_candidates').fetchone()[0] == 2
    again = cleanup(**kwargs,apply=True)
    assert sum(again['tables'].values()) == sum(again['files'].values()) == 0
    assert again['backup'] is None and db_snapshot(db) == after


def test_cleanup_refuses_active_scanner(policy, db, tmp_path):
    kwargs = seed_cleanup(db,policy,tmp_path)
    before = db_snapshot(db)
    with ProcessLock(db.path+'.scanner.lock'):
        with pytest.raises(RuntimeError): cleanup(**kwargs,apply=True)
    assert db_snapshot(db) == before


@pytest.mark.parametrize('field', ['generated','intelligence'])
def test_cleanup_malformed_export_does_not_partially_delete(policy, db, tmp_path, field):
    kwargs = seed_cleanup(db,policy,tmp_path)
    kwargs[field].write_text('{ broken')
    before = db_snapshot(db)
    with pytest.raises(ValueError): cleanup(**kwargs,apply=True)
    assert db_snapshot(db) == before


def test_interrupted_export_repair_is_idempotent(policy, db, tmp_path, monkeypatch):
    kwargs = seed_cleanup(db,policy,tmp_path)
    import cleanup_dex_infrastructure as module
    real_write = module.write_json
    monkeypatch.setattr(module,'write_json',Mock(side_effect=OSError('write failed')))
    with pytest.raises(OSError): cleanup(**kwargs,apply=True)
    # The old file is harmless to runtime authority even during this window.
    book = IntelligenceBook(kwargs['intelligence'],0)
    assert not book.effective(INFRA,100,4)['eligible']
    monkeypatch.setattr(module,'write_json',real_write)
    result = cleanup(**kwargs,apply=True)
    assert sum(result['tables'].values()) == 0 and sum(result['files'].values()) == 2
    assert list(json.loads(kwargs['intelligence'].read_text())['profiles']) == [W]


@pytest.mark.parametrize('exists', [False,True])
def test_cleanup_missing_or_empty_registry_cannot_touch_database(policy, db, tmp_path, exists):
    kwargs = seed_cleanup(db,policy,tmp_path)
    path = Path(policy.trusted_dex_registry)
    if exists: path.write_text(json.dumps({'chain_id':5042,'emitters':[]}))
    else: path.unlink()
    before = db_snapshot(db)
    with pytest.raises(ValueError): cleanup(**kwargs,apply=True)
    assert db_snapshot(db) == before
