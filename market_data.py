from __future__ import annotations
import requests
import math
from typing import Optional
from models import MarketSnapshot

class DexScreenerClient:
    BASE = "https://api.dexscreener.com"

    def __init__(self, chain_id: str = "arc"):
        self.chain_id = chain_id
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "arc-smartmoney-research-bot/1.0"

    def _get(self, url: str, timeout=7):
        r = self.s.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def token_snapshot(self, token: str) -> Optional[MarketSnapshot]:
        pairs = []
        try:
            data = self._get(f"{self.BASE}/tokens/v1/{self.chain_id}/{token}")
            if isinstance(data, list): pairs = data
        except Exception: pass

        if not pairs:
            try:
                r = self.s.get(f"{self.BASE}/latest/dex/search", params={"q": token}, timeout=7)
                r.raise_for_status()
                candidates = r.json().get("pairs", []) or []
                pairs = [p for p in candidates if str(p.get("baseToken", {}).get("address", "")).lower() == token.lower() or str(p.get("quoteToken", {}).get("address", "")).lower() == token.lower()]
            except Exception:
                pairs = []

        # priceUsd, symbol and marketCap describe baseToken, never quoteToken.
        pairs = [p for p in pairs if p.get("chainId") == self.chain_id
                 and str((p.get("baseToken") or {}).get("address", "")).lower() == token.lower()]
        if not pairs: return None

        def liq(p):
            try: return float((p.get("liquidity") or {}).get("usd") or 0)
            except Exception: return 0.0
        p = max(pairs, key=liq)
        vol = p.get("volume") or {}
        txns = p.get("txns") or {}
        m5 = txns.get("m5") or {}
        def f(x):
            try:
                value = float(x)
                return value if math.isfinite(value) and value >= 0 else None
            except Exception: return None
        def i(x):
            try: return int(x) if x is not None else None
            except Exception: return None
        return MarketSnapshot(
            token=token,
            symbol=(p.get("baseToken") or {}).get("symbol", "?"),
            price_usd=f(p.get("priceUsd")),
            liquidity_usd=f((p.get("liquidity") or {}).get("usd")),
            market_cap_usd=f(p.get("marketCap")),
            fdv_usd=f(p.get("fdv")),
            volume_m5=f(vol.get("m5")),
            volume_h1=f(vol.get("h1")),
            buys_m5=i(m5.get("buys")),
            sells_m5=i(m5.get("sells")),
            pair_created_at_ms=i(p.get("pairCreatedAt")),
            pair_address=p.get("pairAddress"), dex_id=p.get("dexId"), url=p.get("url"),
        )
