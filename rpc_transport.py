"""Read-only JSON-RPC transport with bounded retries and a process-shared budget."""
from __future__ import annotations

import json
from contextlib import closing
import logging
import math
from pathlib import Path
import random
import re
import sqlite3
import time
from urllib.parse import urlsplit
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests
from web3.providers.base import JSONBaseProvider

log = logging.getLogger(__name__)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
# This application never signs, submits transactions or enables node APIs.
READ_METHODS = {
    'eth_chainId', 'eth_blockNumber', 'eth_getBlockByNumber', 'eth_getBlockByHash',
    'eth_getLogs', 'eth_getTransactionReceipt', 'eth_getTransactionByHash',
    'eth_getCode', 'eth_call', 'web3_clientVersion',
}
# Throughput CU, not monthly billing CU (eth_chainId costs 5 for throughput).
# https://www.alchemy.com/docs/reference/compute-unit-costs
ALCHEMY_THROUGHPUT_CU = {
    'eth_chainId': 5, 'eth_blockNumber': 10,
    'eth_getBlockByNumber': 20, 'eth_getBlockByHash': 20,
    'eth_getLogs': 60, 'eth_getTransactionReceipt': 20,
    'eth_getTransactionByHash': 20, 'eth_getCode': 20,
    'eth_call': 26, 'web3_clientVersion': 20,
}


class RPCError(RuntimeError):
    """Safe fields only: never retain response bodies or credential-bearing URLs."""
    def __init__(self, method, category, attempt, provider, retryable=False):
        self.method, self.category = method, category
        self.attempt, self.provider, self.retryable = attempt, provider, retryable
        self.fallback_delay = 0.0
        super().__init__(f'method={method} category={category} attempt={attempt} provider={provider}')


class SharedRateLimiter:
    """Serialize request starts across threads/processes using a separate SQLite file.

    No WAL startup or interaction with the scanner's transactions. A dead worker
    releases the lock automatically. All workers must use the same absolute path
    and rate. Wall time makes the budget persist across process/machine restarts.
    """
    def __init__(self, path, requests_per_second=2, *, clock=time.time, sleep=time.sleep):
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError('RPC requests per second must be finite and positive')
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.interval = 1 / requests_per_second
        self.clock, self.sleep = clock, sleep
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL NOT NULL)')
            db.commit()

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=60)

    def acquire(self):
        while True:
            db = self._connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT next_at FROM budget WHERE id=1').fetchone()
                remaining = (row[0] if row else self.clock()) - self.clock()
                if remaining <= 0:
                    db.execute('INSERT OR REPLACE INTO budget VALUES(1,?)', (self.clock() + self.interval,))
                    db.commit()
                    return
                db.rollback()
            finally:
                db.close()
            # No advance reservation: on waking, recheck under the shared lock.
            # A long Retry-After must not hold a database lock while sleeping.
            self.sleep(remaining)

    def defer(self, seconds):
        """Share server cooldowns, including the last failed attempt, with workers."""
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT next_at FROM budget WHERE id=1').fetchone()
            target = max(row[0] if row else 0, self.clock() + seconds)
            db.execute('INSERT OR REPLACE INTO budget VALUES(1,?)', (target,))
            db.commit()
        finally:
            db.close()


class SharedComputeLimiter(SharedRateLimiter):
    """Atomic request + CU budgets shared by all primary RPC workers.

    A rolling one-second ledger avoids spending an entire ten-second allowance
    in a burst. The inherited budget table retains cooldowns across restarts.
    No reservation or lock is held while sleeping; every wakeup rechecks both.
    """
    def __init__(self, path, requests_per_second, compute_units_per_second, **kwargs):
        if (not math.isfinite(compute_units_per_second)
                or compute_units_per_second < max(ALCHEMY_THROUGHPUT_CU.values())):
            raise ValueError('RPC compute units per second must be finite and at least 60')
        super().__init__(path, requests_per_second, **kwargs)
        self.compute_units_per_second = compute_units_per_second
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cu_usage (at REAL NOT NULL, cost INTEGER NOT NULL)')
            db.commit()

    def acquire(self, method):
        cost = ALCHEMY_THROUGHPUT_CU[method]  # No unpriced method is permitted.
        while True:
            db = self._connect()
            try:
                db.execute('BEGIN IMMEDIATE')
                now = self.clock()
                db.execute('DELETE FROM cu_usage WHERE at <= ?', (now - 1,))
                used, oldest = db.execute('SELECT COALESCE(SUM(cost),0), MIN(at) FROM cu_usage').fetchone()
                row = db.execute('SELECT next_at FROM budget WHERE id=1').fetchone()
                wait = max(0, (row[0] if row else now) - now)
                if used + cost > self.compute_units_per_second:
                    wait = max(wait, oldest + 1 - now, 0.000001)
                if wait <= 0:
                    db.execute('INSERT OR REPLACE INTO budget VALUES(1,?)', (now + self.interval,))
                    db.execute('INSERT INTO cu_usage VALUES(?,?)', (now, cost))
                    db.commit()
                    return
                db.commit()  # Keep pruning bounded even while the caller waits.
            finally:
                db.close()
            self.sleep(wait)


def retry_delay(attempt, retry_after=None):
    delay = 0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.25)
    if isinstance(retry_after, str):
        try:
            value = retry_after.strip()
            if value.isascii() and value.isdigit():
                seconds = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - time.time()
            if math.isfinite(seconds):
                delay = max(delay, seconds)
        except (TypeError, ValueError, OverflowError):
            pass
    return delay


def _hex(value, size=None):
    return (isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]*', value) is not None
            and (len(value) == 2 + size * 2 if size is not None else len(value) % 2 == 0))


def _quantity(value):
    return isinstance(value, str) and re.fullmatch(r'0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)', value) is not None


def _alchemy_monthly_capacity(response, url, request_id):
    """Recognize only Alchemy's documented quota refusal, never generic 403s.

    https://www.alchemy.com/docs/reference/error-reference
    The body is inspected locally and is never retained in errors or logs.
    """
    endpoint = urlsplit(url)
    if (response.status_code != 403 or endpoint.scheme != 'https'
            or not (endpoint.hostname or '').endswith('.g.alchemy.com')):
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    if (not isinstance(data, dict) or data.get('jsonrpc') != '2.0'
            or type(data.get('id')) is not int or data['id'] != request_id
            or 'result' in data or not isinstance(data.get('error'), dict)):
        return False
    error = data['error']
    return (type(error.get('code')) is int and error['code'] == -32600
            and error.get('message') == 'Monthly capacity limit exceeded.')


def _valid_log(row):
    return (isinstance(row, dict) and _hex(row.get('address'), 20)
            and _hex(row.get('transactionHash'), 32) and _hex(row.get('blockHash'), 32)
            and _quantity(row.get('blockNumber')) and _quantity(row.get('logIndex'))
            and _quantity(row.get('transactionIndex')) and row.get('removed', False) is False
            and _hex(row.get('data')) and isinstance(row.get('topics'), list)
            and all(_hex(topic, 32) for topic in row['topics']))


def _valid_result(method, result):
    if result is None:
        return False  # Missing/pruned block, transaction or receipt is not completion.
    if method in {'eth_chainId', 'eth_blockNumber'}:
        return _quantity(result)
    if method in {'eth_getCode', 'eth_call'}:
        return _hex(result)
    if method == 'eth_getLogs':
        return isinstance(result, list) and all(_valid_log(row) for row in result)
    if method in {'eth_getBlockByNumber', 'eth_getBlockByHash'}:
        return (isinstance(result, dict) and _quantity(result.get('number'))
                and _quantity(result.get('timestamp')) and _hex(result.get('hash'), 32)
                and isinstance(result.get('transactions'), list))
    if method == 'eth_getTransactionReceipt':
        return (isinstance(result, dict) and result.get('status') in ('0x0', '0x1')
                and _quantity(result.get('blockNumber')) and _hex(result.get('transactionHash'), 32)
                and _hex(result.get('blockHash'), 32) and isinstance(result.get('logs'), list)
                and all(_valid_log(row) for row in result['logs']))
    if method == 'eth_getTransactionByHash':
        return (isinstance(result, dict) and _hex(result.get('hash'), 32)
                and _quantity(result.get('blockNumber')))
    return isinstance(result, str)


def _matches_request(method, params, result):
    """Reject responses for a different block/transaction, including after failover."""
    if method == 'eth_getBlockByNumber' and _quantity(params[0]):
        if int(result['number'], 16) != int(params[0], 16):
            return False
    if method == 'eth_getBlockByHash' and result['hash'].lower() != params[0].lower():
        return False
    if method in {'eth_getBlockByNumber', 'eth_getBlockByHash'}:
        for tx in result['transactions']:
            if params[1]:
                if not (isinstance(tx, dict) and _hex(tx.get('hash'), 32)
                        and _hex(tx.get('from'), 20) and (tx.get('to') is None or _hex(tx['to'], 20))
                        and _quantity(tx.get('value'))):
                    return False
            elif not _hex(tx, 32):
                return False
    if method in {'eth_getTransactionReceipt', 'eth_getTransactionByHash'}:
        key = 'transactionHash' if method == 'eth_getTransactionReceipt' else 'hash'
        if result[key].lower() != params[0].lower():
            return False
    if method == 'eth_getTransactionReceipt':
        return all(row['blockHash'] == result['blockHash']
                   and row['transactionHash'] == result['transactionHash']
                   and row['blockNumber'] == result['blockNumber'] for row in result['logs'])
    if method == 'eth_getLogs':
        query = params[0] if params else {}
        for row in result:
            if 'blockHash' in query and row['blockHash'].lower() != query['blockHash'].lower():
                return False
            number = int(row['blockNumber'], 16)
            if _quantity(query.get('fromBlock')) and number < int(query['fromBlock'], 16):
                return False
            if _quantity(query.get('toBlock')) and number > int(query['toBlock'], 16):
                return False
    return True


class ResilientRPCProvider(JSONBaseProvider):
    """No Web3 HTTPProvider retry layer: exactly three HTTP attempts per endpoint.

    A verified fallback stays active for this process until restart. Only a
    transient failure exhausted on the active endpoint, or an explicit Alchemy
    monthly quota refusal, permits switching.
    """
    def __init__(self, url, chain_id, limiter, fallback_urls=(), *, session=None, sleep=time.sleep,
                 fallback_limiter=None):
        super().__init__()
        self.urls = tuple(dict.fromkeys([url, *fallback_urls]))
        self.chain_id, self.limiter = chain_id, limiter
        # All fallback endpoints share one conservative, process-shared budget.
        # Direct callers retain the original single-budget behavior by default.
        self.fallback_limiter = fallback_limiter if fallback_limiter is not None else limiter
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep
        self.active = 0
        self.verified = set()
        self.global_ccip_read_enabled = False

    def label(self, index):
        return 'primary' if index == 0 else f'fallback-{index}'

    def _error(self, method, category, attempt, index, retryable=False):
        error = RPCError(method, category, attempt, self.label(index), retryable)
        log.warning('%s', error)
        return error

    def _request(self, index, method, params):
        limiter = self.limiter if index == 0 else self.fallback_limiter
        payload = self.encode_rpc_request(method, params)
        request_id = json.loads(payload)['id']
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if isinstance(limiter, SharedComputeLimiter):
                    limiter.acquire(method)
                else:
                    limiter.acquire()
            except (OSError, sqlite3.Error):
                raise self._error(method, 'rate_limiter', attempt, index) from None
            retry_after = None
            response = None
            try:
                response = self.session.post(self.urls[index], data=payload,
                    headers={'Content-Type': 'application/json'}, timeout=10, allow_redirects=False)
                status = response.status_code
                if status != 200:
                    retry_after = response.headers.get('Retry-After')
                    quota = _alchemy_monthly_capacity(response, self.urls[index], request_id)
                    error = self._error(method, 'monthly_capacity' if quota else str(status),
                                        attempt, index, status in RETRY_STATUSES)
                    if (error.retryable or quota) and retry_after is not None:
                        try:
                            limiter.defer(retry_delay(attempt, retry_after))
                        except (OSError, sqlite3.Error):
                            raise self._error(method, 'rate_limiter', attempt, index) from None
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        raise self._error(method, 'invalid_json', attempt, index) from None
                    if (not isinstance(data, dict) or data.get('jsonrpc') != '2.0'
                            or type(data.get('id')) is not int or data['id'] != request_id
                            or ('result' in data) == ('error' in data)):
                        raise self._error(method, 'invalid_response', attempt, index)
                    if 'error' in data:
                        # Contract reverts are semantic failures, not transport
                        # outages. Preserve Web3's optional metadata behavior,
                        # without forwarding untrusted error text/data to logs.
                        if (method == 'eth_call' and isinstance(data['error'], dict)
                                and data['error'].get('code') == 3):
                            data['error'] = {'code': 3, 'message': 'execution reverted'}
                            return data
                        raise self._error(method, 'json_rpc', attempt, index)
                    if not _valid_result(method, data['result']):
                        raise self._error(method, 'invalid_result', attempt, index)
                    if not _matches_request(method, params, data['result']):
                        raise self._error(method, 'response_mismatch', attempt, index)
                    return data
            except requests.Timeout:
                error = self._error(method, 'timeout', attempt, index, True)
            except requests.ConnectionError:
                error = self._error(method, 'connection', attempt, index, True)
            except requests.RequestException:
                raise self._error(method, 'transport', attempt, index) from None
            finally:
                if response is not None:
                    response.close()
            if not error.retryable or attempt == MAX_ATTEMPTS:
                # No wait is needed if giving up; a subsequent fallback must
                # still honor the exhausted provider's Retry-After minimum.
                if (error.retryable or error.category == 'monthly_capacity') and retry_after is not None:
                    error.fallback_delay = retry_delay(attempt, retry_after)
                raise error from None
            self.sleep(retry_delay(attempt, retry_after))

    def make_request(self, method, params):
        if method not in READ_METHODS:
            raise self._error('unsupported', 'read_only', 1, self.active)
        for index in range(self.active, len(self.urls)):
            try:
                if index not in self.verified:
                    identity = self._request(index, 'eth_chainId', [])
                    if int(identity['result'], 16) != self.chain_id:
                        raise self._error('eth_chainId', 'wrong_chain', 1, index)
                    self.verified.add(index)
                    if method == 'eth_chainId':
                        self.active = index
                        return identity
                result = self._request(index, method, params)
                # Chain identity remains checked when callers explicitly ask.
                if method == 'eth_chainId' and int(result['result'], 16) != self.chain_id:
                    self.verified.discard(index)
                    raise self._error(method, 'wrong_chain', 1, index)
                self.active = index
                return result
            except RPCError as error:
                if (not (error.retryable or error.category == 'monthly_capacity')
                        or index == len(self.urls) - 1):
                    raise
                if error.fallback_delay:
                    self.sleep(error.fallback_delay)
        raise AssertionError('No RPC endpoint')
