from __future__ import annotations
import json,time
from pathlib import Path
from web3 import Web3
from models import WalletInfo
from wallet_policy import is_dex_infrastructure


def effective_watchlist(manager, db, cfg):
    """One definition shared by the scanner and webhook ingress (no network)."""
    manager.reload()
    watch = dict(manager.items)
    candidates = [r[0] for r in db.execute(
        "SELECT wallet FROM discovery_candidates WHERE status='QUARANTINE' ORDER BY first_discovered_at")
        if not is_dex_infrastructure(r[0],cfg)]
    for wallet in candidates[:cfg.promotion_max_wallets]:
        addr = Web3.to_checksum_address(wallet)
        watch.setdefault(addr.lower(),WalletInfo(addr,'quarantine',0,'quarantine'))
    return {w:info for w,info in watch.items() if not is_dex_infrastructure(w,cfg)}

class WatchlistManager:
    def __init__(self, base="watchlist.json", generated="generated_watchlist.json", reload_seconds=30):
        self.base=Path(base)
        self.generated=Path(generated)
        self.reload_seconds=reload_seconds
        self.last_load=0.0
        self.items={}

    def _read(self,path):
        if not path.exists(): return []
        try:return json.loads(path.read_text(encoding="utf-8")).get("wallets",[])
        except Exception:return []

    def reload(self,force=False):
        now=time.time()
        if not force and now-self.last_load<self.reload_seconds:
            return False
        self.last_load=now
        merged={}
        for source,path in (("auto",self.generated),("seed",self.base)):
            for x in self._read(path):
                try:addr=Web3.to_checksum_address(x["address"])
                except Exception:continue
                if is_dex_infrastructure(addr): continue
                merged[addr.lower()]=WalletInfo(
                    address=addr,
                    label=x.get("label",source),
                    weight=int(x.get("weight",1)),
                    group=x.get("group",source)
                )
        changed=merged!=self.items
        self.items=merged
        return changed
