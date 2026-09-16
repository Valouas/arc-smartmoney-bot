"""No network, no real waiting: HTTP failures through the actual Web3 transport."""
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from hexbytes import HexBytes
from web3 import Web3

from rpc import ArcRPC, USDC_ERC20
from rpc_transport import RPCError, ResilientRPCProvider, SharedRateLimiter, retry_delay
from storage import Storage
from test_audit_regressions import W, T, H
from test_followup import make_bot


class VirtualClock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.waits.append(seconds)
        self.now += seconds


class FakeHTTP:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def post(self, url, *, data, **kwargs):
        request = json.loads(data)
        self.calls.append((url, request['method'], request['params']))
        assert kwargs['allow_redirects'] is False
        if request['method'] == 'eth_chainId':
            result = '0x13b2'
        else:
            result = next(self.results)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, int):
            return Mock(status_code=result, headers={}, close=Mock())
        response = Mock(status_code=200, headers={})
        response.json.return_value = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        return response


def provider(results, **kwargs):
    clock = VirtualClock()
    http = FakeHTTP(results)
    p = ResilientRPCProvider('https://secret:credential@primary.test/private', 5042,
                            Mock(), session=http, sleep=clock.sleep, **kwargs)
    return p, http, clock


@pytest.mark.parametrize('failure,category', [
    (429, '429'), (500, '500'), (502, '502'), (503, '503'), (504, '504'),
    (requests.Timeout('credential body url'), 'timeout'),
    (requests.ConnectionError('credential body url'), 'connection'),
])
def test_transient_then_success(failure, category, caplog):
    p, http, clock = provider([failure, []])
    assert Web3(p).eth.get_logs({'fromBlock': 1, 'toBlock': 1}) == []
    assert [c[1] for c in http.calls] == ['eth_chainId', 'eth_getLogs', 'eth_getLogs']
    assert p.limiter.acquire.call_count == 3
    assert 0.5 <= clock.waits[0] <= 0.75
    assert f'category={category} attempt=1 provider=primary' in caplog.text
    assert not any(s in caplog.text for s in ('credential', 'secret', 'https:', 'body'))


@pytest.mark.parametrize('status', [400, 401, 403, 404, 408, 501, 301])
def test_no_retry_or_fallback_for_other_http_status(status):
    p, http, clock = provider([status], fallback_urls=['https://secondary.test'])
    with pytest.raises(RPCError, match=f'category={status} attempt=1'):
        Web3(p).eth.get_logs({})
    assert len(http.calls) == 2 and not clock.waits


def test_retry_after_seconds_and_date(monkeypatch):
    monkeypatch.setattr('rpc_transport.random.uniform', lambda *a: 0.1)
    monkeypatch.setattr('rpc_transport.time.time', lambda: 100)
    assert retry_delay(1, '5') == 5
    assert retry_delay(2, format_datetime(datetime.fromtimestamp(110, timezone.utc), usegmt=True)) == 10
    assert retry_delay(2, 'bad') == 1.1
    p, http, clock = provider([[]])
    original = http.post
    pending = [True]
    def request(url, **kwargs):
        if pending and json.loads(kwargs['data'])['method'] == 'eth_getLogs':
            pending.pop()
            return Mock(status_code=429, headers={'Retry-After': '5'})
        return original(url, **kwargs)
    http.post = request
    assert Web3(p).eth.get_logs({}) == []
    assert clock.waits == [5]


@pytest.mark.parametrize('result', [None, {}, {'items': []}, [None], [{}]])
def test_empty_logs_only_after_valid_empty_array(result):
    p, http, clock = provider([result])
    with pytest.raises(RPCError, match='invalid_result'):
        Web3(p).eth.get_logs({})
    assert len(http.calls) == 2 and not clock.waits


@pytest.mark.parametrize('envelope', [
    {}, {'jsonrpc': '2.0', 'id': 999, 'result': []},
    {'jsonrpc': '2.0', 'id': 0, 'result': [], 'error': {'code': -32000}},
    {'jsonrpc': '2.0', 'id': 0, 'error': {'code': -32000, 'message': 'credential'}},
])
def test_invalid_envelope_never_retried_or_empty(envelope, caplog):
    p, http, clock = provider([])
    p.verified.add(0)
    http.post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: envelope))
    with pytest.raises(RPCError):
        p.make_request('eth_getLogs', [])
    assert http.post.call_count == 1 and not clock.waits
    assert 'credential' not in caplog.text


def test_failover_requires_exhaustion_and_validates_chain():
    p, http, clock = provider([502, 502, 502, [], []], fallback_urls=['https://secondary.test'])
    assert Web3(p).eth.get_logs({}) == []
    assert [c[1] for c in http.calls] == ['eth_chainId'] + ['eth_getLogs'] * 3 + ['eth_chainId', 'eth_getLogs']
    assert [c[0] for c in http.calls[:4]] == [p.urls[0]] * 4
    assert all(c[0] == p.urls[1] for c in http.calls[4:])
    assert len(clock.waits) == 2 and p.active == 1
    assert Web3(p).eth.get_logs({}) == []
    assert http.calls[-1][0] == p.urls[1]


def test_wrong_chain_secondary_refused(caplog):
    p, http, _ = provider([429] * 3, fallback_urls=['https://secondary.test'])
    original = http.post
    def request(url, **kwargs):
        response = original(url, **kwargs)
        if url == p.urls[1]:
            response.json.return_value['result'] = '0x1'
        return response
    http.post = request
    with pytest.raises(RPCError, match='wrong_chain.*fallback-1'):
        Web3(p).eth.get_logs({})
    assert http.calls[-1][1] == 'eth_chainId'
    assert len(http.calls) == 5 and p.active == 0


def quota_provider(*, fallback=True, wrong_chain=False, retry_after=None):
    p, http, clock = provider([[], []],
        fallback_urls=['https://secondary.test'] if fallback else [])
    p.urls = ('https://arc-mainnet.g.alchemy.com/v2/secret', *p.urls[1:])
    original = http.post
    calls = []
    def request(url, **kwargs):
        body = json.loads(kwargs['data'])
        calls.append((url, body['method'], body['params']))
        if url == p.urls[0]:
            return Mock(status_code=403,
                headers={'Retry-After': retry_after} if retry_after else {},
                json=lambda: {'jsonrpc': '2.0', 'id': body['id'],
                    'error': {'code': -32600, 'message': 'Monthly capacity limit exceeded.'}})
        response = original(url, **kwargs)
        if wrong_chain and body['method'] == 'eth_chainId':
            response.json.return_value['result'] = '0x1'
        return response
    http.post = request
    return p, http, clock, calls


@pytest.mark.parametrize('verified', [False, True])
def test_alchemy_monthly_quota_switches_without_retry(verified, caplog):
    p, _, clock, calls = quota_provider()
    if verified:
        p.verified.add(0)
    params = [{'fromBlock': '0x7', 'toBlock': '0x7'}]
    assert p.make_request('eth_getLogs', params)['result'] == []
    assert [c[1] for c in calls] == [
        'eth_getLogs' if verified else 'eth_chainId', 'eth_chainId', 'eth_getLogs']
    assert calls[-1][2] == params
    assert p.active == 1 and not clock.waits
    assert 'category=monthly_capacity' in caplog.text
    assert 'secret' not in caplog.text and 'https:' not in caplog.text
    p.make_request('eth_getLogs', params)
    assert calls[-1][0] == p.urls[1]
    assert sum(c[0] == p.urls[0] for c in calls) == 1


@pytest.mark.parametrize('fallback,wrong_chain,category', [
    (False, False, 'monthly_capacity'), (True, True, 'wrong_chain'),
])
def test_monthly_quota_without_usable_fallback_raises(fallback, wrong_chain, category):
    p, _, _, calls = quota_provider(fallback=fallback, wrong_chain=wrong_chain)
    with pytest.raises(RPCError, match=category):
        p.make_request('eth_getLogs', [{}])
    assert p.active == 0
    assert not any(c[1] == 'eth_getLogs' for c in calls)


def test_monthly_quota_retry_after_is_respected_before_fallback():
    p, _, clock, _ = quota_provider(retry_after='5')
    assert p.make_request('eth_getLogs', [{}])['result'] == []
    assert clock.waits == [5]
    p.limiter.defer.assert_called_once_with(5)


@pytest.mark.parametrize('variant', [
    'inactive', 'network', 'wrong_code', 'wrong_id', 'partial', 'not_json',
    'wrong_host', 'spoofed_host', 'wrong_status',
])
def test_quota_recognition_does_not_mask_other_refusals(variant):
    p, http, clock = provider([], fallback_urls=['https://secondary.test'])
    p.urls = ('https://arc-mainnet.g.alchemy.com/v2/secret', p.urls[1])
    if variant == 'wrong_host':
        p.urls = ('https://primary.test', p.urls[1])
    if variant == 'spoofed_host':
        p.urls = ('https://arc-mainnet.g.alchemy.com.attacker.test', p.urls[1])
    def request(url, **kwargs):
        req = json.loads(kwargs['data'])
        data = {'jsonrpc': '2.0', 'id': req['id'],
                'error': {'code': -32600, 'message': 'Monthly capacity limit exceeded.'}}
        if variant == 'inactive':
            data['error']['message'] = 'App is inactive.'
        if variant == 'network':
            data['error']['message'] = 'ARC_MAINNET is not enabled for this app.'
        if variant == 'wrong_code':
            data['error']['code'] = -32000
        if variant == 'wrong_id':
            data['id'] += 1
        if variant == 'partial':
            data['result'] = []
        response = Mock(status_code=400 if variant == 'wrong_status' else 403, headers={})
        response.json.return_value = data
        if variant == 'not_json':
            response.json.side_effect = ValueError('secret')
        return response
    http.post = Mock(side_effect=request)
    with pytest.raises(RPCError, match='category=400' if variant == 'wrong_status' else 'category=403'):
        p.make_request('eth_getLogs', [{}])
    assert http.post.call_count == 1 and not clock.waits and p.active == 0


def _child_limiter(path):
    clock = VirtualClock()
    SharedRateLimiter(path, 2, clock=clock.time, sleep=clock.sleep).acquire()
    return clock.now


def test_limiter_shared_between_instances_and_processes(tmp_path):
    path = str(tmp_path / 'budget.sqlite3')
    assert _child_limiter(path) == 100
    # New interpreters, not an in-memory/thread-local budget.
    for _ in range(2):
        with ProcessPoolExecutor(max_workers=1) as pool:
            actual = pool.submit(_child_limiter, path).result(timeout=30)
        assert actual == 100.5 + _ * 0.5


def test_server_cooldown_shared_and_not_shortened(tmp_path):
    clock = VirtualClock()
    path = tmp_path / 'budget.sqlite3'
    first = SharedRateLimiter(path, 2, clock=clock.time, sleep=clock.sleep)
    second = SharedRateLimiter(path, 2, clock=clock.time, sleep=clock.sleep)
    first.acquire()
    first.defer(10)
    second.defer(2)
    second.acquire()
    assert clock.now == 110
    first.acquire()
    assert clock.now == 110.5


def test_retry_after_exhaustion_survives_next_request(tmp_path):
    clock = VirtualClock()
    path = tmp_path / 'budget.sqlite3'
    limiter = SharedRateLimiter(path, 2, clock=clock.time, sleep=clock.sleep)
    http = FakeHTTP([])
    original = http.post
    def request(url, **kwargs):
        if json.loads(kwargs['data'])['method'] == 'eth_getLogs':
            return Mock(status_code=429, headers={'Retry-After': '5'})
        return original(url, **kwargs)
    http.post = request
    p = ResilientRPCProvider('https://primary.test', 5042, limiter, session=http, sleep=clock.sleep)
    with pytest.raises(RPCError):
        Web3(p).eth.get_logs({})
    exhausted_at = clock.now
    # A different worker/request must still wait after the final failed attempt.
    SharedRateLimiter(path, 2, clock=clock.time, sleep=clock.sleep).acquire()
    assert clock.now >= exhausted_at + 5


@pytest.mark.parametrize('rate', [0, -1, float('nan'), float('inf')])
def test_invalid_rate_rejected(tmp_path, rate):
    with pytest.raises(ValueError):
        SharedRateLimiter(tmp_path / 'budget', rate)


@pytest.mark.parametrize('rate', [0, -1, float('nan'), float('inf')])
def test_invalid_fallback_setting_rejected(rate):
    from dataclasses import replace
    from config import SETTINGS
    with pytest.raises(ValueError, match='ARC_RPC_FALLBACK_REQUESTS_PER_SECOND'):
        replace(SETTINGS, rpc_fallback_requests_per_second=rate)


def test_primary_and_fallback_rates_shared_separately(tmp_path):
    clock = VirtualClock()
    primary = SharedRateLimiter(tmp_path / 'primary', 5, clock=clock.time, sleep=clock.sleep)
    fallback = SharedRateLimiter(tmp_path / 'fallback', 2, clock=clock.time, sleep=clock.sleep)
    http = FakeHTTP(['0x1', '0x2', 429, 429, 429, '0x3', '0x4'])
    p = ResilientRPCProvider('https://primary.test', 5042, primary,
        ['https://secondary.test'], session=http, sleep=clock.sleep, fallback_limiter=fallback)
    p.verified.add(0)
    calls = []
    original = http.post
    def request(url, **kwargs):
        calls.append((url, clock.now))
        return original(url, **kwargs)
    http.post = request
    assert p.make_request('eth_blockNumber', [])['result'] == '0x1'
    assert p.make_request('eth_blockNumber', [])['result'] == '0x2'
    assert calls[1][1] - calls[0][1] == pytest.approx(0.2)
    assert p.make_request('eth_blockNumber', [])['result'] == '0x3'
    assert p.active == 1
    assert p.make_request('eth_blockNumber', [])['result'] == '0x4'
    secondary_times = [when for url, when in calls if url == p.urls[1]]
    assert len(secondary_times) == 3  # Includes the fallback's chain check.
    assert all(b-a >= 0.5 for a,b in zip(secondary_times, secondary_times[1:]))
    # Another worker must respect the same fallback budget, while the primary
    # can still make progress with its own budget.
    primary.acquire()
    assert clock.now == secondary_times[-1]
    SharedRateLimiter(tmp_path / 'fallback', 2, clock=clock.time, sleep=clock.sleep).acquire()
    assert clock.now == secondary_times[-1] + 0.5


def test_fallback_cooldown_goes_to_fallback_budget_only():
    fallback = Mock()
    p, http, clock = provider([[]], fallback_urls=['https://secondary.test'], fallback_limiter=fallback)
    p.active = 1
    p.verified.add(1)
    original = http.post
    pending = [True]
    def request(url, **kwargs):
        if pending:
            pending.pop()
            return Mock(status_code=429, headers={'Retry-After': '5'})
        return original(url, **kwargs)
    http.post = request
    assert p.make_request('eth_getLogs', [{}])['result'] == []
    assert fallback.acquire.call_count == 2
    fallback.defer.assert_called_once_with(5)
    p.limiter.acquire.assert_not_called()
    p.limiter.defer.assert_not_called()
    assert clock.waits == [5]


def test_arc_rpc_wires_distinct_shared_budgets(tmp_path, monkeypatch):
    import config
    from dataclasses import replace
    settings = replace(config.SETTINGS, rpc_requests_per_second=5,
        rpc_fallback_requests_per_second=2, rpc_fallback_urls=('https://secondary.test',),
        rpc_rate_limit_file=str(tmp_path / 'rate.sqlite3'),
        trusted_dex_registry=str(tmp_path / 'missing.json'))
    monkeypatch.setattr(config, 'SETTINGS', settings)
    first = ArcRPC('https://primary.test', 5042).w3.provider
    second = ArcRPC('https://primary.test', 5042).w3.provider
    try:
        assert first.limiter.interval == 0.2
        assert first.fallback_limiter.interval == 0.5
        assert first.limiter.path != first.fallback_limiter.path
        assert first.limiter.path == second.limiter.path
        assert first.fallback_limiter.path == second.fallback_limiter.path
    finally:
        first.session.close()
        second.session.close()


def test_all_web3_methods_and_eth_call_share_budget(tmp_path):
    clock = VirtualClock()
    limiter = SharedRateLimiter(tmp_path / 'budget', 2, clock=clock.time, sleep=clock.sleep)
    tx_hash = '0x' + 'a' * 64
    block_hash = '0x' + 'b' * 64
    block = {'number': '0x1', 'hash': block_hash, 'timestamp': '0x64', 'transactions': []}
    receipt = {'blockNumber': '0x1', 'blockHash': block_hash, 'transactionHash': tx_hash, 'status': '0x1', 'logs': []}
    http = FakeHTTP(['0x1', block, [], receipt, '0x1234', '0x' + '0' * 63 + '6'])
    p = ResilientRPCProvider('https://primary.test', 5042, limiter, session=http, sleep=clock.sleep)
    w3 = Web3(p)
    assert w3.eth.block_number == 1
    assert w3.eth.get_block(1)['number'] == 1
    assert w3.eth.get_logs({}) == []
    assert w3.eth.get_transaction_receipt(tx_hash)['status'] == 1
    assert bytes(w3.eth.get_code(USDC_ERC20)) == b'\x12\x34'
    from rpc import ERC20_ABI
    assert w3.eth.contract(address=USDC_ERC20, abi=ERC20_ABI).functions.decimals().call() == 6
    assert clock.now == 100 + (len(http.calls) - 1) * 0.5
    assert all(wait == 0.5 for wait in clock.waits)


@pytest.mark.parametrize('field', ['symbol', 'name', 'decimals', 'totalSupply'])
def test_metadata_transport_failure_is_never_cached_or_swallowed(field):
    r = ArcRPC.__new__(ArcRPC)
    functions = Mock()
    functions.symbol.return_value.call.return_value = 'T'
    functions.name.return_value.call.return_value = 'Token'
    functions.decimals.return_value.call.return_value = 6
    functions.totalSupply.return_value.call.return_value = 100
    getattr(functions, field).return_value.call.side_effect = RPCError('eth_call', '429', 3, 'primary')
    r.w3 = SimpleNamespace(eth=SimpleNamespace(contract=lambda **k: SimpleNamespace(functions=functions)))
    for _ in range(2):
        with pytest.raises(RPCError):
            r.token_meta(T)
    assert getattr(functions, field).return_value.call.call_count == 2


def scanner(db, tmp_path, rpc):
    bot = make_bot(db, tmp_path)
    bot.rpc = rpc
    bot.watch = {W: bot.watch[W]}
    return bot


def test_three_failures_no_checkpoint_first_block_restart_no_skip(tmp_path):
    path = tmp_path / 'scanner.sqlite3'
    db = Storage(str(path))
    p, http, _ = provider([502] * 3 + [[], [], [], []])
    rpc = ArcRPC.__new__(ArcRPC)
    rpc.w3 = Web3(p)
    blocks = []
    def get_block(n, **kwargs):
        blocks.append(n)
        return {'timestamp': 100 + n, 'transactions': []}
    rpc.get_block = get_block
    bot = scanner(db, tmp_path, rpc)
    with pytest.raises(RPCError, match='502.*attempt=3'):
        bot.process_block(10)
    assert db.next_block() == 10
    assert db.db.execute("SELECT value FROM scanner_state WHERE key='next_block'").fetchone() is None
    assert db.db.execute('SELECT count(*) FROM wallet_coverage').fetchone()[0] == 0
    db.db.close()
    db = Storage(str(path))
    restarted = scanner(db, tmp_path, rpc)
    restarted.process_block(db.next_block())
    restarted.process_block(db.next_block())
    assert blocks == [10, 10, 11] and db.next_block() == 12
    db.db.close()


def test_partial_block_resume_no_duplicate_events_or_transactions(tmp_path, monkeypatch):
    path = tmp_path / 'scanner.sqlite3'
    db = Storage(str(path))
    db.checkpoint(10)
    second = '0x' + 'f' * 64
    txs = [{'from': W, 'hash': HexBytes(h), 'value': 0} for h in (H, second)]
    p, http, _ = provider([502] * 3 + [[]])
    def receipt(h):
        if Web3.to_hex(h) == second:
            p.make_request('eth_getLogs', [])  # Real bounded transport failure mid-block.
        return {'status': 1, 'logs': []}
    rpc = SimpleNamespace(get_block=lambda *a, **k: {'timestamp': 100, 'transactions': txs},
                          watched_participants=lambda *a: {}, get_receipt=receipt)
    monkeypatch.setattr('bot.classify_trade', lambda *a, **k: [dict(side='BUY', token=T, symbol='T',
        token_amount=1, usdc_amount=100, confirmation_eligible=True)])
    bot = scanner(db, tmp_path, rpc)
    with pytest.raises(RPCError):
        bot.process_block(10)
    assert db.next_block() == 10 and db.tx_seen(H) and not db.tx_seen(second)
    assert db.db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
    db.db.close()
    db = Storage(str(path))
    restarted = scanner(db, tmp_path, rpc)
    restarted.process_block(db.next_block())
    restarted.process_block(10)  # Replaying a completed block remains idempotent.
    assert db.next_block() == 11
    for table in ('events', 'seen_txs', 'processed_events'):
        assert db.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 2
    db.db.close()


def test_half_of_participant_queries_is_not_success(tmp_path):
    db = Storage(str(tmp_path / 'scanner.sqlite3'))
    db.checkpoint(10)
    p, http, _ = provider([[], 429, 429, 429])
    rpc = ArcRPC.__new__(ArcRPC)
    rpc.w3 = Web3(p)
    rpc.get_block = lambda *a, **k: {'timestamp': 100, 'transactions': []}
    with pytest.raises(RPCError):
        scanner(db, tmp_path, rpc).process_block(10)
    assert db.next_block() == 10
    assert db.db.execute("SELECT value FROM scanner_state WHERE key='next_block'").fetchone()[0] == 10
    assert len(http.calls) == 5
    db.db.close()


def valid_log():
    return dict(address=T, transactionHash=H, blockHash='0x' + 'b' * 64,
                blockNumber='0xa', logIndex='0x0', transactionIndex='0x0',
                removed=False, data='0x' + '0' * 63 + '1', topics=[
                    Web3.to_hex(Web3.keccak(text='Transfer(address,address,uint256)')),
                    '0x' + '0' * 24 + W[2:], '0x' + '0' * 24 + T[2:]])


def test_failover_between_participant_queries_preserves_first_result():
    row = valid_log()
    p, http, _ = provider([[row], 429, 429, 429, []], fallback_urls=['https://secondary.test'])
    rpc = ArcRPC.__new__(ArcRPC)
    rpc.w3 = Web3(p)
    result = rpc.watched_participants(10, [W], HexBytes(row['blockHash']))
    assert result == {H.lower(): {W.lower()}}
    assert p.active == 1
    queries = [params[0] for _, method, params in http.calls if method == 'eth_getLogs']
    assert all(query['blockHash'] == row['blockHash'] and 'fromBlock' not in query for query in queries)
    assert queries[1:] == [queries[1]] * 4


@pytest.mark.parametrize('corruption', ['wrong_hash', 'wrong_number', 'partial_tail', 'removed'])
def test_log_response_mismatch_and_partial_response_are_rejected(corruption):
    row = valid_log()
    query = {'fromBlock': '0xa', 'toBlock': '0xa'}
    result = [row]
    if corruption == 'wrong_hash':
        query = {'blockHash': '0x' + 'f' * 64}
    elif corruption == 'wrong_number':
        row['blockNumber'] = '0xb'
    elif corruption == 'partial_tail':
        result.append({})
    else:
        row['removed'] = True
    p, http, clock = provider([result], fallback_urls=['https://secondary.test'])
    with pytest.raises(RPCError):
        p.make_request('eth_getLogs', [query])
    assert len(http.calls) == 2 and not clock.waits and p.active == 0


def test_all_providers_exhausted_no_empty_result():
    p, http, clock = provider([503] * 6, fallback_urls=['https://secondary.test'])
    with pytest.raises(RPCError, match='503.*attempt=3.*fallback-1'):
        Web3(p).eth.get_logs({})
    assert len(http.calls) == 8 and len(clock.waits) == 4


def test_retry_after_honored_before_fallback():
    p, http, clock = provider([[]], fallback_urls=['https://secondary.test'])
    original = http.post
    def request(url, **kwargs):
        if url == p.urls[0] and json.loads(kwargs['data'])['method'] == 'eth_getLogs':
            return Mock(status_code=429, headers={'Retry-After': '7'})
        return original(url, **kwargs)
    http.post = request
    assert Web3(p).eth.get_logs({}) == []
    assert clock.waits == [7, 7, 7]


def test_invalid_json_fails_without_retry(caplog):
    p, http, clock = provider([])
    p.verified.add(0)
    response = Mock(status_code=200, headers={})
    response.json.side_effect = ValueError('credential response body')
    http.post = Mock(return_value=response)
    with pytest.raises(RPCError, match='invalid_json'):
        p.make_request('eth_getLogs', [{}])
    assert http.post.call_count == 1 and not clock.waits
    assert 'credential' not in caplog.text


def test_read_only_method_allowlist():
    p, http, _ = provider([])
    with pytest.raises(RPCError, match='read_only'):
        p.make_request('eth_sendRawTransaction', ['sensitive signed bytes'])
    assert not http.calls


def test_receipt_from_different_block_prevents_checkpoint(tmp_path):
    db = Storage(str(tmp_path / 'scanner.sqlite3'))
    db.checkpoint(10)
    row = valid_log()
    p, http, _ = provider([])
    rpc = SimpleNamespace(w3=Web3(p), watched_participants=lambda *a: {},
        get_block=lambda *a, **k: {'hash': HexBytes(row['blockHash']), 'timestamp': 100,
                                  'transactions': [{'hash': HexBytes(H), 'from': W}]},
        get_receipt=lambda *a: {'blockHash': HexBytes('0x' + 'f' * 64), 'status': 1, 'logs': []})
    with pytest.raises(RPCError, match='block_mismatch'):
        scanner(db, tmp_path, rpc).process_block(10)
    assert db.next_block() == 10 and not db.tx_seen(H)
    db.db.close()


def test_failed_rpc_metadata_during_decode_prevents_checkpoint(tmp_path, monkeypatch):
    db = Storage(str(tmp_path / 'scanner.sqlite3'))
    db.checkpoint(10)
    rpc = SimpleNamespace(watched_participants=lambda *a: {},
        get_block=lambda *a, **k: {'timestamp': 100, 'transactions': [{'hash': HexBytes(H), 'from': W}]},
        get_receipt=lambda *a: {'status': 1, 'logs': []})
    monkeypatch.setattr('bot.classify_trade', Mock(side_effect=RPCError('eth_call', '429', 3, 'primary')))
    with pytest.raises(RPCError, match='eth_call'):
        scanner(db, tmp_path, rpc).process_block(10)
    assert db.next_block() == 10 and not db.tx_seen(H)
    db.db.close()


def test_status_distinguishes_pending_from_checkpoint(tmp_path, monkeypatch, capsys):
    import runpy
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'status.py'
    monkeypatch.chdir(tmp_path)
    db = Storage()
    db.checkpoint(10)
    db.begin_block(10)
    db.db.close()
    runpy.run_path(str(script), run_name='__main__')
    output = capsys.readouterr().out
    assert 'Scanner checkpoint next_block: 10' in output
    assert 'Scanner pending block (not completed): 10' in output
