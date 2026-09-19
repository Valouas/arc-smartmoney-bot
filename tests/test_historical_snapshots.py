import json
import sqlite3
from dataclasses import replace
from unittest.mock import Mock
from pathlib import Path

import pytest

from arcscan import ArcscanError
from historical_collector import HistoricalCollector
from historical_report import build_report
from historical_store import initialize
from historical_snapshots import preserve_completed
from dashboard import analysis_snapshot
from test_historical_analysis import cfg, store, fake_collector, page, trade, W, NOW


HASHES = ['0x'+f'{i:064x}' for i in (1,2,3)]


def completed(store, cfg):
    cfg = replace(cfg, historical_transactions_per_step=50, historical_rpc_workers=1,
                  historical_refresh_seconds=10)
    c = fake_collector(store, cfg)
    c.arc.address_activity.return_value = page(HASHES[:2])
    c.step(W, NOW)
    payloads = {HASHES[0]:trade(1,'BUY',10), HASHES[1]:trade(2,'SELL',20), HASHES[2]:trade(3,'BUY',2)}
    c.transaction = Mock(side_effect=lambda wallet,h,run: payloads[h])
    c.identity = Mock(return_value={})
    c.step(W, NOW+1)
    return c, payloads


def saved(store):
    return [tuple(r) for r in store.db.execute('SELECT tx_hash,payload FROM historical_snapshot_transactions ORDER BY tx_hash')]


def test_refresh_preserves_metrics_and_reuses_old_reads_without_duplicates(store, cfg):
    c, payloads = completed(store,cfg)
    baseline = build_report(store.path,c.cfg,NOW+2)[0]
    c.transaction.reset_mock()
    c.arc.address_activity.return_value = page(HASHES[1:], 'next')
    c.step(W,NOW+20)
    retained = build_report(store.path,c.cfg,NOW+20)[0]
    assert retained['using_retained_snapshot'] is True
    assert retained['conservative_pnl_usdc'] == baseline['conservative_pnl_usdc']
    assert retained['completed_at'] == NOW+1 and retained['snapshot_at'] == baseline['snapshot_at']
    assert retained['refresh_phase'] == 'FETCHING'
    assert retained['transactions_pending'] == 0
    assert retained['working_transactions_pending'] == 1
    counts = analysis_snapshot(Path(store.path))['transactions']
    assert counts['queued'] == 3 and counts['decoded'] == 2  # Union by wallet + hash.
    assert counts['working_decoded'] == 1
    c.arc.address_activity.return_value = page([HASHES[0]])
    c.step(W,NOW+21)
    c.step(W,NOW+22)
    assert [call.args[1] for call in c.transaction.call_args_list] == HASHES[2:]
    assert len(saved(store)) == 3
    latest = build_report(store.path,c.cfg,NOW+22)[0]
    assert latest['using_retained_snapshot'] is False
    assert latest['conservative_pnl_usdc'] == 8
    assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0] == 0


def test_transport_failure_and_restart_keep_last_snapshot_and_expiry(store,cfg):
    c,_ = completed(store,cfg)
    previous = saved(store)
    c.arc.address_activity.return_value = page([HASHES[2]],'next')
    c.step(W,NOW+20)
    c.arc.address_activity.side_effect = ArcscanError('offline')
    with pytest.raises(ArcscanError): c.step(W,NOW+21)
    restarted = HistoricalCollector(store,c.rpc,c.arc,c.cfg)
    assert saved(store) == previous
    for at in (NOW+22,NOW+500,NOW+200000):
        row = build_report(store.path,c.cfg,at)[0]
        assert row['using_retained_snapshot'] is True
        assert row['conservative_pnl_usdc'] == 10
        assert row['expires_at'] == NOW+11
        assert row['paper_selection_eligible'] is False
        assert row['paper_review_eligible'] is False
        assert 'retained_selection_expired' in row['blocking_reasons']
    assert restarted.db.execute('SELECT cursor FROM historical_runs').fetchone()[0] == 'next'


def test_completed_snapshot_swap_rolls_back_atomically_on_database_error(store,cfg):
    c,_ = completed(store,cfg)
    previous = saved(store)
    c.arc.address_activity.return_value = page(HASHES)
    c.step(W,NOW+20)
    store.db.execute("CREATE TRIGGER deny_snapshot BEFORE INSERT ON historical_snapshots BEGIN SELECT RAISE(ABORT,'test'); END")
    store.db.commit()
    with pytest.raises(sqlite3.IntegrityError): c.step(W,NOW+21)
    assert saved(store) == previous
    assert store.db.execute('SELECT phase FROM historical_runs').fetchone()[0] == 'DECODING'
    store.db.execute('DROP TRIGGER deny_snapshot');store.db.commit()
    c.step(W,NOW+22)
    assert len(saved(store)) == 3


def test_migration_keeps_legacy_completed_evidence_but_does_not_assume_decode_context(store,cfg):
    c,_ = completed(store,cfg)
    previous = saved(store)
    store.db.execute('DELETE FROM historical_snapshots')
    store.db.execute('DELETE FROM historical_snapshot_transactions')
    store.db.execute('DELETE FROM historical_refresh_context');store.db.commit()
    initialize(store.db);initialize(store.db)
    assert saved(store) == previous
    assert store.db.execute('SELECT context FROM historical_snapshots').fetchone()[0] is None
    c.transaction.reset_mock()
    c.step(W,NOW+20);c.step(W,NOW+21)
    assert c.transaction.call_count == 2  # One safe rebuild before future cache reuse.


def test_changed_registry_forces_redecode(store,cfg,tmp_path):
    c,_ = completed(store,cfg)
    from pathlib import Path
    Path(c.cfg.trusted_dex_registry).write_text('{"chain_id":5042,"emitters":[],"review_changed":true}')
    c.transaction.reset_mock()
    c.step(W,NOW+20);c.step(W,NOW+21)
    assert c.transaction.call_count == 2


def test_reorg_invalidates_retained_authority_and_reuse(store,cfg):
    c,_ = completed(store,cfg)
    c.rpc.get_block = lambda *a: {'hash':b'b'*32,'timestamp':NOW}
    with pytest.raises(ArcscanError,match='reorganization'): c.step(W,NOW+20)
    assert store.db.execute('SELECT invalidated FROM historical_snapshots').fetchone()[0] == 1
    row = build_report(store.path,c.cfg,NOW+21)[0]
    assert row['using_retained_snapshot'] is False
    assert row['paper_selection_eligible'] is False
    assert row['metrics_status'] == 'UNAVAILABLE'


def test_refresh_cannot_omit_a_previously_known_transaction(store,cfg):
    c,_ = completed(store,cfg)
    previous = saved(store)
    c.arc.address_activity.return_value = page([HASHES[0]],'next')
    c.step(W,NOW+20)
    c.arc.address_activity.return_value = page([HASHES[2]])
    with pytest.raises(ArcscanError,match='omitted known'): c.step(W,NOW+21)
    assert saved(store) == previous
    assert store.db.execute('SELECT version FROM historical_runs').fetchone()[0] == 0
    assert build_report(store.path,c.cfg,NOW+21)[0]['using_retained_snapshot'] is True


def test_failed_refresh_does_not_overwrite_good_snapshot_error_metadata(store,cfg):
    c,_ = completed(store,cfg)
    previous = store.db.execute('SELECT run FROM historical_snapshots').fetchone()[0]
    store.db.execute("UPDATE historical_runs SET error='offline',last_attempt=?",(NOW+20,));store.db.commit()
    with store.db: preserve_completed(store.db,W)
    assert store.db.execute('SELECT run FROM historical_snapshots').fetchone()[0] == previous


@pytest.mark.parametrize('defect', [{'error':'ambiguous'}, {'outside_snapshot':True},
                                  {'gas_usdc':None}, {'inventory_gap':True}])
def test_ambiguous_or_outside_payloads_are_revisited(store,cfg,defect):
    c,_ = completed(store,cfg)
    row=json.loads(store.db.execute('SELECT payload FROM historical_snapshot_transactions WHERE tx_hash=?',(HASHES[0],)).fetchone()[0]);row.update(defect)
    store.db.execute('UPDATE historical_snapshot_transactions SET payload=? WHERE tx_hash=?',(json.dumps(row),HASHES[0]));store.db.commit()
    c.transaction.reset_mock();c.step(W,NOW+20);c.step(W,NOW+21)
    assert [call.args[1] for call in c.transaction.call_args_list] == HASHES[:1]
