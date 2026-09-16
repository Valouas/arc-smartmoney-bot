from __future__ import annotations
import json, time
from pathlib import Path
from wallet_policy import is_dex_infrastructure

class IntelligenceBook:
    def __init__(self, path="data/wallet_intelligence.json", reload_seconds=30):
        self.path = Path(path)
        self.reload_seconds = reload_seconds
        self.last_load = 0
        self.profiles = {}

    def reload_if_due(self):
        now = time.time()
        if now - self.last_load < self.reload_seconds:
            return
        self.last_load = now
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.profiles = data.get("profiles", {})
        except Exception:
            pass

    def get(self, wallet):
        if is_dex_infrastructure(wallet): return None
        self.reload_if_due()
        return self.profiles.get(wallet.lower())

    def effective(self, wallet, fallback_score, seed_weight):
        p = self.get(wallet)
        if not p or p.get("profile_version") != 2 or time.time()-p.get("generated_at",0)>max(120,self.reload_seconds*4):
            return {"score":0,"class":"UNVERIFIED","weight":0,"eligible":False}
        return {
            "score": int(p.get("smart_score", fallback_score)),
            "class": p.get("profile_class", "WATCH"),
            "weight": int(p.get("recommended_weight", seed_weight)),
            "eligible": bool(p.get("confirmation_eligible", False) and p.get("data_quality_ok",False)),
        }
