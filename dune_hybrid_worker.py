"""Bounded raw-history prefetch. Reuses stored Nansen dates; no Nansen HTTP.

Only pending hashes of the existing campaign are requested. The consumer keeps
Arcscan completeness, canonical RPC headers, trusted DEX checks and its decoder.
"""
import argparse
from contextlib import closing
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time

import requests

from hybrid_history import SCHEMA, connect, import_result
from nansen_holding_campaign import readonly, write_new
from process_lock import ProcessLock
from provider_history_comparison import capture, load_response, hex_value
from copy_holding_campaign import snapshot

BASE='https://api.dune.com/api/v1/'


def prepare(directory,campaign,nansen,*,max_queries=20,max_credits=50):
    if type(max_queries) is not int or not 1<=max_queries<=20 or not math.isfinite(max_credits) or not 0<max_credits<=50:
        raise ValueError('bounded_initial_policy_required')
    out=Path(directory).resolve();out.mkdir(parents=True,exist_ok=False);(out/'backups').mkdir()
    campaign=Path(campaign).resolve();settings=json.loads((campaign/'settings.json').read_text())
    with closing(connect(out/'history.sqlite3')) as db,closing(readonly(nansen)) as src:
        db.executescript(SCHEMA+'CREATE TABLE hints(tx_hash TEXT PRIMARY KEY,day TEXT NOT NULL);')
        # Dates narrow a Dune lookup only. Missing/mistimed hints never prove that
        # a transaction was absent and can never advance a historical cursor.
        with db:
            for p in src.execute('SELECT body,digest FROM pages'):
                if hashlib.sha256(p['body']).hexdigest()!=p['digest']:raise ValueError('nansen_archive_integrity')
                for row in json.loads(p['body']).get('data',[]):
                    if row.get('chain')!='arc':continue
                    h=hex_value(row.get('transaction_hash'),64)
                    day=str(row.get('block_timestamp',''))[:10]
                    try:date.fromisoformat(day)
                    except ValueError:continue
                    db.execute('INSERT OR IGNORE INTO hints VALUES(?,?)',(h,day))
        snapshot(db,out/'backups','initial')
    write_new(out/'settings.json',dict(campaign=str(campaign),context=settings['context'],
        scope_sha256=settings['scope_sha256'],max_queries=max_queries,max_credits=max_credits,
        included_reserve=100,batch_size=100,created_at=time.time(),max_seconds=7200,
        nansen_http_calls=0,automatic_purchase=False))


def plan(db,campaign,limit=100):
    attempted={h for r in db.execute('SELECT hashes FROM jobs') for h in json.loads(r[0])}
    cached={r[0] for r in db.execute('SELECT tx_hash FROM bundles')}
    hints=dict(db.execute('SELECT tx_hash,day FROM hints'))
    hashes=[];day=None
    with closing(readonly(campaign/'campaign.sqlite3')) as source:
        rows=source.execute('''SELECT t.tx_hash FROM historical_transactions t
          JOIN campaign_queue q ON q.wallet=t.wallet JOIN historical_runs h ON h.wallet=q.wallet
          WHERE t.payload IS NULL AND q.state='PENDING' AND h.version=1
          ORDER BY CASE WHEN h.phase='DECODING' THEN 0 ELSE 1 END,q.priority,t.tx_hash''')
        for row in rows:
            h=row[0]
            if h in attempted or h in cached or h not in hints or h in hashes:continue
            if day is None:day=hints[h]
            if hints[h]!=day:continue
            hashes.append(hex_value(h,64))
            if len(hashes)>=limit:break
    return hashes,day


def query(hashes,day):
    if not 1<=len(hashes)<=100:raise ValueError('batch_limit')
    ids=','.join(hex_value(h,64) for h in hashes)
    d=date.fromisoformat(day);start=(d-timedelta(days=1)).isoformat();end=(d+timedelta(days=1)).isoformat()
    return f'''WITH t AS (
 SELECT * FROM arc.transactions WHERE block_date BETWEEN DATE '{start}' AND DATE '{end}' AND hash IN ({ids})
), l AS (
 SELECT * FROM arc.logs WHERE block_date BETWEEN DATE '{start}' AND DATE '{end}' AND tx_hash IN ({ids})
), n AS (SELECT tx_hash,count(*) AS log_count FROM l GROUP BY tx_hash)
SELECT 'tx' kind,cast(t.hash as varchar) tx_hash,cast(t.block_hash as varchar) block_hash,
 t.block_number,cast(t.block_time as varchar) block_time,t.index tx_index,
 cast(t."from" as varchar) sender,cast(t."to" as varchar) recipient,cast(t.value as varchar) raw_value,
 t.gas_used,cast(t.gas_price as varchar) gas_price,t.success,t.chain_id,coalesce(n.log_count,0) log_count,
 CAST(NULL AS bigint) log_index,CAST(NULL AS varchar) emitter,CAST(NULL AS varchar) topic0,
 CAST(NULL AS varchar) topic1,CAST(NULL AS varchar) topic2,CAST(NULL AS varchar) topic3,CAST(NULL AS varchar) data
FROM t LEFT JOIN n ON n.tx_hash=t.hash
UNION ALL
SELECT 'log',cast(tx_hash as varchar),cast(block_hash as varchar),block_number,cast(block_time as varchar),tx_index,
 NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,index,cast(contract_address as varchar),
 cast(topic0 as varchar),cast(topic1 as varchar),cast(topic2 as varchar),cast(topic3 as varchar),cast(data as varchar)
FROM l WHERE tx_hash IN (SELECT tx_hash FROM n WHERE log_count<=128)'''


def publish(db,out):
    report=dict(updated_at=time.time(),jobs=[dict(r) for r in db.execute('SELECT id,state,cost,error FROM jobs')],
                bundles=db.execute('SELECT count(*) FROM bundles').fetchone()[0],
                nansen_http_calls=0,history_authority=False)
    tmp=out/'status.tmp';tmp.write_text(json.dumps(report,indent=2),encoding='utf-8');tmp.replace(out/'status.json')
    print(json.dumps(report),flush=True)


def usage(session,headers,folder,name):
    capture(session,'POST',BASE+'usage',headers,folder,name,{})
    d=load_response(folder,name);today=time.strftime('%Y-%m-%d',time.gmtime())
    periods=[p for p in d['billing_periods'] if p['start_date']<=today<p['end_date']]
    if len(periods)!=1:raise ValueError('unknown_credit_period')
    return periods[0]


def fetch_result(session,headers,folder,execution):
    """Resume result pagination from archived pages; never re-execute SQL."""
    rows=[];offset=0;total=None
    for _ in range(20):
        name='result' if offset==0 else f'result_{offset}'
        if not (folder/(name+'.response.json')).exists():
            capture(session,'GET',BASE+f'execution/{execution}/results',headers,folder,name,
                    params={'limit':10000,'offset':offset})
        d=load_response(folder,name)
        meta=d.get('result',{}).get('metadata',{});page=d.get('result',{}).get('rows')
        if d.get('execution_id')!=execution or d.get('state')!='QUERY_STATE_COMPLETED' or not isinstance(page,list):
            raise ValueError('invalid_result_page')
        n=meta.get('total_row_count')
        if type(n) is not int or n<0 or n>200000 or (total is not None and n!=total):
            raise ValueError('inconsistent_result_count')
        total=n
        if meta.get('row_count')!=len(page) or len(page)>10000:raise ValueError('invalid_page_count')
        rows.extend(page);end=offset+len(page);nxt=d.get('next_offset')
        if end>total:raise ValueError('result_overflow')
        if nxt is None:
            if end!=total:raise ValueError('truncated_result')
            return dict(state='QUERY_STATE_COMPLETED',execution_id=execution,
                        result={'rows':rows,'metadata':{'total_row_count':total}})
        if not page or type(nxt) is not int or nxt!=end or end>=total:raise ValueError('pagination_cycle_or_gap')
        offset=nxt
    raise ValueError('result_page_limit')


def finish_job(db,s,headers,out,job):
    folder=out/f"job_{job['id']:04d}"
    result=fetch_result(s,headers,folder,job['execution'])
    imported=import_result(db,result,json.loads(job['hashes']),str(folder))
    if not (folder/'import.json').exists():write_new(folder/'import.json',imported)
    if not (folder/'usage_after.response.json').exists():
        usage(s,headers,folder,'usage_after')
    before=load_response(folder,'usage_before')['billing_periods']
    after=load_response(folder,'usage_after')['billing_periods']
    by_period={p['start_date']:p for p in after}
    deltas=[max(0,by_period[p['start_date']]['credits_used']-p['credits_used']) for p in before if p['start_date'] in by_period]
    with db:db.execute("UPDATE jobs SET state='COMPLETE',cost=max(cost,?) WHERE id=?",(sum(deltas),job['id']))
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file() and p.name!='manifest.json'}
    if not (folder/'manifest.json').exists():write_new(folder/'manifest.json',manifest)
    archive=out/'backups'/folder.name
    if not archive.exists():shutil.copytree(folder,archive)
    if any(hashlib.sha256((archive/name).read_bytes()).hexdigest()!=digest for name,digest in manifest.items()):
        raise ValueError('evidence_backup_integrity')
    snapshot(db,out/'backups','job');publish(db,out)


def resume_results(directory,key):
    """Explicit recovery of completed queries only; no fresh SQL execution."""
    out=Path(directory).resolve()
    with ProcessLock(out/'worker.lock'),closing(connect(out/'history.sqlite3')) as db,requests.Session() as s:
        snapshot(db,out/'backups','before_result_resume')
        for job in db.execute("SELECT * FROM jobs WHERE state='SUBMITTED'").fetchall():
            folder=out/f"job_{job['id']:04d}"
            statuses=sorted(folder.glob('status_*.response.json'),key=lambda p:int(p.name.split('_')[1].split('.')[0]))
            if not statuses:raise ValueError('completion_unproven')
            last=load_response(folder,statuses[-1].name.removesuffix('.response.json'))
            if last['state']!='QUERY_STATE_COMPLETED' or last['execution_id']!=job['execution']:raise ValueError('completion_unproven')
            finish_job(db,s,{'X-Dune-Api-Key':key},out,job)


def run(directory,key,*,once=False):
    if not key:raise ValueError('missing_dune_key')
    out=Path(directory).resolve();cfg=json.loads((out/'settings.json').read_text());campaign=Path(cfg['campaign'])
    source_cfg=json.loads((campaign/'settings.json').read_text())
    if any(cfg[k]!=source_cfg[k] for k in ('context','scope_sha256')):raise ValueError('campaign_changed')
    if hashlib.sha256((campaign/'scope.json').read_bytes()).hexdigest()!=cfg['scope_sha256']:raise ValueError('scope_changed')
    if (type(cfg['max_queries']) is not int or not 1<=cfg['max_queries']<=20 or
            not math.isfinite(cfg['max_credits']) or not 0<cfg['max_credits']<=50 or cfg['batch_size']!=100):
        raise ValueError('invalid_collection_limits')
    headers={'X-Dune-Api-Key':key}
    with ProcessLock(out/'worker.lock'),closing(connect(out/'history.sqlite3')) as db,requests.Session() as s:
        # Intent/crash recovery never resubmits a paid query automatically.
        if db.execute("SELECT 1 FROM jobs WHERE state='SUBMITTED'").fetchone():raise ValueError('uncertain_previous_job_review_required')
        try:
            while not (out/'STOP').exists() and time.time()<cfg['created_at']+cfg['max_seconds']:
                count,cost=db.execute('SELECT count(*),coalesce(sum(cost),0) FROM jobs').fetchone()
                if count>=cfg['max_queries'] or cost>=cfg['max_credits']:break
                hashes,day=plan(db,campaign,cfg['batch_size'])
                if not hashes:break
                folder=out/f'job_{count+1:04d}';folder.mkdir(exist_ok=False)
                u=usage(s,headers,folder,'usage_before')
                if u['credits_included']-u['credits_used']<cfg['included_reserve']+cfg['max_credits']-cost:break
                with db:db.execute("INSERT INTO jobs(id,state,hashes) VALUES(?,'SUBMITTED',?)",(count+1,json.dumps(hashes)))
                sql=query(hashes,day);(folder/'query.sql').write_text(sql,encoding='utf-8')
                capture(s,'POST',BASE+'sql/execute',headers,folder,'execute',{'sql':sql,'performance':'medium'})
                d=load_response(folder,'execute');e=d['execution_id']
                if not re.fullmatch('[A-Z0-9]{26}',e):raise ValueError('invalid_execution')
                with db:db.execute('UPDATE jobs SET execution=? WHERE id=?',(e,count+1))
                deadline=time.monotonic()+120;poll=0;finished=False
                while time.monotonic()<deadline and not (out/'STOP').exists():
                    time.sleep(3)
                    capture(s,'GET',BASE+f'execution/{e}/status',headers,folder,f'status_{poll}')
                    d=load_response(folder,f'status_{poll}');poll+=1
                    c=d.get('execution_cost_credits',0)
                    if not isinstance(c,(float,int)) or not math.isfinite(c) or c<0:raise ValueError('unknown_cost')
                    with db:db.execute('UPDATE jobs SET cost=max(cost,?) WHERE id=?',(c,count+1))
                    if d.get('is_execution_finished'):
                        finished=True;break
                    if c>=cfg['max_credits']-cost:break
                if not finished:
                    capture(s,'POST',BASE+f'execution/{e}/cancel',headers,folder,'cancel',{})
                    # Retain SUBMITTED: cancellation is not proof of final billing.
                    raise ValueError('query_cancelled_review_required')
                if d['state']!='QUERY_STATE_COMPLETED':
                    with db:db.execute("UPDATE jobs SET state='FAILED',error='query_failed' WHERE id=?",(count+1,))
                    break
                finish_job(db,s,headers,out,db.execute('SELECT * FROM jobs WHERE id=?',(count+1,)).fetchone())
                if once:break
        except Exception:
            # A polling/transport failure must not leave an unbounded query
            # running deliberately. Cancellation itself may be uncertain; keep
            # SUBMITTED and all reservations for explicit operator recovery.
            pending=db.execute("SELECT * FROM jobs WHERE state='SUBMITTED' AND execution IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            if pending:
                folder=out/f"job_{pending['id']:04d}"
                try:
                    name='cancel_on_error_'+str(time.time_ns())
                    capture(s,'POST',BASE+f"execution/{pending['execution']}/cancel",headers,folder,name,{})
                except Exception:pass
            raise
        finally:
            snapshot(db,out/'backups','exit');publish(db,out)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','run','resume-results'])
    p.add_argument('--directory',required=True);p.add_argument('--campaign');p.add_argument('--nansen-database')
    p.add_argument('--once',action='store_true')
    a=p.parse_args()
    if a.action=='prepare':prepare(a.directory,a.campaign,a.nansen_database)
    else:
        from config import SETTINGS
        if a.action=='resume-results':resume_results(a.directory,SETTINGS.dune_api_key)
        else:run(a.directory,SETTINGS.dune_api_key,once=a.once)


if __name__=='__main__':main()
