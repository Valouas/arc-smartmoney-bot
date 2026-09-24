"""Last completed evidence, separate from mutable refresh work and checkpoints."""
import hashlib
import json
from pathlib import Path


SCHEMA = '''
CREATE TABLE IF NOT EXISTS historical_snapshots (
 wallet TEXT PRIMARY KEY, run TEXT NOT NULL, context TEXT,
 invalidated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS historical_snapshot_transactions (
 wallet TEXT NOT NULL, tx_hash TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(wallet,tx_hash)
);
CREATE TABLE IF NOT EXISTS historical_refresh_context (
 wallet TEXT PRIMARY KEY, context TEXT NOT NULL, reuse INTEGER NOT NULL DEFAULT 0
);
'''


def decoding_context(cfg):
    digest = hashlib.sha256(str(cfg.chain_id).encode())
    root = Path(__file__).resolve().parent
    for name in ('historical_collector.py', 'historical_prefetch.py', 'decoder.py', 'rpc.py', 'dex_registry.py'):
        digest.update((root/name).read_bytes())
    path = Path(cfg.trusted_dex_registry)
    digest.update(path.read_bytes() if path.exists() else b'no-registry')
    return digest.hexdigest()


def preserve_completed(db, wallet):
    """Caller owns the transaction: header and evidence are published together."""
    row = db.execute('SELECT * FROM historical_runs WHERE wallet=?', (wallet,)).fetchone()
    if not row or row['phase'] != 'COMPLETE' or row['version'] != 1:
        return
    if db.execute('SELECT 1 FROM historical_transactions WHERE wallet=? AND payload IS NULL LIMIT 1', (wallet,)).fetchone():
        return
    previous = db.execute('SELECT run,invalidated FROM historical_snapshots WHERE wallet=?', (wallet,)).fetchone()
    run = dict(row)
    if previous and not previous['invalidated']:
        old = json.loads(previous['run'])
        if (old['cutoff_block'],old.get('completed_at') or 0) > (run['cutoff_block'],run.get('completed_at') or 0):
            return
        if all(old.get(k) == run.get(k) for k in ('version','started_at','cutoff_block','cutoff_hash','completed_at')):
            return
    context = db.execute('SELECT context FROM historical_refresh_context WHERE wallet=?', (wallet,)).fetchone()
    db.execute('DELETE FROM historical_snapshot_transactions WHERE wallet=?', (wallet,))
    db.execute('INSERT INTO historical_snapshot_transactions SELECT wallet,tx_hash,payload FROM historical_transactions WHERE wallet=?', (wallet,))
    db.execute('INSERT OR REPLACE INTO historical_snapshots(wallet,run,context) VALUES(?,?,?)',
               (wallet, json.dumps(run), context[0] if context else None))


def initialize_snapshots(db):
    db.executescript(SCHEMA)
    # Legacy completed generations are saved once, with unknown decode context.
    # They remain reportable but cannot silently qualify for payload reuse.
    with db:
        wallets = db.execute('''SELECT wallet FROM historical_runs WHERE phase='COMPLETE' AND version=1
            AND wallet NOT IN (SELECT wallet FROM historical_snapshots)''').fetchall()
        for row in wallets:
            preserve_completed(db, row[0])


def invalidate(db, wallet):
    db.execute('UPDATE historical_snapshots SET invalidated=1 WHERE wallet=?', (wallet,))
    db.execute('UPDATE historical_runs SET version=0 WHERE wallet=?', (wallet,))


def add_hashes(db, wallet, hashes, reuse):
    """Only reuse rows actually enumerated by this fresh Arcscan traversal."""
    for h in hashes:
        payload = None
        if reuse:
            saved = db.execute('SELECT payload FROM historical_snapshot_transactions WHERE wallet=? AND tx_hash=?', (wallet,h)).fetchone()
            if saved:
                row = json.loads(saved[0])
                # Revisit missing/ambiguous evidence instead of freezing defects.
                if (not row.get('outside_snapshot') and not row.get('error')
                        and not row.get('inventory_gap') and row.get('gas_usdc') is not None
                        and all(t.get('confirmation_eligible') is True for t in row.get('trades', []))):
                    payload = saved[0]
        db.execute('INSERT OR IGNORE INTO historical_transactions VALUES(?,?,?)', (wallet,h,payload))
