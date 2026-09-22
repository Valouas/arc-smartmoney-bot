"""Bounded supplemental Nansen enumeration; no history-completeness authority."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import requests

from nansen_transactions_probe import URL, validate_page, timestamp
from process_lock import ProcessLock


SCHEMA = '''
CREATE TABLE policy(id INTEGER PRIMARY KEY CHECK(id=1),cap INTEGER NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,used INTEGER NOT NULL DEFAULT 0,halt TEXT);
CREATE TABLE scope(wallet TEXT PRIMARY KEY,priority INTEGER,cutoff_block INTEGER,
 cutoff_hash TEXT,context TEXT,date_from TEXT,date_to TEXT,page INTEGER DEFAULT 1,
 state TEXT DEFAULT 'PENDING',reason TEXT);
CREATE TABLE attempts(id INTEGER PRIMARY KEY,wallet TEXT,page INTEGER,request TEXT,
 state TEXT NOT NULL,status INTEGER,billing TEXT,body BLOB,digest TEXT);
CREATE TABLE pages(wallet TEXT,page INTEGER,body BLOB,digest TEXT,origin TEXT,
 PRIMARY KEY(wallet,page));
CREATE TABLE seeds(wallet TEXT,tx_hash TEXT,PRIMARY KEY(wallet,tx_hash));
'''


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def connect(path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2, allow_nan=False)


def request_body(row):
    return dict(chain='arc',address=row['wallet'],hide_spam_token=False,
                date={'from':row['date_from'],'to':row['date_to']},
                pagination={'page':row['page'],'per_page':100},
                order_by=[{'field':'block_timestamp','direction':'ASC'}])


def prepare(campaign, directory, cap, archives=()):
    if type(cap) is not int or not 1 <= cap <= 2000:
        raise ValueError('maximum_2000_additional_attempts')
    campaign = Path(campaign).resolve()
    settings = json.loads((campaign/'settings.json').read_text())
    with closing(readonly(campaign/'campaign.sqlite3')) as src:
        rows = src.execute('''SELECT q.wallet,q.priority,h.cutoff_block,h.cutoff_hash,h.cutoff_at
          FROM campaign_queue q JOIN historical_runs h ON h.wallet=q.wallet
          WHERE q.state='PENDING' AND h.phase='FETCHING' AND h.version=1 ORDER BY q.priority''').fetchall()
    if not rows or len(rows)>1512:
        raise ValueError('invalid_target_scope')
    out=Path(directory).resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'evidence').mkdir();(out/'backups').mkdir()
    with closing(connect(out/'campaign.sqlite3')) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute('INSERT INTO policy(id,cap) VALUES(1,?)',(cap,))
            for r in rows:
                until=datetime.fromtimestamp(r['cutoff_at'],timezone.utc).isoformat()
                if timestamp(until)<=timestamp('2026-07-15T00:00:00Z'):
                    continue
                db.execute('''INSERT INTO scope(wallet,priority,cutoff_block,cutoff_hash,context,date_from,date_to)
                  VALUES(?,?,?,?,?,?,?)''',(r['wallet'],r['priority'],r['cutoff_block'],r['cutoff_hash'],
                  settings['context'],'2026-07-15T00:00:00Z',until))
        # Prior paid pages are imported as cached evidence, never counted as new calls.
        for archive in archives:
            root=Path(archive)
            manifest=json.loads((root/'manifest.json').read_text())
            for name,digest in manifest.items():
                if Path(name).name!=name or hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:
                    raise ValueError('archive_integrity')
            for p in sorted(root.glob('attempt_*.json')):
                body=json.loads(p.read_text())['request'];w=body['address']
                r=db.execute('SELECT * FROM scope WHERE wallet=?',(w,)).fetchone()
                if not r:continue
                expected=request_body(dict(r,page=body['pagination']['page']))
                # Normalize equivalent UTC date encodings without weakening the interval.
                candidate=dict(body,date=expected['date'])
                if candidate!=expected or any(timestamp(body['date'][k])!=timestamp(expected['date'][k]) for k in ('from','to')):
                    continue
                suffix=p.stem.split('_')[-1]
                meta=json.loads((root/f'response_{suffix}.json').read_text())
                if meta['status']!=200:continue
                raw=(root/f'response_{suffix}.body').read_bytes()
                with db:
                    db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?)',
                      (w,body['pagination']['page'],raw,hashlib.sha256(raw).hexdigest(),str(root)))
        write_new(out/'settings.json',dict(schema_version=1,campaign=str(campaign),cap=cap,
            created_at=time.time(),max_seconds=7200,context=settings['context'],
            additional_budget=True,no_automatic_purchase=True))
        backup(db,out)
        publish(db,out)


def backup(db,out):
    p=Path(out)/'backups'/f'checkpoint_{time.time_ns()}.sqlite3'
    with closing(sqlite3.connect(p)) as dst:db.backup(dst)
    write_new(p.with_suffix('.sha256.json'),dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest()))


def publish(db,out):
    policy=dict(db.execute('SELECT * FROM policy').fetchone())
    data=dict(at=time.time(),**policy,states=dict(db.execute('SELECT state,count(*) FROM scope GROUP BY state')),
              pages=db.execute('SELECT count(*) FROM pages').fetchone()[0],
              unique_wallet_transactions=db.execute('SELECT count(*) FROM seeds').fetchone()[0],
              history_completeness_authority=False,paper_selected=False)
    tmp=Path(out)/'status.tmp';tmp.write_text(json.dumps(data,indent=2),encoding='utf-8')
    tmp.replace(Path(out)/'status.json')
    print(json.dumps(data),flush=True)
    return data


def eligible(source,row):
    with closing(readonly(source)) as db:
        r=db.execute('''SELECT q.state,h.phase,h.version,h.cutoff_block,h.cutoff_hash
          FROM campaign_queue q JOIN historical_runs h ON h.wallet=q.wallet WHERE q.wallet=?''',(row['wallet'],)).fetchone()
    return bool(r and r['state']=='PENDING' and r['phase']=='FETCHING' and r['version']==1
                and r['cutoff_block']==row['cutoff_block'] and r['cutoff_hash']==row['cutoff_hash'])


def reserve(db,row):
    with db:
        db.execute('BEGIN IMMEDIATE')
        p=db.execute('SELECT * FROM policy').fetchone()
        if p['halt'] or p['attempts']>=p['cap']:raise ValueError('budget_or_halt')
        if db.execute("SELECT 1 FROM attempts WHERE state='INFLIGHT'").fetchone():
            raise ValueError('uncertain_previous_attempt')
        attempt=p['attempts']+1
        db.execute('UPDATE policy SET attempts=?',(attempt,))
        db.execute('INSERT INTO attempts(id,wallet,page,request,state) VALUES(?,?,?,?,?)',
                   (attempt,row['wallet'],row['page'],json.dumps(request_body(row)),'INFLIGHT'))
    return attempt


def fetch(db,out,row,key,session):
    attempt=reserve(db,row)
    path=Path(out)/'evidence'/f'response_{attempt:04d}.body'
    status=None;billing={};raw=bytearray();failure=None
    try:
        with session.post(URL,headers={'apikey':key,'Accept':'application/json'},json=request_body(row),
                          timeout=(5,20),allow_redirects=False,stream=True) as response:
            status=response.status_code
            billing={k.lower():v for k,v in response.headers.items() if k.lower().startswith('x-nansen-credits-')}
            # Persist partial bytes even when receiving the stream fails.
            with path.open('xb') as f:
                for chunk in response.iter_content(16384):
                    f.write(chunk);f.flush();raw.extend(chunk)
                    if len(raw)>4*1024*1024:raise ValueError('payload_size_limit')
    except requests.RequestException:
        failure='transport_uncertain'
    except ValueError:
        failure='payload_size_limit'
    digest=hashlib.sha256(raw).hexdigest()
    with db:
        db.execute('UPDATE attempts SET state=?,status=?,billing=?,body=?,digest=? WHERE id=?',
                   ('RECEIVED' if not failure else 'UNCERTAIN',status,json.dumps(billing),bytes(raw),digest,attempt))
        try:
            cost=int(billing['x-nansen-credits-cost']);used=int(billing['x-nansen-credits-used'])
            if min(cost,used)<0:raise ValueError()
            db.execute('UPDATE policy SET used=used+?',(used,))
            if max(cost,used)>1:raise ValueError()
        except (KeyError,ValueError,TypeError):
            failure=failure or 'billing_unverified_or_changed'
        if status!=200:failure=failure or 'http_'+str(status)
        if failure:db.execute('UPDATE policy SET halt=?',(failure,))
    write_new(path.with_suffix('.json'),dict(at=time.time(),status=status,billing=billing,sha256=digest,failure=failure))
    if failure:raise ValueError(failure)
    with db:
        db.execute('INSERT INTO pages VALUES(?,?,?,?,?)',
                   (row['wallet'],row['page'],bytes(raw),digest,'request_'+str(attempt)))
        if int(billing.get('x-nansen-credits-remaining','1000000'))<=100:
            db.execute("UPDATE policy SET halt='account_reserve_100'")
    return bytes(raw)


def accept(db,row,raw):
    records,last=validate_page(json.loads(raw),row['page'],timestamp(row['date_from']),timestamp(row['date_to']),True)
    hashes=[r['tx_hash'] for r in records]
    if len(set(hashes))!=len(hashes):raise ValueError('duplicate_page_hash')
    with db:
        if any(db.execute('SELECT 1 FROM seeds WHERE wallet=? AND tx_hash=?',(row['wallet'],h)).fetchone() for h in hashes):
            raise ValueError('duplicate_cross_page_hash')
        db.executemany('INSERT INTO seeds VALUES(?,?)',((row['wallet'],h) for h in hashes))
        db.execute('UPDATE scope SET page=page+1,state=? WHERE wallet=?',
                   ('PAGES_COMPLETE' if last else 'PENDING',row['wallet']))


def recover_received(db):
    """A crash between response persistence and indexing must not pay twice."""
    with db:
        for r in db.execute("SELECT * FROM attempts WHERE state='RECEIVED' AND status=200").fetchall():
            if r['body'] is None or hashlib.sha256(r['body']).hexdigest()!=r['digest']:
                raise ValueError('received_integrity')
            billing=json.loads(r['billing'])
            if not all(0<=int(billing[k])<=1 for k in ('x-nansen-credits-cost','x-nansen-credits-used')):
                raise ValueError('received_billing')
            db.execute('INSERT OR IGNORE INTO pages VALUES(?,?,?,?,?)',
                       (r['wallet'],r['page'],r['body'],r['digest'],'request_'+str(r['id'])))


def read_seeds(path,wallet,run,context):
    """Advisory hashes only, tied to the unchanged local historical generation."""
    with closing(readonly(path)) as db:
        r=db.execute('SELECT * FROM scope WHERE wallet=?',(wallet,)).fetchone()
        if not r or r['context']!=context or r['cutoff_block']!=run['cutoff_block'] or r['cutoff_hash']!=run['cutoff_hash']:
            return []
        return [x[0] for x in db.execute('SELECT tx_hash FROM seeds WHERE wallet=? ORDER BY tx_hash',(wallet,))]


def ready_wallet(db,path,context,now):
    """Find useful staged work; caller reserves alternating turns for normal FIFO."""
    with closing(readonly(path)) as seeds:
        candidates=seeds.execute('''SELECT s.* FROM scope s WHERE EXISTS(
          SELECT 1 FROM seeds t WHERE t.wallet=s.wallet) ORDER BY s.priority''').fetchall()
        for r in candidates:
            if r['context']!=context:continue
            q=db.execute('''SELECT q.* FROM campaign_queue q JOIN historical_runs h ON h.wallet=q.wallet
              WHERE q.wallet=? AND q.state='PENDING' AND q.next_attempt<=? AND h.phase='FETCHING'
              AND h.version=1 AND h.cutoff_block=? AND h.cutoff_hash=?''',
              (r['wallet'],now,r['cutoff_block'],r['cutoff_hash'])).fetchone()
            if not q:continue
            decoded={x[0] for x in db.execute('SELECT tx_hash FROM historical_transactions WHERE wallet=? AND payload IS NOT NULL',(r['wallet'],))}
            if any(x[0] not in decoded for x in seeds.execute('SELECT tx_hash FROM seeds WHERE wallet=?',(r['wallet'],))):
                return q
    return None


def run(directory,key):
    if not key:raise ValueError('missing_nansen_key')
    out=Path(directory).resolve();cfg=json.loads((out/'settings.json').read_text())
    with ProcessLock(out/'worker.lock'),closing(connect(out/'campaign.sqlite3')) as db,requests.Session() as session:
        last_backup=time.monotonic();last_request=0
        if db.execute("SELECT 1 FROM attempts WHERE state IN ('INFLIGHT','UNCERTAIN')").fetchone():
            with db:db.execute("UPDATE policy SET halt=COALESCE(halt,'uncertain_previous_attempt')")
        try:
            if not db.execute('SELECT halt FROM policy').fetchone()[0]:recover_received(db)
            while time.time()<cfg['created_at']+cfg['max_seconds'] and not (out/'STOP').exists():
                p=db.execute('SELECT * FROM policy').fetchone()
                if p['cap']!=cfg['cap']:raise ValueError('policy_changed')
                if p['halt'] or p['attempts']>=p['cap']:break
                row=db.execute("SELECT * FROM scope WHERE state='PENDING' ORDER BY page,priority LIMIT 1").fetchone()
                if not row:break
                if not eligible(Path(cfg['campaign'])/'campaign.sqlite3',row):
                    with db:db.execute("UPDATE scope SET state='SKIPPED_LOCAL_PROGRESS' WHERE wallet=?",(row['wallet'],))
                    continue
                cached=db.execute('SELECT body,digest FROM pages WHERE wallet=? AND page=?',(row['wallet'],row['page'])).fetchone()
                try:
                    if cached:
                        raw=cached['body']
                        if hashlib.sha256(raw).hexdigest()!=cached['digest']:raise ValueError('cached_integrity')
                    else:
                        time.sleep(max(0,1-(time.monotonic()-last_request)));last_request=time.monotonic()
                        raw=fetch(db,out,row,key,session)
                    accept(db,row,raw)
                except (ValueError,TypeError,KeyError):
                    with db:db.execute("UPDATE scope SET state='REVIEW_REQUIRED',reason='provider_or_schema_error' WHERE wallet=?",(row['wallet'],))
                    if db.execute('SELECT halt FROM policy').fetchone()[0]:break
                publish(db,out)
                if time.monotonic()-last_backup>300:backup(db,out);last_backup=time.monotonic()
        finally:
            backup(db,out);data=publish(db,out)
            write_new(out/f'run_{time.time_ns()}.json',data)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','run','status'])
    p.add_argument('--directory',required=True);p.add_argument('--campaign')
    p.add_argument('--max-credits',type=int);p.add_argument('--archive',action='append',default=[])
    a=p.parse_args()
    if a.action=='prepare':
        if not a.campaign or a.max_credits is None:p.error('--campaign and --max-credits required')
        prepare(a.campaign,a.directory,a.max_credits,a.archive)
    elif a.action=='status':
        with closing(readonly(Path(a.directory)/'campaign.sqlite3')) as db:publish(db,a.directory)
    else:
        from config import SETTINGS
        run(a.directory,SETTINGS.nansen_api_key)


if __name__=='__main__':main()
