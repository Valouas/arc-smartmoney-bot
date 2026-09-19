import sqlite3
import json
from pathlib import Path

p = Path("data/arc_bot.sqlite3")
backup_status = Path('data/backup_status.json')
if backup_status.exists():
    try:
        state=json.loads(backup_status.read_text(encoding='utf-8'))
        print('Automatic backup:',state.get('status'),'| last success:',state.get('last_success'))
    except (OSError,ValueError):
        print('Automatic backup: status unavailable')
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
if "scanner_state" in tables:
    state={r['key']:r['value'] for r in db.execute("SELECT key,value FROM scanner_state")}
    print("Scanner checkpoint next_block:",state.get('next_block'))
    print("Scanner pending block (not completed):",state.get('pending_block'))
if 'historical_runs' in tables:
    from historical_store import VERSION
    print('\n=== HISTORICAL COLLECTION (separate from forward promotion) ===')
    for row in db.execute('''WITH wallets AS (
        SELECT wallet FROM discovery_candidates UNION SELECT wallet FROM wallet_profiles)
        SELECT CASE WHEN r.wallet IS NULL OR r.version<>? THEN 'EN ATTENTE'
                    WHEN r.phase='COMPLETE' THEN 'ANALYSE' ELSE 'EN ANALYSE' END AS analysis,
               COUNT(*) count FROM wallets w LEFT JOIN historical_runs r ON r.wallet=w.wallet
        GROUP BY analysis''', (VERSION,)):
        print(dict(row))
    print('Historical decoded / identified transactions:', tuple(db.execute(
        'SELECT COUNT(payload),COUNT(*) FROM historical_transactions').fetchone()))
    print('Historical pending transactions:', db.execute('SELECT COUNT(*) FROM historical_transactions WHERE payload IS NULL').fetchone()[0])
    print('Ranking: python wallet_ranking.py --historical --top 20')
if 'alchemy_jobs' in tables:
    print('\n=== ALCHEMY WEBHOOK QUEUE ===')
    for row in db.execute('SELECT status,COUNT(*) count FROM alchemy_jobs GROUP BY status'):
        print(dict(row))
    print('Accepted deliveries:',db.execute('SELECT COUNT(*) FROM alchemy_deliveries').fetchone()[0])
    if 'alchemy_paper_triggers' in tables:
        print('Paper-copy triggers (consumer status: python -m paper_copy --status):',
              db.execute('SELECT COUNT(*) FROM alchemy_paper_triggers').fetchone()[0])
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
