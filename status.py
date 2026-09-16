import sqlite3
from pathlib import Path

p = Path("data/arc_bot.sqlite3")
if not p.exists():
    print("No database yet. Start bot.py first.")
    raise SystemExit

db = sqlite3.connect(p)
db.row_factory = sqlite3.Row

print("\n=== OPEN PAPER POSITIONS ===")
rows = db.execute("SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_at DESC").fetchall()
for r in rows: print(dict(r))
if not rows: print("none")

print("\n=== LAST 20 EVENTS ===")
for r in db.execute("SELECT timestamp,wallet,side,symbol,token,usdc_amount FROM events ORDER BY id DESC LIMIT 20"):
    print(dict(r))

print("\n=== LAST 10 SIGNALS ===")
for r in db.execute("SELECT created_at,side,status,score,token FROM signals ORDER BY id DESC LIMIT 10"):
    print(dict(r))
