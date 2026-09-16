from __future__ import annotations
import requests
from typing import Optional

class ArcscanClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "arc-smartmoney-research-bot/4.0"

    def _json(self, path: str, params=None):
        r = self.s.get(self.base + path, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def address_overview(self, addr: str):
        try: return self._json(f"/address/{addr}")
        except Exception: return {}

    def address_facts(self, addr: str):
        try: return self._json(f"/address/{addr}/facts")
        except Exception: return {}

    def address_activity(self, addr: str, limit=100, cursor=None, kind=None, token=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if kind: params["kind"] = kind
        if token: params["token"] = token
        try: return self._json(f"/address/{addr}/activity", params=params)
        except Exception: return {}

    def explore_contracts(self, limit=100, cursor=None, filter_name=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if filter_name: params["filter"] = filter_name
        try: return self._json("/explore/contracts", params=params)
        except Exception: return {}

    def explore_token_transfers(self, limit=100, cursor=None, standard="ERC-20"):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        if standard: params["standard"] = standard
        try: return self._json("/explore/token-transfers", params=params)
        except Exception: return {}

    def token_transfers(self, token: str, limit=100, cursor=None):
        params = {"limit": limit}
        if cursor: params["cursor"] = cursor
        try: return self._json(f"/tokens/{token}/transfers", params=params)
        except Exception: return {}

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
        flat = {}
        def walk(obj, prefix=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    key = f"{prefix}.{k}" if prefix else k
                    flat[key.lower()] = v
                    walk(v, key)
            elif isinstance(obj, list):
                for n, v in enumerate(obj):
                    walk(v, f"{prefix}[{n}]")
        walk(data)
        top10 = None
        holders = None
        for k, v in flat.items():
            if top10 is None and ("top10" in k or "top_10" in k or "top 10" in k):
                try:
                    val = float(v)
                    if 0 <= val <= 1: val *= 100
                    if 0 <= val <= 100: top10 = val
                except Exception: pass
            if holders is None and ("holder_count" in k or k.endswith(".holders") or k == "holders"):
                try: holders = int(v)
                except Exception: pass
        return top10, holders
