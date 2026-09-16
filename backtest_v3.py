from __future__ import annotations
import json, sqlite3, statistics
from pathlib import Path
from config import SETTINGS

def f(x):
    try: return float(x)
    except Exception: return None

class BacktestV3:
    def __init__(self, db_path="data/arc_bot.sqlite3"):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row

    def validated_signals(self):
        return self.db.execute("SELECT id,created_at,token,score,payload FROM signals WHERE side='BUY' AND status='VALIDATED' ORDER BY created_at").fetchall()

    def signal_entry_price(self, row):
        try:
            payload = json.loads(row["payload"])
            return f(((payload.get("risk") or {}).get("market") or {}).get("price_usd"))
        except Exception: return None

    def future_prices(self, token, after_ts):
        rows = self.db.execute("SELECT horizon_minutes,observed_at,delay_seconds,price_usd FROM launch_snapshots WHERE lower(token)=lower(?) AND observed_at>? AND price_usd IS NOT NULL ORDER BY observed_at", (token, after_ts)).fetchall()
        return [{"horizon":r["horizon_minutes"],"observed_at":r["observed_at"],"delay":r["delay_seconds"],"price":float(r["price_usd"])} for r in rows]

    def run(self):
        trades=[]
        for r in self.validated_signals():
            entry=self.signal_entry_price(r); future=self.future_prices(r["token"], r["created_at"])
            if not entry or entry <= 0 or not future:
                trades.append({"signal_id":r["id"],"token":r["token"],"score":r["score"],"evaluable":False,"reason":"missing_entry_or_future_snapshot"})
                continue
            returns=[100*(x["price"]/entry-1) for x in future]
            max_ret=max(returns); final_ret=returns[-1]
            trades.append({"signal_id":r["id"],"token":r["token"],"score":r["score"],"evaluable":True,"entry_price":entry,"max_return_pct":max_ret,"last_return_pct":final_ret,"winner":max_ret>=SETTINGS.winner_return_pct,"snapshots":future})
        ev=[x for x in trades if x["evaluable"]]; wins=[x for x in ev if x["winner"]]
        summary={"validated_signals":len(trades),"evaluable_signals":len(ev),"winners":len(wins),"winner_threshold_pct":SETTINGS.winner_return_pct,"win_rate":len(wins)/len(ev) if ev else None,"median_max_return_pct":statistics.median([x["max_return_pct"] for x in ev]) if ev else None,"median_last_return_pct":statistics.median([x["last_return_pct"] for x in ev]) if ev else None,"insufficient_data":len(ev)<10}
        out={"summary":summary,"trades":trades}
        Path("data/backtest_v3.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print("=== V3 FORWARD BACKTEST ===")
        for k,v in summary.items(): print(f"{k}: {v}")
        if summary["insufficient_data"]: print("\nWARNING: fewer than 10 evaluable VALIDATED signals; do not infer an edge yet.")
        return out

if __name__ == "__main__":
    BacktestV3().run()
