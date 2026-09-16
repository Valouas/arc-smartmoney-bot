from __future__ import annotations
import sqlite3
from pathlib import Path

p=Path("data/arc_bot.sqlite3")
if not p.exists():
    print("No database yet."); raise SystemExit
db=sqlite3.connect(p); db.row_factory=sqlite3.Row

print("=== V4 AUTO-DISCOVERY ===")
for status in ("QUARANTINE","PROMOTED"):
    n=db.execute("SELECT COUNT(*) n FROM discovery_candidates WHERE status=?",(status,)).fetchone()["n"]
    print(status, n)

print("\n=== PROMOTED ===")
for r in db.execute("SELECT wallet,smart_score,profile_class,weight,label,promoted_at FROM promoted_wallets WHERE enabled=1 ORDER BY smart_score DESC"):
    print(dict(r))

print("\n=== TOP QUARANTINE CANDIDATES ===")
for r in db.execute("""SELECT wallet,source_winners,best_early_rank,profile_score,profile_class,promotion_reason,first_discovered_at,hydrated_at
       FROM discovery_candidates WHERE status='QUARANTINE'
       ORDER BY source_winners DESC,profile_score DESC,best_early_rank ASC LIMIT 30"""):
    print(dict(r))
