from __future__ import annotations
import json,time
from pathlib import Path
from web3 import Web3
from models import WalletInfo

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
                merged[addr.lower()]=WalletInfo(
                    address=addr,
                    label=x.get("label",source),
                    weight=int(x.get("weight",1)),
                    group=x.get("group",source)
                )
        changed=merged!=self.items
        self.items=merged
        return changed
