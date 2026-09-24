"""Idempotent cleanup of reviewed DEX addresses in wallet-derived state only.

Stop run_v4 before --apply. Default is a read-only preview. Historical trades,
events, swap evidence, signals, paper positions and checkpoints are untouched.
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from atomic_json import write_json
from config import SETTINGS
from process_lock import ProcessLock
from wallet_policy import infrastructure_addresses

WALLET_TABLES = ('discovery_candidates', 'candidate_sources', 'promoted_wallets', 'wallet_profiles')


def _filtered_snapshot(path, kind, excluded):
    if not path.exists():
        return None, 0
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('Invalid wallet snapshot; cleanup cancelled')
    if kind == 'wallets':
        rows = data.get('wallets')
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('address'), str) for r in rows):
            raise ValueError('Invalid generated watchlist; cleanup cancelled')
        kept = [r for r in rows if r['address'].lower() not in excluded]
    else:
        rows = data.get('profiles')
        if not isinstance(rows, dict) or any(not isinstance(r, dict) for r in rows.values()):
            raise ValueError('Invalid intelligence snapshot; cleanup cancelled')
        kept = {wallet:p for wallet,p in rows.items()
                if wallet.lower() not in excluded and str(p.get('wallet', '')).lower() not in excluded}
    data[kind] = kept
    return data, len(rows) - len(kept)


def cleanup(*, database='data/arc_bot.sqlite3', generated='generated_watchlist.json',
            intelligence='data/wallet_intelligence.json', cfg=None, apply=False):
    cfg = cfg or SETTINGS
    if not Path(cfg.trusted_dex_registry).is_file():
        raise ValueError('Configured trusted DEX registry must exist before cleanup')
    excluded = infrastructure_addresses(cfg)
    if not excluded:
        raise ValueError('No reviewed DEX emitter addresses; nothing to clean')
    database = Path(database).resolve()
    # The scanner lock refuses apply while bot.py owns the database. The two
    # other wallet writers must also be stopped; see the documented command.
    if apply:
        with ProcessLock(str(database) + '.scanner.lock'):
            return _cleanup(database, Path(generated), Path(intelligence), excluded, True)
    return _cleanup(database, Path(generated), Path(intelligence), excluded, False)


def _cleanup(database, generated, intelligence, excluded, apply):
    # Parse every file before touching SQLite; malformed files are not replaced
    # with an empty snapshot. Preserve unrelated rows and top-level metadata.
    snapshots = [(generated, *_filtered_snapshot(generated, 'wallets', excluded)),
                 (intelligence, *_filtered_snapshot(intelligence, 'profiles', excluded))]
    db = sqlite3.connect(database.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=10)
    addresses = tuple(sorted(excluded))
    marks = ','.join('?' for _ in addresses)
    result = {'apply': apply, 'emitters': list(addresses), 'tables': {}, 'files': {}, 'backup': None}
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in WALLET_TABLES:
            if table in tables:
                result['tables'][table] = db.execute(
                    f'SELECT COUNT(*) FROM {table} WHERE lower(wallet) IN ({marks})', addresses).fetchone()[0]
        result['files'] = {str(path): count for path, _, count in snapshots}
        if not apply or not (sum(result['tables'].values()) + sum(result['files'].values())):
            return result
        backup = database.parent / 'dex_cleanup_backups' / uuid4().hex
        backup.mkdir(parents=True)
        copy_db = sqlite3.connect(backup / database.name)
        try:
            db.backup(copy_db)
        finally:
            copy_db.close()
        # Distinct names even if callers chose identical file basenames.
        for index, (path, _, _) in enumerate(snapshots):
            if path.exists(): shutil.copy2(path, backup / f'{index}-{path.name}')
        result['backup'] = str(backup)
        with db:
            for table in result['tables']:
                db.execute(f'DELETE FROM {table} WHERE lower(wallet) IN ({marks})', addresses)
        # File replacement is atomic per file, not across SQLite and files.
        # The runtime exclusion protects stale exports; rerunning completes a
        # cleanup interrupted here without deleting any unrelated rows.
        for path, payload, count in snapshots:
            if count: write_json(path, payload)
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply cleanup after stopping all four processes')
    parser.add_argument('--database', default='data/arc_bot.sqlite3')
    parser.add_argument('--generated', default='generated_watchlist.json')
    parser.add_argument('--intelligence', default='data/wallet_intelligence.json')
    args = parser.parse_args()
    try:
        result = cleanup(**vars(args))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        # No file content, SQLite payload, credentials or raw exception text.
        print(f'Cleanup failed ({type(error).__name__}). Check registry, snapshots and stopped processes.')
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
