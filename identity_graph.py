from __future__ import annotations
from dataclasses import dataclass
from collections import defaultdict
from typing import Optional
import time
import re
from concurrent.futures import ThreadPoolExecutor

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
    def __init__(self, arcscan_client, cache_seconds=3600, workers=4):
        self.arcscan = arcscan_client
        self.facts: dict[str, WalletFacts] = {}
        self.parent: dict[str, str] = {}
        self.last_refresh = 0.0
        self.cache_seconds = cache_seconds
        self.workers = max(1,min(workers,8))
        self._retry = {}

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
        old=self.facts
        self.parent={}; self.facts={}
        now=time.time()
        wallets=list(dict.fromkeys(a.lower() for a in wallets))
        def fetch(addr):
            cached=old.get(addr)
            if cached and cached.funded_by and now-cached.fetched_at<self.cache_seconds:
                return addr,cached
            retry=self._retry.get(addr,(0,0))
            if now<retry[1]: return addr,WalletFacts(address=addr)
            try:
                # requests.Session is not shared across worker threads.
                if hasattr(self.arcscan,"base"):
                    from arcscan import ArcscanClient
                    client=ArcscanClient(self.arcscan.base)
                    try: data=client.address_facts(addr)
                    finally: client.s.close()
                else: data=self.arcscan.address_facts(addr)
                return addr,self._parse_facts(addr,data)
            except Exception: return addr,WalletFacts(address=addr)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            fetched=list(pool.map(fetch,wallets))
            funders=list({f.funded_by for _,f in fetched if f.funded_by and not f.shared_service}-set(wallets))[:128]
            fetched.extend(pool.map(fetch,funders))
        for addr,facts in fetched:
            self._make(addr); self.facts[addr]=facts
            if facts.funded_by:
                self._retry.pop(addr,None)
            else:
                failures=self._retry.get(addr,(0,0))[0]+1
                self._retry[addr]=(failures,now+min(3600,30*2**min(failures-1,7)))
        shared={addr for addr,f in self.facts.items() if f.shared_service}
        buckets=defaultdict(list)
        for addr,facts in self.facts.items():
            if facts.funded_by and not facts.shared_service:
                buckets[facts.funded_by].append(addr)
        for funder,members in buckets.items():
            if funder in shared: continue
            for addr in members:
                self.union(addr,funder)
        self.last_refresh=time.time()

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
            if facts.first_tx_hash is None and ("first" in path and "tx" in path or path=="first.hash"):
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


class IdentityRefresher:
    """Single background refresh; apply snapshots only for the current watch set."""
    def __init__(self, client, cache_seconds=3600, workers=4):
        self.current=IdentityGraph(client,cache_seconds,workers)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="identity-refresh")
        self.future=None
        self.requested=()
        self.last_request=0.0

    def poll(self, wallets, interval=30):
        import copy
        key=tuple(sorted(set(w.lower() for w in wallets)))
        if self.future is not None and self.future.done():
            result=self.future.result()
            if self.requested==key: self.current=result
            self.future=None
        now=time.time()
        if self.future is None and (key!=self.requested or now-self.last_request>=interval):
            graph=IdentityGraph(self.current.arcscan,self.current.cache_seconds,self.current.workers)
            graph.facts=copy.deepcopy(self.current.facts)
            graph._retry=dict(self.current._retry)
            def refresh():
                graph.refresh(list(key))
                return graph
            self.requested=key; self.last_request=now
            self.future=self.executor.submit(refresh)
        return self.current

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=True)
