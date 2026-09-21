from __future__ import annotations
import requests
import math
import logging
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

log = logging.getLogger(__name__)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3


def retry_delay(attempt, retry_after=None):
    """Retry-After is a minimum, in seconds or as an HTTP date."""
    delay = 0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.25)
    if not isinstance(retry_after, str):
        return delay
    try:
        value = retry_after.strip()
        if value.isascii() and value.isdigit():
            seconds = float(value)
        else:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = target.timestamp() - time.time()
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    except (ValueError, TypeError, OverflowError):
        pass  # Invalid header: retain the bounded local backoff.
    return delay

class ArcscanError(RuntimeError):
    """Unavailable or incomplete explorer data; never an empty successful scan."""


def require_complete_history(data):
    # Validated against the live page.coverage schema (2026-09-16). Coverage
    # describes the index; all pages must still be consumed. A capped result
    # remains incomplete even when its last available cursor is exhausted.
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ArcscanError("unrecognized history envelope")
    page = data.get("page") or {}
    coverage = page.get("coverage") or {}
    if (coverage.get("indexed") is not True or coverage.get("from_block") != 0
            or coverage.get("gaps") != [] or coverage.get("gap_blocks") != 0
            or not isinstance(coverage.get("to_block"), int)
            or coverage.get("last_contiguous_block", -1) < coverage["to_block"]
            or data.get("truncated") or data.get("capped")
            or page.get("has_more") not in (True, False)
            or (page.get("has_more") and not page.get("next"))):
        raise ArcscanError("history coverage unverified; keep in quarantine")


class ArcscanClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "arc-smartmoney-research-bot/4.0"

    def _json(self, path: str, params=None, *, page_index=None):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = None
            retryable = False
            retry_after = None
            try:
                response = self.s.get(self.base + path, params=params, timeout=10)
                status = response.status_code
                if not 200 <= status < 300:
                    category = str(status)
                    retryable = status in RETRY_STATUSES
                    retry_after = response.headers.get("Retry-After")
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        category = "invalid_json"
                    else:
                        if (not isinstance(data, (dict, list)) or
                                isinstance(data, dict) and (data.get("error") or data.get("truncated"))):
                            category = "invalid_payload"
                        else:
                            from data_preservation import archive_api_response
                            archive_api_response('arcscan',path,params,data)
                            return data
            except requests.Timeout:
                category, retryable = "timeout", True
            except requests.ConnectionError:
                category, retryable = "connection", True
            except requests.RequestException:
                category = "request_error"
            finally:
                if response is not None:
                    response.close()

            # Only controlled categories and counters are logged, never a URL,
            # cursor, response body, Retry-After header or raw exception text.
            if not retryable or attempt == MAX_ATTEMPTS:
                log.warning("Arcscan request failed category=%s page=%s attempt=%s/%s",
                            category, page_index, attempt, MAX_ATTEMPTS)
                raise ArcscanError(f"Arcscan request failed: category={category} attempts={attempt}") from None
            delay = retry_delay(attempt, retry_after)
            log.warning("Arcscan retry category=%s page=%s attempt=%s/%s delay=%.2fs",
                        category, page_index, attempt, MAX_ATTEMPTS, delay)
            time.sleep(delay)

    def address_overview(self, addr: str):
        return self._json(f"/address/{addr}")

    def address_facts(self, addr: str):
        return self._json(f"/address/{addr}/facts")

    def address_activity(self, addr: str, limit=100, cursor=None, kind=None, token=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if kind: params["kind"] = kind
        if token: params["token"] = token
        return self._json(f"/address/{addr}/activity", params=params)

    def explore_contracts(self, limit=100, cursor=None, filter_name=None, *, page_index=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if filter_name: params["filter"] = filter_name
        return self._json("/explore/contracts", params=params, page_index=page_index)

    def explore_token_transfers(self, limit=100, cursor=None, standard="erc20", *, page_index=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if standard: params["standard"] = standard.lower().replace("-", "")
        return self._json("/explore/token-transfers", params=params, page_index=page_index)

    def token_transfers(self, token: str, limit=100, cursor=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        return self._json(f"/tokens/{token}/transfers", params=params)

    @staticmethod
    def next_cursor(data):
        if not isinstance(data, dict):
            return None
        for key in ("next_cursor", "nextCursor", "cursor_next", "next"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v
        page = data.get("page") or data.get("pagination") or data.get("meta")
        if isinstance(page, dict):
            for key in ("next_cursor", "nextCursor", "next"):
                v = page.get(key)
                if isinstance(v, str) and v:
                    return v
        return None

    def concentration(self, token: str) -> tuple[Optional[float], Optional[int]]:
        try:
            data = self._json(f"/tokens/{token}/concentration")
        except Exception:
            return None, None
        # Only explicit units are accepted. "top10": 0.5 is ambiguous, and
        # recursive substring matching could mistake a top100/count for a percent.
        if not isinstance(data, dict):
            return None, None
        if "summary" in data:
            census = data.get("census") or {}
            if census.get("is_census") is not True or census.get("truncated") is not False:
                return None, None
            try:
                val = float(data["summary"]["top10_share"])
                holders = int(census["enumerated"])
                return (val, holders) if math.isfinite(val) and 0 <= val <= 100 and holders >= 0 else (None, None)
            except (KeyError, TypeError, ValueError):
                return None, None
        node = data.get("concentration", data)
        if not isinstance(node, dict):
            return None, None
        top10 = None
        for key, scale in (("top10_pct", 1), ("top_10_pct", 1),
                           ("top10_percent", 1), ("top10_ratio", 100)):
            if key in node:
                try:
                    val = float(node[key]) * scale
                    if math.isfinite(val) and 0 <= val <= 100:
                        top10 = val
                except (TypeError, ValueError):
                    pass
                break
        try:
            holders = int(node["holder_count"])
            if holders < 0: holders = None
        except (KeyError, TypeError, ValueError):
            holders = None
        return top10, holders
