from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional
import time
import re

@dataclass
class WalletFacts:
    address: str
    funded_by: Optional[str] = None
    first_tx_hash: Optional[str] = None
    first_block: Optional[int] = None
    latest_block: Optional[int] = None
    fetched_at: int = 0
    shared_service: bool = False

def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield p.lower(), v
            yield from _walk(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")

def _first_address(value):
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        return value.lower()
    if isinstance(value, dict):
        for key in ("address", "from", "funder", "funded_by", "fundedBy"):
            v = value.get(key)
            if isinstance(v, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", v):
                return v.lower()
    return None

class IdentityGraph:
    def __init__(self, arcscan_client):
        self.arcscan = arcscan_client
        self.facts: dict[str, WalletFacts] = {}
        self.parent: dict[str, str] = {}
        self.last_refresh = 0.0

    def _make(self, x: str):
        x = x.lower(); self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        x = x.lower(); self._make(x)
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.parent[rb] = ra

    def refresh(self, wallets: list[str]):
        self.parent = {}
        self.facts = {}
        funder_buckets = defaultdict(list)
        wallets = list(dict.fromkeys(a.lower() for a in wallets))
        for addr in wallets:
            addr = addr.lower(); self._make(addr)
            try: facts = self._parse_facts(addr, self.arcscan.address_facts(addr))
            except Exception: facts = WalletFacts(address=addr)
            self.facts[addr] = facts
            if facts.funded_by and not facts.shared_service:
                funder_buckets[facts.funded_by].append(addr)
                if facts.funded_by in wallets:
                    self.union(addr, facts.funded_by)
        for _, members in funder_buckets.items():
            if len(members) > 1:
                lead = members[0]
                for other in members[1:]: self.union(lead, other)
        self.last_refresh = time.time()

    def _parse_facts(self, addr: str, data) -> WalletFacts:
        facts = WalletFacts(address=addr, fetched_at=int(time.time()))
        if not isinstance(data, (dict, list)): return facts
        if isinstance(data, dict) and (data.get("available") is False or data.get("history_complete") is False): return facts
        for path, value in _walk(data):
            if facts.funded_by is None and ("funded_by" in path or "fundedby" in path or path.endswith(".funder") or path == "funder"):
                candidate = _first_address(value)
                if candidate and candidate != addr.lower():
                    facts.funded_by = candidate
                    if isinstance(value, dict):
                        label = value.get("label") or {}
                        category = value.get("category") or (label.get("category") if isinstance(label, dict) else None)
                        facts.shared_service = category in {"exchange", "bridge", "faucet", "custodian"}
            if facts.first_tx_hash is None and "first" in path and "tx" in path:
                if isinstance(value, str) and value.startswith("0x") and len(value) == 66: facts.first_tx_hash = value
            if facts.first_block is None and "first" in path and "block" in path:
                try: facts.first_block = int(value)
                except Exception: pass
            if facts.latest_block is None and ("latest" in path or "last" in path) and "block" in path:
                try: facts.latest_block = int(value)
                except Exception: pass
        return facts

    def eligible(self, wallet):
        facts = self.facts.get(wallet.lower())
        return bool(facts and facts.funded_by and not facts.shared_service)

    def identity_id(self, wallet: str) -> str: return self.find(wallet)
    def independent_count(self, wallets: list[str]) -> int: return len({self.identity_id(w) for w in wallets})
    def cluster_map(self):
        out = defaultdict(list)
        for w in self.parent: out[self.find(w)].append(w)
        return dict(out)
    def related(self, a: str, b: str) -> bool: return self.find(a) == self.find(b)
