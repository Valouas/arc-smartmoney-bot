"""Append-only evidence archives. Never consulted by scoring or reconstruction."""
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

ROOT=Path(__file__).resolve().parent
ARCHIVE=ROOT/'data/evidence_archive'
OPERATIONS={'run_all_state.json','backup_status.json','nansen_bulk_status.json','alchemy_sync_status.json'}
MAIN_TABLES={'events','trade_context','signals','paper_positions','paper_fills','launch_candidates',
    'launches','launch_snapshots','wallet_profiles','discovery_winners','discovery_candidates',
    'candidate_sources','research_trades','promoted_wallets','wallet_activity','wallet_coverage',
    'trade_outcomes','reconstruction_gaps','historical_runs','historical_transactions',
    'historical_snapshots','historical_snapshot_transactions','historical_recovery_runs',
    'historical_recovery_transactions','external_wallet_candidates','external_wallet_metrics',
    'alchemy_deliveries','alchemy_items','alchemy_jobs','alchemy_paper_triggers'}
DATABASES={'arc_bot.sqlite3':MAIN_TABLES,'rpc_cache.sqlite3':{'rpc_cache_entries'},
    'nansen_bulk.sqlite3':{'pages','jobs'},'paper_copy.sqlite3':{'accounts','positions','fills','state'}}


def quoted(name): return '"'+name.replace('"','""')+'"'


def install_database(path, tables):
    """Additive SQLite triggers also protect INSERT OR REPLACE, without decoder changes."""
    path=Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=30)) as db:
        existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS retained_evidence_versions(
                table_name TEXT NOT NULL,row_json TEXT NOT NULL,archived_at INTEGER NOT NULL,
                UNIQUE(table_name,row_json))''')
            for table in sorted(tables & existing):
                columns=list(db.execute('PRAGMA table_info('+quoted(table)+')'))
                def encoded(prefix):
                    parts=[]
                    for col in columns:
                        name=col[1];ref=prefix+'.'+quoted(name)
                        parts.extend(["'"+name.replace("'","''")+"'",
                            "CASE WHEN typeof("+ref+")='blob' THEN json_object('__blob_hex__',hex("+ref+")) ELSE "+ref+' END'])
                    return 'json_object('+','.join(parts)+')'
                old,new=encoded('OLD'),encoded('NEW')
                literal="'"+table.replace("'","''")+"'"
                save='INSERT OR IGNORE INTO retained_evidence_versions SELECT '+literal+','+old+",CAST(strftime('%s','now') AS INTEGER)"
                for operation in ('DELETE','UPDATE'):
                    when=' WHEN '+old+' IS NOT '+new if operation=='UPDATE' else ''
                    db.execute('CREATE TRIGGER IF NOT EXISTS '+quoted('retain_'+table+'_'+operation.lower())+
                        ' BEFORE '+operation+' ON '+quoted(table)+when+' BEGIN '+save+'; END')
                keys=[]
                primary=[c[1] for c in sorted(columns,key=lambda c:c[5]) if c[5]]
                if primary: keys.append(primary)
                for index in db.execute('PRAGMA index_list('+quoted(table)+')').fetchall():
                    if index[2] and not index[4]:
                        cols=[r[2] for r in db.execute('PRAGMA index_info('+quoted(index[1])+')')]
                        if cols and all(cols):keys.append(cols)
                if keys:
                    matches=' OR '.join('('+' AND '.join('OLD.'+quoted(c)+'=NEW.'+quoted(c) for c in key)+')' for key in keys)
                    db.execute('CREATE TRIGGER IF NOT EXISTS '+quoted('retain_'+table+'_replace')+
                        ' BEFORE INSERT ON '+quoted(table)+' BEGIN '+save+' FROM '+quoted(table)+
                        ' AS OLD WHERE ('+matches+') AND '+old+' IS NOT '+new+'; END')


def install_all(root=ROOT):
    installed=[]
    for name,tables in DATABASES.items():
        path=Path(root)/'data'/name
        if path.exists():install_database(path,tables);installed.append(name)
    return installed


def should_archive(path,root=ROOT):
    path=Path(path).resolve();root=Path(root).resolve()
    try: relative=path.relative_to(root/'data')
    except ValueError:return False
    return (path.suffix.lower() in {'.json','.csv','.jsonl','.txt','.md'} and path.name not in OPERATIONS
            and not any(part in {'evidence_archive','automatic_backups','backups','recovery_forensics',
                                  'dex_cleanup_backups','launcher_logs'} for part in relative.parts))


def archive_file(source,logical_path=None,root=ROOT):
    """Content-addressed compressed files; catalogue publication follows a durable blob."""
    source=Path(source);logical=Path(logical_path or source).resolve();root=Path(root).resolve()
    if not source.exists(): return None
    relative=logical.relative_to(root).as_posix()
    directory=root/'data/evidence_archive';blobs=directory/'blobs';blobs.mkdir(parents=True,exist_ok=True)
    sha=hashlib.sha256();size=0
    with tempfile.NamedTemporaryFile(dir=blobs,prefix='.partial_',delete=False) as f:
        temp=Path(f.name)
        try:
            with gzip.GzipFile(fileobj=f,mode='wb',mtime=0) as compressed,source.open('rb') as stream:
                while chunk:=stream.read(1024*1024):
                    sha.update(chunk);size+=len(chunk);compressed.write(chunk)
            f.flush();os.fsync(f.fileno())
        except BaseException:
            f.close();temp.unlink(missing_ok=True);raise
    key=sha.hexdigest();dest=blobs/(key+'.gz')
    try:
        if dest.exists():temp.unlink()
        else:os.replace(temp,dest)
        with closing(sqlite3.connect(directory/'catalog.sqlite3',timeout=10)) as db,db:
            db.execute('''CREATE TABLE IF NOT EXISTS versions(path TEXT,sha256 TEXT,bytes INTEGER,
                archived_at INTEGER,PRIMARY KEY(path,sha256))''')
            db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?)',(relative,key,size,int(time.time())))
    finally:temp.unlink(missing_ok=True)
    return key


def before_replace(path,temporary,root=ROOT):
    if should_archive(path,root):
        archive_file(path,root=root)
        archive_file(temporary,logical_path=path,root=root)


def archive_api_response(provider,path,params,data,root=ROOT):
    if os.getenv('PRESERVE_RAW_API_RESPONSES','false').lower()!='true':return
    if provider not in {'arcscan','zerion','dune'}:raise ValueError('Unsupported evidence provider')
    # Only successful JSON and request scope; never auth headers, credentials or error bodies.
    request={'provider':provider,'path':path,'params':params}
    key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    directory=Path(root)/'data/evidence_archive';directory.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=directory,delete=False) as f:
        temp=Path(f.name)
        try:json.dump({'request':request,'response':data},f,allow_nan=False)
        except BaseException:f.close();temp.unlink(missing_ok=True);raise
    try:archive_file(temp,Path(root)/'data'/('raw_'+provider)/(key+'.json'),root)
    finally:temp.unlink(missing_ok=True)


def archive_existing(root=ROOT):
    root=Path(root);count=0
    for path in (root/'data').rglob('*'):
        if path.is_file() and should_archive(path,root):archive_file(path,root=root);count+=1
    return count


def verify_archive(root=ROOT):
    directory=Path(root)/'data/evidence_archive';p=(directory/'catalog.sqlite3').resolve()
    with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as db:
        rows=list(db.execute('SELECT DISTINCT sha256,bytes FROM versions'))
    for key,size in rows:
        sha=hashlib.sha256();read=0
        with gzip.open(directory/'blobs'/(key+'.gz'),'rb') as stream:
            while chunk:=stream.read(1024*1024):sha.update(chunk);read+=len(chunk)
        if sha.hexdigest()!=key or read!=size:raise ValueError('Evidence archive verification failed')
    return len(rows)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--verify',action='store_true')
    args=parser.parse_args()
    if args.verify:print('Verified evidence blobs:',verify_archive())
    else:
        print('Protected databases:',install_all())
        print('Existing files archived:',archive_existing())
