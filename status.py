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


print("\n=== DELIVERY / DISCOVERY HEALTH ===")
tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if "alert_outbox" in tables:
    for r in db.execute("SELECT state,COUNT(*) count FROM alert_outbox GROUP BY state"):
        print(dict(r))
    print("UNKNOWN deliveries require checking Telegram before any manual resend.")
if "discovery_cursors" in tables:
    for r in db.execute("SELECT stream,completed_at,error FROM discovery_cursors"):
        print(dict(r))
if "proposals" in tables:
    print("Pending human proposals:",db.execute("SELECT COUNT(*) FROM proposals WHERE status='PENDING_HUMAN_REVIEW'").fetchone()[0])
if "reconstruction_gaps" in tables:
    print("Unresolved reconstruction gaps:",db.execute("SELECT COUNT(*) FROM reconstruction_gaps").fetchone()[0])
