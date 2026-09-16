from __future__ import annotations
import argparse, json, time, logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import SETTINGS
from storage import Storage
from wallet_metrics import profile_from_rows
from atomic_json import write_json
from forward_metrics import build_forward_profile, collect_outcomes
from market_data import DexScreenerClient

logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("wallet-profiler")

def hour_bucket(ts):
    return int(ts) // 3600

class WalletProfiler:
    def __init__(self):
        self.cfg = SETTINGS
        self.db = Storage()
        self.market = DexScreenerClient(self.cfg.dexscreener_chain_id)

    def wallets(self):
        return [r[0] for r in self.db.db.execute(
            "SELECT DISTINCT lower(wallet) FROM events"
        ).fetchall() if r[0]]

    def _active_hours(self, wallet):
        rows = self.db.db.execute(
            "SELECT DISTINCT timestamp FROM events WHERE lower(wallet)=? ORDER BY timestamp",
            (wallet.lower(),)
        ).fetchall()
        return {hour_bucket(r["timestamp"]) for r in rows if r["timestamp"]}

    def _opportunities(self, active_hours):
        if not active_hours:
            return 0
        rows = self.db.db.execute("SELECT pair_created_at FROM launches").fetchall()
        return sum(1 for r in rows if hour_bucket(r["pair_created_at"]) in active_hours)

    def _launch_entries(self, wallet):
        rows = self.db.db.execute(
            """
            SELECT e.*, l.pair_created_at,
                   CASE WHEN e.token_amount>0 THEN e.usdc_amount/e.token_amount END AS entry_price
            FROM events e
            JOIN launches l ON lower(l.token)=lower(e.token)
            LEFT JOIN trade_context tc
              ON tc.tx_hash=e.tx_hash AND lower(tc.token)=lower(e.token)
              AND lower(tc.wallet)=lower(e.wallet)
            WHERE lower(e.wallet)=? AND e.side='BUY' AND e.trade_verified=1
            ORDER BY e.timestamp
            """,
            (wallet.lower(),)
        ).fetchall()
        by_token = {}
        for r in rows:
            token = r["token"].lower()
            x = by_token.get(token)
            if x is None:
                x = {
                    "token": token,
                    "first_ts": int(r["timestamp"]),
                    "usdc": 0.0,
                    "entry_price": r["entry_price"],
                    "pair_created_at": int(r["pair_created_at"]),
                }
                by_token[token] = x
            x["usdc"] += float(r["usdc_amount"] or 0)
            if not x["entry_price"] and r["entry_price"]:
                x["entry_price"] = r["entry_price"]

        out = []
        for token, x in by_token.items():
            snaps = self.db.db.execute(
                """SELECT horizon_minutes,price_usd FROM launch_snapshots
                   WHERE token=? AND observed_at>? AND price_usd IS NOT NULL
                   ORDER BY observed_at""",
                (token, x["first_ts"])
            ).fetchall()
            future_prices = [float(s["price_usd"]) for s in snaps if s["price_usd"]]
            sell = self.db.db.execute(
                """
                SELECT CASE WHEN e.token_amount>0 THEN e.usdc_amount/e.token_amount END AS exit_price
                FROM events e
                LEFT JOIN trade_context tc
                  ON tc.tx_hash=e.tx_hash AND lower(tc.token)=lower(e.token)
                  AND lower(tc.wallet)=lower(e.wallet)
                WHERE lower(e.wallet)=? AND lower(e.token)=? AND e.side='SELL' AND e.trade_verified=1
                  AND e.timestamp>=?
                ORDER BY e.timestamp LIMIT 1
                """,
                (wallet.lower(), token, x["first_ts"])
            ).fetchone()
            out.append({
                "token": token,
                "usdc": x["usdc"],
                "age_seconds": max(0, x["first_ts"] - x["pair_created_at"]),
                "entry_price": float(x["entry_price"]) if x["entry_price"] else None,
                "max_future_price": max(future_prices) if future_prices else None,
                "exit_price": float(sell["exit_price"]) if sell and sell["exit_price"] else None,
            })
        return out

    def profile_wallet(self, wallet):
        return build_forward_profile(self.db,wallet,self.cfg)

    def cycle(self):
        # Register targets before sampling; stale targets become MISSING, never
        # a convenient late quote or retrospective maximum.
        wallets = self.wallets()
        for w in wallets: self.profile_wallet(w)
        collect_outcomes(self.db,self.market)
        profiles = []
        for w in self.wallets():
            p = self.profile_wallet(w)
            self.db.save_wallet_profile(p)
            profiles.append(p)
        profiles.sort(key=lambda x: (-x["smart_score"], x["wallet"]))
        write_json("data/wallet_intelligence.json", {
            "generated_at": int(time.time()),
            "profiles": {p["wallet"]: p for p in profiles},
        })
        log.info("profiled %s wallets", len(profiles))
        for p in profiles[:10]:
            log.info(
                "%s score=%s class=%s hit=%.1f%% capture=%.1f%% buys=%s",
                p["wallet"], p["smart_score"], p["profile_class"],
                p["adjusted_hit_rate"]*100,
                p["opportunity_capture_rate"]*100,
                p["launches_bought"]
            )
        return profiles

    def run(self):
        while True:
            try:
                self.cycle()
            except KeyboardInterrupt:
                return
            except Exception:
                log.exception("wallet profiler cycle failed")
            time.sleep(self.cfg.profile_refresh_seconds)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    p = WalletProfiler()
    if args.once:
        p.cycle()
    else:
        p.run()
