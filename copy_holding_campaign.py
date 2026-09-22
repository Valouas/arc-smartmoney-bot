"""Resumable public-only holding-time research; never writes production authority."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from arcscan import ArcscanClient
from copy_candidate_report import shortlist
from copy_holding_duration import stored_holding
from copy_holding_pilot import ARC_URL, PUBLIC_RPC_URLS, Budget, BoundedSession, PilotCache, PilotLimit, save_new
from historical_collector import HistoricalCollector, parse_page
from historical_snapshots import decoding_context
from historical_store import SCHEMA
from process_lock import ProcessLock
from rpc_transport import RPCError, ResilientRPCProvider, SharedRateLimiter

QUEUE = """
CREATE TABLE campaign_queue (
 wallet TEXT PRIMARY KEY, priority INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 turns INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, reason TEXT, result TEXT);
CREATE TABLE campaign_budget(service TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0);
INSERT INTO campaign_budget VALUES('rpc',0),('arcscan',0);
"""


def connect(path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row
    return db


def snapshot(db, directory, label):
    destination=Path(directory)/f'{label}_{time.time_ns()}.sqlite3'
    with closing(sqlite3.connect(destination)) as saved:db.backup(saved)
    save_new(destination.with_suffix('.sha256.json'),dict(file=destination.name,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest()))
    return destination


def prepare(source, scope, directory, cfg, *, rpc_cap=200000, arc_cap=10000):
    wallets=shortlist(scope)
    if not wallets or len(wallets)>1512:raise ValueError('campaign_scope_size')
    from wallet_policy import infrastructure_addresses
    if set(wallets)&infrastructure_addresses(cfg):raise ValueError('infrastructure_in_scope')
    out=Path(directory).resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'backups').mkdir()
    scope_bytes=Path(scope).read_bytes();(out/'scope.json').write_bytes(scope_bytes)
    context=decoding_context(cfg)
    db=connect(out/'campaign.sqlite3');db.executescript(SCHEMA+QUEUE)
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src:
        src.row_factory=sqlite3.Row;src.execute('PRAGMA query_only=ON');src.execute('BEGIN')
        with db:
            for rank,w in enumerate(wallets):
                db.execute('INSERT INTO campaign_queue(wallet,priority) VALUES(?,?)',(w,rank))
                run=src.execute('SELECT * FROM historical_runs WHERE wallet=?',(w,)).fetchone()
                if run:
                    keys=','.join(run.keys())
                    db.execute(f"INSERT INTO historical_runs({keys}) VALUES({','.join('?' for _ in run)})",tuple(run))
                db.executemany('INSERT INTO historical_transactions VALUES(?,?,?)',
                    src.execute('SELECT wallet,tx_hash,payload FROM historical_transactions WHERE wallet=?',(w,)))
                context_row=src.execute('SELECT context FROM historical_refresh_context WHERE wallet=?',(w,)).fetchone()
                reason=None
                if not run:reason='MISSING_HISTORY_GENERATION'
                elif run['version']!=1:reason='INVALIDATED_HISTORY_REQUIRES_REVIEW'
                elif run['phase'] not in ('FETCHING','DECODING','COMPLETE'):reason='UNSUPPORTED_HISTORY_PHASE'
                elif not context_row or context_row[0]!=context:reason='DECODER_CONTEXT_REQUIRES_REVIEW'
                if reason:db.execute("UPDATE campaign_queue SET state='REVIEW_REQUIRED',reason=? WHERE wallet=?",(reason,w))
    snapshot(db,out/'backups','initial')
    save_new(out/'settings.json',dict(schema_version=1,scope_sha256=hashlib.sha256(scope_bytes).hexdigest(),
        wallets=len(wallets),context=context,rpc_cap=rpc_cap,arcscan_cap=arc_cap,
        created_at=time.time(),max_seconds=72*3600,public_requests_per_second=1,
        nansen_calls=0,alchemy_calls=0,production_database_modified=False))
    status(db,out);db.close()


class CampaignBudget(Budget):
    """Counts are reserved in SQLite before every HTTP attempt, across restarts."""
    def __init__(self,directory,db,settings):
        super().__init__(directory,settings['rpc_cap'],settings['arcscan_cap'],settings['max_seconds'])
        self.db=db
        self.counts=Counter({r['service']:r['attempts'] for r in db.execute('SELECT * FROM campaign_budget')})
        self.deadline=time.monotonic()+max(0,settings['created_at']+settings['max_seconds']-time.time())

    def take(self,service,method):
        if time.monotonic()>=self.deadline:raise PilotLimit()
        with self.db:
            changed=self.db.execute('UPDATE campaign_budget SET attempts=attempts+1 WHERE service=? AND attempts<?',
                (service,self.limits[service])).rowcount
        if not changed:raise PilotLimit()
        self.counts[service]+=1;self.methods[method]+=1


def check_anchor(rpc,run):
    h=rpc.get_block(run['cutoff_block'],False)['hash'].hex().removeprefix('0x')
    if h.lower()!=run['cutoff_hash'].lower().removeprefix('0x'):raise ValueError('campaign_anchor_mismatch')


def advance(db,wallet,rpc,arc,*,batch=10):
    """One fully validated page or a small decoder batch. Commit each successful tx."""
    run=dict(db.execute('SELECT * FROM historical_runs WHERE wallet=?',(wallet,)).fetchone())
    if run['version']!=1:raise ValueError('campaign_invalid_generation')
    # Canonical reads are deliberately outside the provider historical cache context.
    check_anchor(rpc,run)
    if run['phase']=='FETCHING':
        seen=json.loads(run['seen'])
        page=arc.address_activity(wallet,limit=100,cursor=run['cursor'])
        hashes,nxt=parse_page(page,run['cutoff_block'],seen)
        with db:
            db.executemany('INSERT OR IGNORE INTO historical_transactions VALUES(?,?,NULL)',((wallet,h) for h in hashes))
            db.execute('UPDATE historical_runs SET cursor=?,seen=?,phase=?,error=NULL WHERE wallet=?',
                (nxt,json.dumps(seen+[nxt] if nxt else seen),'FETCHING' if nxt else 'DECODING',wallet))
        return None
    missing=db.execute('SELECT tx_hash FROM historical_transactions WHERE wallet=? AND payload IS NULL ORDER BY tx_hash LIMIT ?',
        (wallet,batch)).fetchall()
    if missing:
        # A COMPLETE header with missing payloads must cease being complete before work.
        with db:db.execute("UPDATE historical_runs SET phase='DECODING',error=NULL WHERE wallet=?",(wallet,))
        decoder=object.__new__(HistoricalCollector)
        for row in missing:
            with rpc.w3.provider.historical_headers():
                payload=decoder.transaction(wallet,row['tx_hash'],run,rpc=rpc)
            with db:db.execute('UPDATE historical_transactions SET payload=? WHERE wallet=? AND tx_hash=? AND payload IS NULL',
                (json.dumps(payload,allow_nan=False),wallet,row['tx_hash']))
    if db.execute('SELECT 1 FROM historical_transactions WHERE wallet=? AND payload IS NULL LIMIT 1',(wallet,)).fetchone():return None
    check_anchor(rpc,run)
    with db:db.execute("UPDATE historical_runs SET phase='COMPLETE',completed_at=?,error=NULL WHERE wallet=?",(int(time.time()),wallet))
    # Persisted ambiguous trades are evidence, not transport errors to endlessly refetch.
    return stored_holding(db,wallet)


def status(db,out):
    rows=[dict(r) for r in db.execute('SELECT * FROM campaign_queue ORDER BY priority')]
    for r in rows:
        r['result']=json.loads(r['result']) if r['result'] else None
        r.update(live_signal_authority=False,paper_selected=False)
    result=dict(updated_at=time.time(),total=len(rows),states=dict(Counter(r['state'] for r in rows)),
        http_attempts={r['service']:r['attempts'] for r in db.execute('SELECT * FROM campaign_budget')},
        transactions=db.execute('SELECT COUNT(*) FROM historical_transactions').fetchone()[0],
        decoded=db.execute('SELECT COUNT(*) FROM historical_transactions WHERE payload IS NOT NULL').fetchone()[0],
        nansen_http_calls=0,nansen_credits_consumed=0,alchemy_http_calls=0,
        production_database_modified=False,rows=rows)
    temp=Path(out)/'status.tmp';temp.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    temp.replace(Path(out)/'status.json')  # Rebuildable status; immutable evidence lives in DB/backups/ledger.
    return result


def run(directory,cfg):
    out=Path(directory).resolve();settings=json.loads((out/'settings.json').read_text())
    if decoding_context(cfg)!=settings['context']:raise ValueError('campaign_decoder_changed')
    if hashlib.sha256((out/'scope.json').read_bytes()).hexdigest()!=settings['scope_sha256']:raise ValueError('campaign_scope_changed')
    from rpc import ArcRPC
    from web3 import Web3
    from dex_registry import TrustedDexRegistry
    with ProcessLock(out/'worker.lock'),closing(connect(out/'campaign.sqlite3')) as db:
        budget=CampaignBudget(out,db,settings)
        cache=PilotCache(out/'rpc_cache.sqlite3',[cfg.rpc_cache_file,
            'outputs/holding_pilot_mainnet_20260922/rpc_cache.sqlite3','outputs/holding_pilot_live_20260922/rpc_cache.sqlite3'])
        clients=[]
        for url in ['https://rpc.mainnet.arc.io','https://rpc.arc-scan.org']:
            assert url in PUBLIC_RPC_URLS
            rpc=object.__new__(ArcRPC);rpc.chain_id=5042
            rpc.dex_registry=TrustedDexRegistry(cfg.trusted_dex_registry,5042)
            rpc.w3=Web3(ResilientRPCProvider(url,5042,
                SharedRateLimiter(cfg.rpc_rate_limit_file+'.fallback',1),
                session=BoundedSession(budget,'rpc',url),cache=cache))
            clients.append(rpc)
        arc=ArcscanClient(ARC_URL);arc.s.close();arc.s=BoundedSession(budget,'arcscan')
        last_backup=time.monotonic();steps=0;stop='COMPLETE'
        try:
            while True:
                if (out/'STOP').exists():stop='STOP_REQUESTED';break
                if time.monotonic()>=budget.deadline:stop='DEADLINE';break
                row=db.execute("SELECT * FROM campaign_queue WHERE state='PENDING' AND next_attempt<=? ORDER BY turns,priority LIMIT 1",(time.time(),)).fetchone()
                if not row:
                    if db.execute("SELECT 1 FROM campaign_queue WHERE state='PENDING'").fetchone():time.sleep(5);continue
                    break
                w=row['wallet'];budget.wallet=w
                # Alternate public providers only after an actual failed attempt.
                rpc=clients[row['failures']%len(clients)]
                try:
                    result=advance(db,w,rpc,arc)
                    if result:
                        state='VERIFIED' if result['holding_duration_eligible'] else 'TOO_SHORT' if result['holding_duration_status']=='EXCLUDED_SHORT_HOLDING' else 'REVIEW_REQUIRED'
                        with db:db.execute('UPDATE campaign_queue SET state=?,result=?,reason=?,turns=turns+1 WHERE wallet=?',
                            (state,json.dumps(result),result['holding_duration_reason'],w))
                    else:
                        with db:db.execute('UPDATE campaign_queue SET turns=turns+1,reason=NULL WHERE wallet=?',(w,))
                except PilotLimit:stop='BUDGET_OR_DEADLINE';break
                except Exception as error:
                    category=error.category if isinstance(error,RPCError) else type(error).__name__
                    failures=row['failures']+1
                    with db:
                        db.execute('UPDATE historical_runs SET error=? WHERE wallet=?',('campaign_'+category,w))
                        db.execute('UPDATE campaign_queue SET failures=?,turns=turns+1,next_attempt=?,state=?,reason=? WHERE wallet=?',
                            (failures,time.time()+min(1800,60*2**failures),'BLOCKED' if failures>=3 else 'PENDING',category,w))
                    print(json.dumps(dict(wallet=w,category=category,attempt=failures,provider='public_primary' if rpc is clients[0] else 'public_secondary')),flush=True)
                steps+=1
                s=status(db,out)
                print(json.dumps({k:s[k] for k in ('updated_at','states','decoded','transactions','http_attempts')}),flush=True)
                if time.monotonic()-last_backup>=1800:
                    snapshot(db,out/'backups','progress')
                    with closing(sqlite3.connect(out/'rpc_cache.sqlite3')) as cache_db:snapshot(cache_db,out/'backups','cache')
                    last_backup=time.monotonic()
        except KeyboardInterrupt:stop='INTERRUPTED'
        finally:
            for rpc in clients:rpc.w3.provider.session.close()
            arc.s.close()
            s=status(db,out);snapshot(db,out/'backups','exit')
            save_new(out/f'run_{time.time_ns()}.json',dict(stop=stop,steps=steps,states=s['states'],http_attempts=s['http_attempts']))
            print(json.dumps(dict(stop=stop,states=s['states'])),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','run','status'])
    p.add_argument('--directory',required=True);p.add_argument('--scope')
    p.add_argument('--source',default='data/arc_bot.sqlite3')
    args=p.parse_args(argv)
    if args.action=='status':
        with closing(sqlite3.connect((Path(args.directory)/'campaign.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            print(json.dumps(dict(states=dict(db.execute('SELECT state,count(*) FROM campaign_queue GROUP BY state')),
                http_attempts=dict(db.execute('SELECT service,attempts FROM campaign_budget'))),indent=2))
        return
    from config import SETTINGS
    if args.action=='prepare':
        if not args.scope:p.error('--scope required')
        prepare(args.source,args.scope,args.directory,SETTINGS)
    else:run(args.directory,SETTINGS)


if __name__=='__main__':main()
