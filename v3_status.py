from __future__ import annotations
import sqlite3
from pathlib import Path

DB = Path("data/arc_bot.sqlite3")
if not DB.exists():
    print("No database yet.")
    raise SystemExit

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

launches = db.execute("SELECT COUNT(*) n FROM launches").fetchone()["n"]
cands = db.execute("SELECT COUNT(*) n FROM launch_candidates WHERE status='CANDIDATE'").fetchone()["n"]
profiles = db.execute("SELECT COUNT(*) n FROM wallet_profiles").fetchone()["n"]
scanner = db.execute("SELECT COUNT(*) n FROM wallet_profiles WHERE profile_class='SCANNER_BOT'").fetchone()["n"]

print("=== ARC V3 STATUS ===")
print("Live launches indexed:", launches)
print("Candidates waiting:", cands)
print("Wallet profiles:", profiles)
print("Scanner bots excluded:", scanner)

print("\n=== TOP PROFILES ===")
for r in db.execute("SELECT wallet,smart_score,profile_class,recommended_weight,confirmation_eligible FROM wallet_profiles ORDER BY smart_score DESC LIMIT 20"):
    print(dict(r))

print("\n=== RECENT LAUNCHES ===")
for r in db.execute("SELECT symbol,token,pair_created_at,first_liquidity_usd,first_market_cap_usd FROM launches ORDER BY pair_created_at DESC LIMIT 20"):
    print(dict(r))
