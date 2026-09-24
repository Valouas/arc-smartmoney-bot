import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arcscan import ArcscanError
from historical_priority import PriorityCollector
from historical_snapshots import decoding_context
from storage import Storage
from test_historical_analysis import cfg, store, fake_collector, page, trade, W, W2, T, H, NOW
from test_historical_snapshots import completed, saved, HASHES


def setup(store, cfg, monkeypatch, budget=8):
    monkeypatch.setattr('historical_priority.time.time', lambda: NOW)
    c = fake_collector(store, replace(cfg, discovery_max_wallets_per_cycle=budget,
                                    historical_focus_steps=4))
    return c, PriorityCollector(c)


def candidate(store, wallet, sources=1, rank=10):
    store.upsert_discovery_candidate(wallet, T, rank)
    with store.db:
        store.db.execute('UPDATE discovery_candidates SET source_winners=?,best_early_rank=? WHERE wallet=?',
                         (sources, rank, wallet))


def test_focused_candidate_finishes_in_one_cycle_without_new_authority(store, cfg, monkeypatch):
    candidate(store, W2, sources=5, rank=1)
    c, scheduler = setup(store, cfg, monkeypatch, budget=3)
    c.transaction = Mock(return_value=trade(1, 'BUY', 100))
    c.identity = Mock(return_value={})
    result = scheduler.cycle()
    # Start -> decode/complete the priority wallet before starting the other.
    assert c.arc.address_activity.call_args_list[0].args[0] == W2
    assert store.db.execute('SELECT phase FROM historical_runs WHERE wallet=?', (W2,)).fetchone()[0] == 'COMPLETE'
    assert result['wallet_steps'] == 3
    assert store.db.execute("SELECT count(*) FROM discovery_candidates WHERE status='QUARANTINE'").fetchone()[0] == 2
    for table in ('events', 'promoted_wallets', 'generated_watchlist'):
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            assert store.db.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM scanner_state').fetchone()[0] == 0


def test_focus_and_fairness_persist_across_single_step_restarts(store, cfg, monkeypatch):
    wallets = [W, W2] + ['0x'+f'{i:040x}' for i in range(10,16)]
    for wallet in wallets: candidate(store, wallet)
    candidate(store, W2, sources=9, rank=1)
    c, scheduler = setup(store, cfg, monkeypatch, budget=1)
    c.step = Mock()
    for _ in range(32):
        PriorityCollector(c).cycle()
    calls = [x.args[0] for x in c.step.call_args_list]
    assert calls[:3] == [W2]*3
    assert calls[3] != W2  # Fourth persisted slot guarantees an older-served wallet.
    assert set(calls) == set(wallets)
    assert store.db.execute('SELECT sequence FROM historical_priority_state').fetchone()[0] == 32


def test_bursts_rotate_between_wallets_even_if_one_never_finishes(store, cfg, monkeypatch):
    candidate(store, W2, sources=9, rank=1)
    c, scheduler = setup(store, cfg, monkeypatch, budget=16)
    c.step = Mock()
    scheduler.cycle()
    calls = [x.args[0] for x in c.step.call_args_list]
    assert calls[:3] == [W2]*3
    assert calls[5:7] == [W]*2  # Next priority round rotates, despite W2's higher rank.


def test_failure_cooldown_preserves_cursor_and_serves_other_candidates(store, cfg, monkeypatch):
    candidate(store, W, sources=9, rank=1)
    candidate(store, W2)
    c, scheduler = setup(store, cfg, monkeypatch)
    c.arc.address_activity.return_value = page([H], 'next')
    c.step(W, NOW-1)
    original = c.step
    def step(wallet, now):
        if wallet == W: raise ArcscanError('provider secret')
        return original(wallet, now)
    c.step = Mock(side_effect=step)
    # W2 also fails on the cyclic next cursor; neither may monopolize a cycle.
    first = scheduler.cycle()
    assert first['incomplete_steps'] == 2
    assert store.db.execute('SELECT cursor FROM historical_runs WHERE wallet=?', (W,)).fetchone()[0] == 'next'
    count = c.step.call_count
    scheduler.cycle()
    assert c.step.call_count == count
    monkeypatch.setattr('historical_priority.time.time', lambda: NOW+c.cfg.historical_retry_seconds)
    scheduler.cycle()
    assert c.step.call_count > count
    assert 'secret' not in str([tuple(r) for r in store.db.execute('SELECT error FROM historical_runs')])


def test_infrastructure_excluded_contract_account_stays_eligible(store, cfg, monkeypatch):
    candidate(store, W2)
    monkeypatch.setattr('historical_priority.is_dex_infrastructure', lambda w,c:w == W)
    c, scheduler = setup(store, cfg, monkeypatch, budget=1)
    c.step = Mock()
    scheduler.cycle()
    c.step.assert_called_once_with(W2, NOW)


@pytest.mark.parametrize('payload,delayed', [
    ({'timestamp': 10}, True),
    ({'timestamp': NOW-5}, False),
    ({'timestamp': None}, False),
    ({'timestamp': '10'}, False),
    ({'timestamp': NOW+1}, False),
    ({'outside_snapshot': True}, False),
])
def test_inactive_refresh_deferral_is_bounded_and_unknown_stays_due(store, cfg, monkeypatch, payload, delayed):
    # Isolate timestamp scheduling: these deliberately partial payloads are not
    # trade/report fixtures, and this wallet has no paper priority.
    monkeypatch.setattr('historical_priority.build_report', lambda *args: [])
    c, _ = completed(store, cfg)
    # Fixture NOW must be > inactivity window for the old timestamp.
    c.cfg = replace(c.cfg, historical_inactive_days=1, historical_inactive_refresh_seconds=86400,
                    historical_max_age_hours=1, discovery_max_wallets_per_cycle=1)
    with store.db:
        store.db.execute('UPDATE historical_transactions SET payload=?', (json.dumps(payload),))
    scheduler = PriorityCollector(c)
    due, deferred = scheduler.candidates(NOW+20)
    assert (W not in due) is delayed
    assert deferred == int(delayed)
    assert W in scheduler.candidates(NOW+1802)[0]  # Half freshness horizon cap, not 24h.


def test_no_new_rpc_reads_for_cached_valid_transactions_and_fingerprint_unchanged(store, cfg, monkeypatch):
    c, payloads = completed(store, cfg)
    before, context = saved(store), decoding_context(c.cfg)
    c.cfg = replace(c.cfg, discovery_max_wallets_per_cycle=4)
    scheduler = PriorityCollector(c)
    assert saved(store) == before and decoding_context(c.cfg) == context
    monkeypatch.setattr('historical_priority.time.time', lambda: NOW+20)
    c.arc.address_activity.return_value = page(HASHES)
    c.transaction.reset_mock()
    scheduler.cycle()
    assert [call.args[1] for call in c.transaction.call_args_list] == HASHES[2:]
    assert len(saved(store)) == 3
    assert decoding_context(c.cfg) == context


@pytest.mark.parametrize('field,value', [('historical_focus_steps',0),('historical_focus_steps',17),
    ('historical_inactive_days',0),('historical_inactive_refresh_seconds',0),('historical_retry_seconds',0)])
def test_invalid_scheduler_config(cfg, field, value):
    with pytest.raises(ValueError, match='scheduling'):
        replace(cfg, **{field:value})


def test_equal_step_budget_finishes_priority_wallet_sooner(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr('historical_priority.time.time', lambda: NOW)
    counts = []
    for mode in ('legacy', 'focused'):
        store = Storage(tmp_path/(mode+'.sqlite3'))
        try:
            for n in range(1,13): candidate(store, '0x'+f'{n:040x}', sources=13-n, rank=n)
            settings = replace(cfg, discovery_max_wallets_per_cycle=10, historical_rpc_workers=1,
                               historical_transactions_per_step=1, historical_focus_steps=4)
            c = fake_collector(store, settings)
            c.arc.address_activity.return_value = page(HASHES)
            c.transaction = Mock(side_effect=lambda w,h,r:trade(int(h,16),'BUY',10))
            c.identity = Mock(return_value={})
            worker = c if mode == 'legacy' else PriorityCollector(c)
            result = worker.cycle()
            assert result['wallet_steps'] == 10
            counts.append(store.db.execute("SELECT count(*) FROM historical_runs WHERE phase='COMPLETE'").fetchone()[0])
        finally: store.db.close()
    assert counts[0] == 0 and counts[1] >= 1


def test_expired_paper_only_blocker_prioritized_but_fair_slots_remain(store,cfg,monkeypatch):
    candidate(store,W2,sources=99,rank=1)
    monkeypatch.setattr('historical_priority.build_report',lambda *a:[
        {'wallet':W,'paper_review_blockers':['retained_selection_expired']},
        {'wallet':W2,'paper_review_blockers':['insufficient_regularity']}])
    c,scheduler=setup(store,cfg,monkeypatch,budget=4)
    c.step=Mock()
    scheduler.cycle()
    assert [call.args[0] for call in c.step.call_args_list]==[W,W,W,W2]
    assert store.db.execute("SELECT COUNT(*) FROM discovery_candidates WHERE status='QUARANTINE'").fetchone()[0]==2


def test_paper_attention_never_bypasses_transport_cooldown(store,cfg,monkeypatch):
    monkeypatch.setattr('historical_priority.build_report',lambda *a:[
        {'wallet':W,'paper_review_blockers':['historical_snapshot_stale']}])
    c,scheduler=setup(store,cfg,monkeypatch,budget=1)
    scheduler.candidates(NOW)
    with store.db: store.db.execute('UPDATE historical_priority_wallets SET retry_at=?',(NOW+300,))
    c.step=Mock()
    scheduler.cycle()
    c.step.assert_not_called()


def decoding_dossiers(store,cfg,monkeypatch,budget=1):
    candidate(store,W2,sources=99,rank=1)
    c,scheduler=setup(store,cfg,monkeypatch,budget=budget)
    c.cfg=replace(c.cfg,historical_transactions_per_step=10,historical_rpc_workers=1)
    scheduler.cfg=c.cfg
    for wallet,count in ((W,7),(W2,40)):
        c.arc.address_activity.return_value=page(['0x'+f'{i:064x}' for i in range(1,count+1)])
        c.step(wallet,NOW-1)
    c.transaction=Mock(side_effect=lambda w,h,r:trade(int(h,16),'BUY',10))
    c.identity=Mock(return_value={})
    monkeypatch.setattr('historical_priority.build_report',lambda *a:[
        {'wallet':w,'paper_review_blockers':['retained_selection_expired']} for w in (W,W2)])
    scheduler.candidates(NOW)
    with store.db:
        store.db.execute('UPDATE historical_priority_wallets SET rounds=99 WHERE wallet=?',(W,))
    return c,scheduler


def test_finish_near_complete_paper_dossier_before_starting_another_round(store,cfg,monkeypatch):
    c,scheduler=decoding_dossiers(store,cfg,monkeypatch)
    scheduler.cycle()
    assert store.db.execute('SELECT phase FROM historical_runs WHERE wallet=?',(W,)).fetchone()[0]=='COMPLETE'
    assert store.db.execute('SELECT COUNT(payload) FROM historical_transactions WHERE wallet=?',(W2,)).fetchone()[0]==0
    assert c.transaction.call_count==7
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0]==0
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0]==0


def test_fetching_partial_count_is_not_treated_as_a_small_complete_dossier(store,cfg,monkeypatch):
    c,scheduler=decoding_dossiers(store,cfg,monkeypatch)
    with store.db:
        store.db.execute("UPDATE historical_runs SET phase='FETCHING' WHERE wallet=?",(W,))
    c.step=Mock()
    scheduler.cycle()
    c.step.assert_called_once_with(W2,NOW)


def test_near_complete_failure_keeps_evidence_and_allows_other_wallets(store,cfg,monkeypatch):
    c,scheduler=decoding_dossiers(store,cfg,monkeypatch,budget=2)
    original=c.step
    def step(wallet,now):
        if wallet==W: raise ArcscanError('timeout')
        return original(wallet,now)
    c.step=Mock(side_effect=step)
    result=scheduler.cycle()
    assert result['incomplete_steps']==1 and result['wallet_steps']==1
    assert [call.args[0] for call in c.step.call_args_list]==[W,W2]
    assert store.db.execute('SELECT COUNT(*) FROM historical_transactions WHERE wallet=?',(W,)).fetchone()[0]==7
    assert W not in scheduler.candidates(NOW)[0]
