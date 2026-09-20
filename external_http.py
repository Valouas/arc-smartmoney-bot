"""Bounded GET-only transport. Errors contain categories, never provider text."""
import hashlib
import json
import math
import random
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests


class ExternalError(RuntimeError):
    def __init__(self, category, retry_after=0):
        self.category, self.retry_after = category, retry_after
        super().__init__(category)


def wallet_address(value):
    if (not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}', value)
            or int(value[2:], 16) == 0):
        raise ExternalError('invalid_wallet')
    return value.lower()


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ExternalError('non_finite_number')
    if isinstance(value, dict):
        for child in value.values(): finite_tree(child)
    if isinstance(value, list):
        for child in value: finite_tree(child)


def number(value, optional=False):
    if value is None and optional: return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ExternalError('invalid_number')
    return value


def stamp(value):
    if type(value) is int and value >= 0: return value
    if not isinstance(value, str): raise ExternalError('invalid_timestamp')
    try:
        match = re.fullmatch(r'(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value)
        if not match: raise ValueError()
        base, fraction, zone = match.groups()
        iso = base + ('.'+fraction[:6].ljust(6,'0') if fraction else '') + zone.replace('Z','+00:00')
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None: raise ValueError()
        result = int(parsed.timestamp())
        if result < 0: raise ValueError()
        return result
    except (ValueError, OverflowError):
        raise ExternalError('invalid_timestamp') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def retry_seconds(value, now):
    if value is None: return 0
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try: delay = parsedate_to_datetime(value).timestamp()-now
        except (ValueError, TypeError, OverflowError): return 0
    return max(0, delay) if math.isfinite(delay) else 0


class ExternalHTTP:
    max_bytes = 2 * 1024 * 1024
    max_inline_delay = 30

    def __init__(self, provider, key, session=None, sleep=time.sleep, clock=time.time,
                 min_interval=0, monotonic=time.monotonic, before_request=None):
        if provider not in ('dune', 'zerion'): raise ValueError('Unknown provider')
        if not key: raise ExternalError('missing_configuration')
        self.provider, self.session = provider, session or requests.Session()
        self.base = {'dune':'https://api.dune.com/api/v1/', 'zerion':'https://api.zerion.io/v1/'}[provider]
        self.headers = {'Accept':'application/json'}
        self.auth = (key, '') if provider == 'zerion' else None
        if provider == 'dune': self.headers['X-Dune-API-Key'] = key
        self.sleep, self.clock = sleep, clock
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError('Invalid external request interval')
        self.min_interval, self.monotonic, self.next_request = min_interval, monotonic, 0.0
        self.before_request = before_request

    def get(self, path, params=None):
        if not re.fullmatch(r'[A-Za-z0-9_/-]+', path) or '..' in path:
            raise ExternalError('invalid_route')
        for attempt in range(3):
            delay = 0
            try:
                remaining = self.next_request - self.monotonic()
                if remaining > 0: self.sleep(remaining)
                self.next_request = self.monotonic() + self.min_interval
                if self.before_request is not None: self.before_request()
                with self.session.get(self.base+path, params=params, headers=self.headers,
                        auth=self.auth, timeout=(5, 20), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status != 200:
                        category = 'http_'+str(status)
                        if status != 429 and not 500 <= status <= 599:
                            raise ExternalError(category)
                        delay = retry_seconds(response.headers.get('Retry-After'), self.clock())
                    else:
                        if 'application/json' not in response.headers.get('Content-Type', '').lower():
                            raise ExternalError('content_type')
                        body = bytearray()
                        for chunk in response.iter_content(16384):
                            body.extend(chunk)
                            if len(body) > self.max_bytes: raise ExternalError('payload_limit')
                        try:
                            data = json.loads(body.decode('utf-8'))
                            finite_tree(data)
                        except (ValueError, UnicodeError, RecursionError):
                            raise ExternalError('invalid_json') from None
                        if not isinstance(data, dict): raise ExternalError('invalid_schema')
                        return data
            except requests.Timeout: category = 'timeout'
            except requests.ConnectionError: category = 'connection'
            except requests.RequestException:
                raise ExternalError('transport') from None
            if attempt == 2 or delay > self.max_inline_delay:
                raise ExternalError(category, delay)
            self.sleep(max(delay, .5*(2**attempt)+random.uniform(0, .25)))
        raise AssertionError('Unreachable')

    def close(self): self.session.close()
