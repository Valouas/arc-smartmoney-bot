"""Deterministic throughput checks: no network, no real sleeps."""
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import sqlite3

import pytest

from config import SETTINGS
from rpc_transport import (
    ALCHEMY_THROUGHPUT_CU, READ_METHODS, ResilientRPCProvider,
    SharedComputeLimiter, SharedRateLimiter,
)
from test_rpc_transport import FakeHTTP, VirtualClock


def limiter(tmp_path, clock, rate=300):
    return SharedComputeLimiter(tmp_path / 'budget', 30, rate,
                                clock=clock.time, sleep=clock.sleep)


def test_every_allowed_method_has_an_explicit_throughput_price():
    assert set(ALCHEMY_THROUGHPUT_CU) == READ_METHODS
    assert ALCHEMY_THROUGHPUT_CU['eth_chainId'] == 5
    assert ALCHEMY_THROUGHPUT_CU['eth_call'] == 26


def test_mixed_workload_exceeds_five_requests_without_exceeding_cu(tmp_path):
    clock = VirtualClock()
    budget = limiter(tmp_path, clock)
    events = []
    methods = ['eth_getLogs', 'eth_getBlockByNumber', 'eth_getTransactionReceipt',
               'eth_call', 'eth_getCode', 'eth_blockNumber', 'eth_chainId'] * 20
    for method in methods:
        budget.acquire(method)
        events.append((clock.now, ALCHEMY_THROUGHPUT_CU[method]))
    assert sum(at < 101 for at, _ in events) > 5
    for end, _ in events:
        assert sum(cost for at, cost in events if end - 1 < at <= end) <= 300
    assert all(b[0]-a[0] >= 1/30-1e-9 for a,b in zip(events, events[1:]))


def test_sixth_get_logs_waits_for_capacity_across_instances(tmp_path):
    clock = VirtualClock()
    first = limiter(tmp_path, clock)
    for _ in range(5):
        first.acquire('eth_getLogs')
    assert clock.now < 101
    limiter(tmp_path, clock).acquire('eth_getLogs')
    assert clock.now >= 101


def test_usage_totals_survive_restart_and_count_only_admitted_requests(tmp_path):
    clock = VirtualClock()
    first = limiter(tmp_path, clock)
    first.acquire('eth_getLogs')
    def totals():
        with sqlite3.connect(first.path) as db:
            return dict((m,(n,c)) for m,n,c in db.execute('SELECT * FROM rpc_usage_totals'))
    def sleep(seconds):
        assert totals() == {'eth_getLogs': (1,60)}
        clock.sleep(seconds)
    first.sleep = sleep
    first.acquire('eth_getLogs')
    limiter(tmp_path, clock).acquire('eth_chainId')
    assert totals() == {'eth_getLogs': (2,120), 'eth_chainId': (1,5)}


def _child_weighted(path):
    clock = VirtualClock()
    budget = SharedComputeLimiter(path, 30, 60, clock=clock.time, sleep=clock.sleep)
    budget.acquire('eth_getLogs')
    return clock.now


def test_cu_ledger_shared_between_processes_and_survives_restart(tmp_path):
    path = str(tmp_path / 'budget')
    assert _child_weighted(path) == 100
    for expected in (101, 102):
        with ProcessPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_child_weighted, path).result(timeout=30) == expected


def test_cooldown_persists_and_no_db_lock_is_held_while_waiting(tmp_path):
    clock = VirtualClock()
    first = limiter(tmp_path, clock)
    second = limiter(tmp_path, clock)
    first.acquire('eth_getLogs')
    first.defer(5)
    def sleep(seconds):
        # A second connection can write while the first caller sleeps.
        second.defer(0)
        clock.sleep(seconds)
    first.sleep = sleep
    first.acquire('eth_blockNumber')
    assert clock.now == 105
    assert clock.waits == [5]


def test_retries_charged_and_fallback_still_uses_public_rate(tmp_path):
    clock = VirtualClock()
    budget = limiter(tmp_path, clock)
    fallback = SharedRateLimiter(tmp_path / 'fallback', 2, clock=clock.time, sleep=clock.sleep)
    http = FakeHTTP([429, 429, 429, [], []])
    p = ResilientRPCProvider('https://primary.test', 5042, budget,
        ['https://secondary.test'], session=http, sleep=clock.sleep, fallback_limiter=fallback)
    costs = []
    acquire = budget.acquire
    def record(method):
        costs.append(ALCHEMY_THROUGHPUT_CU[method])
        acquire(method)
    budget.acquire = record
    assert p.make_request('eth_getLogs', [{}])['result'] == []
    assert costs == [5, 60, 60, 60]
    assert p.active == 1
    now = clock.now
    p.make_request('eth_getLogs', [{}])
    assert clock.now >= now + 0.5
    assert len(costs) == 4


@pytest.mark.parametrize('rate', [-1, 1, 59, float('nan'), float('inf')])
def test_invalid_compute_budget_rejected(tmp_path, rate):
    with pytest.raises(ValueError):
        limiter(tmp_path, VirtualClock(), rate)
    with pytest.raises(ValueError):
        replace(SETTINGS, rpc_compute_units_per_second=rate)


def test_weighted_budget_is_optional(tmp_path, monkeypatch):
    import config
    from rpc import ArcRPC
    cfg = replace(SETTINGS, rpc_compute_units_per_second=0,
                  rpc_rate_limit_file=str(tmp_path / 'rate'), rpc_requests_per_second=2,
                  rpc_fallback_urls=(), trusted_dex_registry=str(tmp_path / 'none.json'))
    monkeypatch.setattr(config, 'SETTINGS', cfg)
    rpc = ArcRPC('https://primary.test', 5042)
    try:
        assert type(rpc.w3.provider.limiter) is SharedRateLimiter
        assert rpc.w3.provider.limiter.interval == 0.5
    finally:
        rpc.w3.provider.session.close()
