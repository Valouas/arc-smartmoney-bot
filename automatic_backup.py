"""Consistent, verified SQLite backups, independent of the mutable live database."""
import argparse
import gzip
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from atomic_json import write_json
from process_lock import ProcessLock

OWNER = 'arc_smartmoney_backups_v1'
SOURCES = (Path('data/arc_bot.sqlite3'), Path('data/paper_copy.sqlite3'), Path('data/rpc_cache.sqlite3'),
           Path('data/nansen_bulk.sqlite3'), Path('data/zerion_budget.sqlite3'),
           Path('data/rpc_rate_limit.sqlite3'), Path('data/paper_copy_validation.sqlite3'),
           Path('data/evidence_archive/catalog.sqlite3'))
STATUS = Path('data/backup_status.json')


def reparse(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 1024)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def backup_database(source, target, timeout):
    """Pin a WAL read snapshot, so ongoing writes cannot restart the copy forever."""
    deadline = time.monotonic() + timeout
    def progress(*_):
        if time.monotonic() >= deadline:
            raise TimeoutError('SQLite backup deadline exceeded')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
        src.execute('BEGIN')
        src.execute('SELECT count(*) FROM sqlite_master').fetchone()
        with closing(sqlite3.connect(target)) as out:
            src.backup(out, pages=1024, progress=progress, sleep=.05)
            out.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            if list(out.execute('PRAGMA quick_check')) != [('ok',)]:
                raise ValueError('Backup integrity check failed')
            out.set_progress_handler(None, 0)
            # A single-file archive, independent of the original WAL/SHM.
            out.execute('PRAGMA journal_mode=DELETE')
    with target.open('r+b') as stream:
        os.fsync(stream.fileno())
    return {'bytes': target.stat().st_size, 'sha256': digest(target), 'quick_check': 'ok'}


def evidence_entries(catalog):
    with closing(sqlite3.connect(catalog.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        entries=list(db.execute('SELECT DISTINCT sha256,bytes FROM versions'))
    if any(not re.fullmatch('[0-9a-f]{64}',key) or type(size) is not int or size<0 for key,size in entries):
        raise ValueError('Invalid evidence catalogue')
    return entries


def verify_blob(path,key,size):
    sha=hashlib.sha256();length=0
    with gzip.open(path,'rb') as stream:
        while chunk:=stream.read(1024*1024):sha.update(chunk);length+=len(chunk)
    if length!=size or sha.hexdigest()!=key:raise ValueError('Evidence checksum mismatch')


def backup_evidence(catalog,source,root):
    # Shared immutable blobs avoid duplicating every export in every hourly backup.
    target=root/'evidence_blobs';target.mkdir(exist_ok=True)
    entries=evidence_entries(catalog)
    for key,size in entries:
        src=source/'blobs'/(key+'.gz');dst=target/(key+'.gz')
        if not dst.exists():
            partial=target/('.partial_'+uuid.uuid4().hex)
            try:
                shutil.copyfile(src,partial)
                verify_blob(partial,key,size)
                with partial.open('r+b') as stream:os.fsync(stream.fileno())
                partial.replace(dst)
            finally:partial.unlink(missing_ok=True)
    return len(entries)


def generations(directory):
    root = Path(directory).resolve()
    if not root.exists():
        return []
    result = []
    for folder in root.glob('backup_*'):
        try:
            if reparse(folder) or folder.resolve().parent != root or not folder.is_dir():
                continue
            names = {p.name for p in folder.iterdir()}
            if any(reparse(p) or not p.is_file() for p in folder.iterdir()):
                continue
            manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            files = manifest.get('files', {})
            if (manifest.get('owner') != OWNER or manifest.get('complete') is not True
                    or not files or not set(files) <= {p.name for p in SOURCES}
                    or names != {'manifest.json', *files}
                    or not isinstance(manifest.get('completed_at'), (int, float))):
                continue
            if any((folder/name).stat().st_size != detail['bytes'] for name, detail in files.items()):
                continue
            result.append((folder, manifest))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda item: item[1]['completed_at'], reverse=True)


def prune(directory, keep_last, keep_daily):
    """Delete only this service's complete bundles; never traverse or recurse."""
    root = Path(directory).resolve()
    found = generations(root)
    keep = {p for p, _ in found[:keep_last]}
    days = set()
    for folder, manifest in found:
        day = datetime.fromtimestamp(manifest['completed_at'], timezone.utc).date()
        if day not in days and len(days) < keep_daily:
            keep.add(folder); days.add(day)
    for folder, manifest in found:
        if folder in keep:
            continue
        # Resolved paths stay inside the dedicated backup root. No recursive delete.
        if reparse(folder) or folder.resolve().parent != root:
            raise ValueError('Unsafe backup retention path')
        paths = [folder/name for name in [*manifest['files'], 'manifest.json']]
        if any(reparse(p) or p.resolve().parent != folder.resolve() for p in paths):
            raise ValueError('Unsafe backup retention file')
        for path in paths:
            path.unlink()
        folder.rmdir()


def create_backup(cfg, *, sources=SOURCES, now=None):
    root = Path(cfg.automatic_backup_directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    present = [Path(p) for p in sources if Path(p).exists()]
    fixed_time = now
    now = time.time() if now is None else now
    token = datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex
    stage, final = root/('.partial_' + token), root/('backup_' + token)
    with ProcessLock(root/'automatic_backup.lock'):
        previous = generations(root)
        if previous and set(previous[0][1]['files']) - {p.name for p in present}:
            raise FileNotFoundError('A previously protected database is missing')
        if not present:
            return None  # New installation: nothing exists to protect yet.
        needed = sum(p.stat().st_size + (Path(str(p)+'-wal').stat().st_size
                     if Path(str(p)+'-wal').exists() else 0) for p in present)
        needed += cfg.automatic_backup_reserve_mb * 1024**2
        if shutil.disk_usage(root).free < needed:
            raise OSError('Insufficient free space for a verified backup')
        stage.mkdir()
        try:
            manifest = {'owner': OWNER, 'complete': False, 'started_at': now, 'files': {},
                        'cross_database_transaction': False}
            for source in present:
                manifest['files'][source.name] = backup_database(source, stage/source.name, cfg.automatic_backup_timeout_seconds)
            if 'catalog.sqlite3' in manifest['files']:
                source=next(p.parent for p in present if p.name=='catalog.sqlite3')
                manifest['evidence_blobs']=backup_evidence(stage/'catalog.sqlite3',source,root)
            manifest.update(complete=True, completed_at=time.time() if fixed_time is None else fixed_time)
            write_json(stage/'manifest.json', manifest)
            stage.rename(final)  # Publish only the fully verified bundle.
        except BaseException:
            # Only remove files we just created, in our private staging directory.
            if stage.exists() and stage.resolve().parent == root and not reparse(stage):
                allowed = {p.name for p in present} | {p.name+s for p in present for s in ('-wal','-shm','-journal')} | {'manifest.json'}
                for path in stage.iterdir():
                    if path.name in allowed and path.is_file() and not reparse(path):
                        path.unlink()
                if not any(stage.iterdir()): stage.rmdir()
            raise
        # Prune only after the new, usable backup has been committed to disk.
        if not getattr(cfg,'automatic_backup_keep_all',True):
            prune(root, cfg.automatic_backup_keep_last, cfg.automatic_backup_keep_daily)
        write_json(STATUS, {'status':'OK', 'last_success':manifest['completed_at'],
                           'generation':final.name,'files':manifest['files']})
        return final


def due(cfg, now):
    found = generations(cfg.automatic_backup_directory)
    if not found: return True
    latest = found[0][1]
    return (now - latest['completed_at'] >= cfg.automatic_backup_interval_seconds
            or {p.name for p in SOURCES if p.exists()} != set(latest['files']))


def verify_backup(folder):
    """Verify a published archive without restoring over a live database."""
    folder = Path(folder).resolve()
    found = [(p,m) for p,m in generations(folder.parent) if p == folder]
    if not found: raise ValueError('Not a complete managed backup')
    manifest = found[0][1]
    for name, detail in manifest['files'].items():
        path = folder/name
        if digest(path) != detail['sha256']: raise ValueError('Backup checksum mismatch')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            if list(db.execute('PRAGMA quick_check')) != [('ok',)]:
                raise ValueError('Backup integrity check failed')
    if 'catalog.sqlite3' in manifest['files']:
        for key,size in evidence_entries(folder/'catalog.sqlite3'):
            verify_blob(folder.parent/'evidence_blobs'/(key+'.gz'),key,size)
    return manifest


def main():
    from config import SETTINGS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        verify_backup(args.verify)
        print('Sauvegarde verifiee (SHA-256 + SQLite). Aucune restauration sur la base active.')
        return
    if not SETTINGS.automatic_backup_enabled:
        return
    from data_preservation import install_all, archive_existing
    install_all()
    started = time.time()
    while True:
        if args.once or due(SETTINGS, time.time()):
            try:
                archive_existing()
                result = create_backup(SETTINGS)
                if result: print('Sauvegarde SQLite verifiee : ' + result.name, flush=True)
            except Exception as error:
                found = generations(SETTINGS.automatic_backup_directory)
                last = found[0][1]['completed_at'] if found else started
                write_json(STATUS, {'status':'FAILED', 'category':type(error).__name__,
                                   'last_success':last if found else None,'attempt_at':time.time()})
                print('Sauvegarde impossible | category='+type(error).__name__, flush=True)
                if args.once or time.time()-last >= SETTINGS.automatic_backup_max_age_seconds:
                    raise RuntimeError('Protection sauvegarde indisponible : arret du lanceur requis') from None
        if args.once: return
        time.sleep(30)


if __name__ == '__main__':
    main()
