"""Recover evidence from a SQLite backup without overwriting live work or paper."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from historical_store import initialize
from process_lock import ProcessLock


ARCHIVE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS historical_recovery_runs (
 source_id TEXT NOT NULL, wallet TEXT NOT NULL, run TEXT NOT NULL,
 PRIMARY KEY(source_id,wallet)
);
CREATE TABLE IF NOT EXISTS historical_recovery_transactions (
 source_id TEXT NOT NULL, wallet TEXT NOT NULL, tx_hash TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(source_id,wallet,tx_hash)
);
'''


def recover(source, target, source_id, verify_anchor, *, apply=False):
    """Partial evidence is archived, never promoted into a completed snapshot."""
    source.row_factory = target.row_factory = sqlite3.Row
    runs = {r['wallet']: dict(r) for r in source.execute('SELECT * FROM historical_runs')}
    existing = set(tuple(r) for r in target.execute('SELECT wallet,tx_hash FROM historical_transactions WHERE payload IS NOT NULL'))
    rows = list(source.execute('SELECT wallet,tx_hash,payload FROM historical_transactions WHERE payload IS NOT NULL'))
    counts = {}
    for row in rows:
        payload = json.loads(row['payload'])
        if not isinstance(payload, dict) or payload.get('tx_hash') != row['tx_hash'] or row['wallet'] not in runs:
            raise ValueError('Backup historical payload invalid')
        counts[row['wallet']] = counts.get(row['wallet'],0)+1
    result = {'backup_decoded_rows':len(rows), 'missing_from_current_work':sum((r['wallet'],r['tx_hash']) not in existing for r in rows),
              'archived_rows_added':0, 'completed_snapshots_restored':0}
    if not apply: return result
    initialize(target)
    target.executescript(ARCHIVE_SCHEMA)
    candidates = []
    for wallet, run in runs.items():
        if run['phase'] != 'COMPLETE' or run['version'] != 1: continue
        total = source.execute('SELECT COUNT(*) FROM historical_transactions WHERE wallet=?',(wallet,)).fetchone()[0]
        if total != counts.get(wallet,0): continue
        old = target.execute('SELECT run,invalidated FROM historical_snapshots WHERE wallet=?',(wallet,)).fetchone()
        if old:
            prior = json.loads(old['run'])
            if old['invalidated'] or (prior['cutoff_block'],prior.get('completed_at') or 0) >= (run['cutoff_block'],run.get('completed_at') or 0):
                continue
        if verify_anchor(run['cutoff_block'],run['cutoff_hash']): candidates.append(wallet)
    with target:
        target.executemany('INSERT OR IGNORE INTO historical_recovery_runs VALUES(?,?,?)',
                           [(source_id,w,json.dumps(r)) for w,r in runs.items()])
        before = target.total_changes
        target.executemany('INSERT OR IGNORE INTO historical_recovery_transactions VALUES(?,?,?,?)',
                           [(source_id,r['wallet'],r['tx_hash'],r['payload']) for r in rows])
        result['archived_rows_added'] = target.total_changes-before
        for wallet in candidates:
            target.execute('DELETE FROM historical_snapshot_transactions WHERE wallet=?',(wallet,))
            target.execute('''INSERT INTO historical_snapshot_transactions
                SELECT wallet,tx_hash,payload FROM historical_recovery_transactions WHERE source_id=? AND wallet=?''',(source_id,wallet))
            target.execute('INSERT OR REPLACE INTO historical_snapshots(wallet,run,context) VALUES(?,?,NULL)',(wallet,json.dumps(runs[wallet])))
        result['completed_snapshots_restored'] = len(candidates)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--db', default='data/arc_bot.sqlite3')
    parser.add_argument('--apply', action='store_true', help='Default: read-only inventory. Stop the launcher before applying.')
    args = parser.parse_args()
    backup, dest = Path(args.backup).resolve(), Path(args.db).resolve()
    if backup == dest: parser.error('Backup and live database must differ')
    source_id = hashlib.sha256((str(backup)+str(backup.stat().st_size)+str(backup.stat().st_mtime_ns)).encode()).hexdigest()
    from config import SETTINGS
    from rpc import ArcRPC
    from decoder import hex_value
    from functools import lru_cache
    rpc = None
    try:
        if args.apply:
            rpc = ArcRPC(SETTINGS.rpc_url,SETTINGS.chain_id);rpc.sanity_check()
        @lru_cache(maxsize=None)
        def verify(number, expected):
            return hex_value(rpc.get_block(number,False)['hash']) == expected
        with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as source:
            source.execute('BEGIN')
            if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok': raise ValueError('Backup integrity check failed')
            if args.apply:
                with ProcessLock(dest.parent/'run_all.lock'), ProcessLock(str(dest)+'.historical.lock'):
                    # A fresh safety backup makes the recovery reversible.
                    safety = dest.parent/'backups'/('before_recovery_'+str(time.time_ns())+'.sqlite3')
                    safety.parent.mkdir(exist_ok=True)
                    with closing(sqlite3.connect(dest.as_uri()+'?mode=rw',uri=True)) as target:
                        with closing(sqlite3.connect(safety)) as copy: target.backup(copy)
                        result = recover(source,target,source_id,verify,apply=True)
            else:
                with closing(sqlite3.connect(dest.as_uri()+'?mode=ro',uri=True)) as target:
                    result = recover(source,target,source_id,verify)
        print(json.dumps(result))
    finally:
        if rpc: rpc.w3.provider.session.close()


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('Historical recovery failed | category='+type(error).__name__)
        raise SystemExit(1) from None
