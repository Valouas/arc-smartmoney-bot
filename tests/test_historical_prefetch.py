import json
import threading
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arcscan import ArcscanError
from config import Settings
from historical_prefetch import ordered_reads
from historical_metrics import analyze
from rpc_transport import RPCError
from test_historical_analysis import cfg, store, fake_collector, page, W, NOW


HASHES = ['0x'+f'{n:064x}' for n in range(1, 4)]


def factory(read_hook=lambda h: None, clients=None):
    clients = [] if clients is None else clients

    def create():
        owner = threading.get_ident()
        session = SimpleNamespace(close=Mock())

        def tx(h):
            assert threading.get_ident() == owner
            read_hook(h)
            return {'blockNumber': int(h, 16), 'blockHash': b'a'*32, 'from': W}

        rpc = SimpleNamespace(
            w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=tx), provider=SimpleNamespace(session=session)),
            get_receipt=lambda h: {'blockHash': b'a'*32, 'blockNumber': int(h, 16),
                'transactionIndex': 0, 'status': 0, 'gasUsed': 21000, 'effectiveGasPrice': 10**9},
            get_block=lambda *a: {'hash': b'a'*32, 'timestamp': NOW})
        clients.append(rpc)
        return rpc
    return create


def test_parallel_reads_finish_out_of_order_but_yield_in_order_and_close():
    barrier = threading.Barrier(3)
    later_done = threading.Event()
    completed = []
    clients = []

    def hook(h):
        barrier.wait(timeout=5)  # Fails if calls are actually sequential.
        if h != HASHES[-1]:
            assert later_done.wait(5)
        completed.append(h)
        if h == HASHES[-1]: later_done.set()

    rows = list(ordered_reads(HASHES, 100, 3, factory(hook, clients)))
    assert completed[0] == HASHES[-1]
    assert [h for h, _ in rows] == HASHES
    assert len(clients) == 3
    assert all(c.w3.provider.session.close.call_count == 1 for c in clients)


def test_consumer_failure_cancels_bounded_prefetch_and_closes():
    calls, clients = [], []
    with pytest.raises(ValueError):
        with closing(ordered_reads(HASHES*100, 100, 2, factory(calls.append, clients))) as rows:
            next(rows)
            raise ValueError('decoder failure')
    assert len(calls) <= 2
    assert all(c.w3.provider.session.close.call_count == 1 for c in clients)


@pytest.mark.parametrize('failure', [ArcscanError('coverage'),
    RPCError('eth_getTransactionReceipt', '429', 3, 'primary')])
def test_failed_read_keeps_ordered_prefix_and_resume_no_loss_or_duplicates(store, cfg, failure):
    cfg = replace(cfg, historical_transactions_per_step=3, historical_rpc_workers=3)
    c = fake_collector(store, cfg)
    c.arc.address_activity.return_value = page(HASHES)
    c.step(W, NOW)
    calls, clients = [], []

    def hook(h):
        calls.append(h)
        if h == HASHES[1]: raise failure

    c.rpc_factory = factory(hook, clients)
    with pytest.raises(type(failure)): c.step(W, NOW+1)
    rows = store.db.execute('SELECT tx_hash,payload FROM historical_transactions ORDER BY tx_hash').fetchall()
    assert [r[0] for r in rows if r[1] is not None] == HASHES[:1]
    assert store.db.execute('SELECT phase FROM historical_runs').fetchone()[0] == 'DECODING'
    assert all(x.w3.provider.session.close.call_count == 1 for x in clients)

    # A new collector simulates a process restart, retaining only committed work.
    resumed = fake_collector(store, cfg)
    retry_calls = []
    resumed.rpc_factory = factory(retry_calls.append)
    resumed.identity = Mock(return_value={})
    resumed.step(W, NOW+2)
    assert sorted(retry_calls) == HASHES[1:]
    assert store.db.execute('SELECT phase FROM historical_runs').fetchone()[0] == 'COMPLETE'
    rows = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM historical_transactions ORDER BY tx_hash')]
    serial = fake_collector(store, cfg)
    serial.rpc = factory()()
    expected = [serial.transaction(W, h, {'cutoff_block': 100}) for h in HASHES]
    assert rows == expected
    assert analyze(rows, cfg) == analyze(expected, cfg)
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0] == 0


def test_parallel_reads_preserve_reorganization_guard(store, cfg):
    cfg = replace(cfg, historical_transactions_per_step=3, historical_rpc_workers=3)
    c = fake_collector(store, cfg)
    c.arc.address_activity.return_value = page(HASHES)
    c.step(W, NOW)
    c.rpc_factory = factory()
    c.rpc.get_block = lambda *a: {'hash': b'b'*32, 'timestamp': NOW}
    with pytest.raises(ArcscanError, match='reorganization'): c.step(W, NOW+1)
    run = store.db.execute('SELECT phase,version FROM historical_runs').fetchone()
    assert run['phase'] == 'DECODING' and run['version'] == 0


def test_parallel_swap_decode_matches_serial_and_keeps_writes_on_main_thread(store, cfg, monkeypatch):
    from test_historical_analysis import T
    cfg = replace(cfg, historical_transactions_per_step=3, historical_rpc_workers=3)
    c = fake_collector(store, cfg)
    c.arc.address_activity.return_value = page(HASHES)
    c.step(W, NOW)
    owner = threading.get_ident()
    decoder_threads = []
    def create():
        rpc = factory()()
        old = rpc.get_receipt
        rpc.get_receipt = lambda h: {**old(h), 'status': 1}
        return rpc
    def classify(rpc, receipt, *args, **kwargs):
        decoder_threads.append(threading.get_ident())
        # This RPC asserts that the decoder uses its thread's own connection.
        rpc.w3.eth.get_transaction(HASHES[0])
        n = receipt['blockNumber']
        return [{'token': T, 'side': 'SELL' if n == 2 else 'BUY',
                 'token_amount': 1, 'usdc_amount': 20 if n == 2 else 10,
                 'confirmation_eligible': True}]
    monkeypatch.setattr('historical_collector.classify_trade', classify)
    monkeypatch.setattr('historical_collector.wallet_token_deltas', lambda *args: {})
    c.rpc_factory = create
    c.identity = Mock(return_value={})
    writes = []
    store.db.set_trace_callback(lambda sql: writes.append((threading.get_ident(), sql)))
    c.step(W, NOW+1)
    assert decoder_threads and all(t != owner for t in decoder_threads)
    assert all(t == owner for t, _ in writes)
    rows = [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM historical_transactions ORDER BY tx_hash')]
    serial_rpc = create()
    expected = [c.transaction(W, h, {'cutoff_block': 100}, rpc=serial_rpc) for h in HASHES]
    assert rows == expected
    assert analyze(rows, cfg) == analyze(expected, cfg)
    assert analyze(rows, cfg)['closed_positions'] == 1


def test_decoder_rpc_failure_does_not_commit_later_results(store, cfg, monkeypatch):
    cfg = replace(cfg, historical_transactions_per_step=3, historical_rpc_workers=3)
    c = fake_collector(store, cfg)
    c.arc.address_activity.return_value = page(HASHES)
    c.step(W, NOW)
    c.rpc_factory = factory()
    original = c.transaction
    def decode(wallet, h, *args, **kwargs):
        if h == HASHES[1]: raise RPCError('eth_call', 'timeout', 3, 'primary')
        return original(wallet, h, *args, **kwargs)
    monkeypatch.setattr(c, 'transaction', decode)
    with pytest.raises(RPCError): c.step(W, NOW+1)
    assert store.db.execute('SELECT COUNT(*) FROM historical_transactions WHERE payload IS NOT NULL').fetchone()[0] == 1
    assert store.db.execute('SELECT phase FROM historical_runs').fetchone()[0] == 'DECODING'


def test_outside_snapshot_does_not_fetch_receipt_or_block():
    def create():
        client = factory()()
        client.get_receipt = Mock(side_effect=AssertionError('outside snapshot'))
        client.get_block = Mock(side_effect=AssertionError('outside snapshot'))
        return client
    rows = list(ordered_reads([HASHES[0]], 0, 1, create))
    assert rows[0][1][1:] == (None, None)


@pytest.mark.parametrize('workers', [0, 17])
def test_concurrency_setting_bounded(workers):
    with pytest.raises(ValueError, match='HISTORICAL_RPC_WORKERS'):
        replace(Settings(), historical_rpc_workers=workers)


@pytest.mark.parametrize('seconds', [0, float('nan'), float('inf')])
def test_history_poll_setting_bounded(seconds):
    with pytest.raises(ValueError, match='HISTORICAL_POLL_SECONDS'):
        replace(Settings(), historical_poll_seconds=seconds)
