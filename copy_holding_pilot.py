"""Explicit three-wallet timing pilot, public endpoints, isolated evidence only."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import requests

from arcscan import ArcscanClient
from copy_candidate_report import shortlist
from copy_holding_duration import stored_holding
from historical_collector import HistoricalCollector, parse_page
from historical_store import SCHEMA
from rpc_cache import RPCCache
from rpc_transport import RPCError, ResilientRPCProvider, SharedRateLimiter, READ_METHODS

RPC_URL = 'https://rpc.arc-scan.org'
PUBLIC_RPC_URLS = {RPC_URL, 'https://rpc.mainnet.arc.io'}
ARC_URL = 'https://api.arc-scan.org/v1'


def save_new(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False, indent=2)


class PilotLimit(RPCError):
    def __init__(self):
        super().__init__('pilot', 'pilot_budget_or_deadline', 0, 'public')


class Budget:
    def __init__(self, directory, rpc=300, arcscan=12, seconds=600):
        self.directory = Path(directory)
        self.limits = {'rpc': rpc, 'arcscan': arcscan}
        self.counts = Counter()
        self.methods = Counter()
        self.wallet = None
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.last_request = 0

    def take(self, service, method):
        if time.monotonic() >= self.deadline or self.counts[service] >= self.limits[service]:
            raise PilotLimit()
        self.counts[service] += 1  # Every attempt, including transport errors.
        self.methods[method] += 1

    def record(self, event):
        with (self.directory/'requests.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, allow_nan=False)+'\n')
            f.flush()
            os.fsync(f.fileno())


class BoundedSession(requests.Session):
    def __init__(self, budget, service, rpc_url=RPC_URL):
        super().__init__()
        if rpc_url not in PUBLIC_RPC_URLS:
            raise ValueError('pilot_endpoint_forbidden')
        self.budget, self.service = budget, service
        self.rpc_url = rpc_url

    def request(self, method, url, **kwargs):
        if self.service == 'rpc':
            if method.upper() != 'POST' or url != self.rpc_url:
                raise ValueError('pilot_endpoint_forbidden')
            payload = json.loads(kwargs['data'])
            name = payload.get('method')
            if name not in READ_METHODS:
                raise ValueError('pilot_method_forbidden')
            scope = {'method': name, 'params': payload.get('params')}
        else:
            expected = ARC_URL+'/address/'+str(self.budget.wallet)+'/activity'
            if method.upper() != 'GET' or url != expected:
                raise ValueError('pilot_endpoint_forbidden')
            name = 'address_activity'
            scope = {'method': name, 'params': kwargs.get('params')}
        self.budget.take(self.service, name)
        delay = max(0, 1-(time.monotonic()-self.budget.last_request))
        if time.monotonic()+delay >= self.budget.deadline:
            raise PilotLimit()
        time.sleep(delay)
        self.budget.last_request = time.monotonic()
        # Persist the attempt before touching the network; no URLs/auth headers.
        event = dict(service=self.service, wallet=self.budget.wallet,
                     sequence=sum(self.budget.counts.values()), scope=scope, state='attempt')
        self.budget.record(event)
        kwargs['allow_redirects'] = False
        kwargs['timeout'] = min(10, max(.1,self.budget.deadline-time.monotonic()))
        try:
            response = super().request(method,url,**kwargs)
            event.update(state='response',status=response.status_code)
            if response.status_code == 200:
                try: event['response'] = response.json()
                except ValueError: event['category'] = 'invalid_json'
            self.budget.record(event)
            return response
        except requests.RequestException as error:
            event.update(state='failure',category=type(error).__name__)
            self.budget.record(event)
            raise


class ReadOnlyCache(RPCCache):
    def __init__(self, path): self.path = Path(path).resolve()
    def connect(self): return sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
    def stat(self, method, column): pass


class PilotCache(RPCCache):
    def __init__(self, path, sources):
        super().__init__(path)
        self.sources = [ReadOnlyCache(source) for source in sources if Path(source).exists()]
        self.reused = 0

    def get(self, chain, method, params, finalized):
        value = super().get(chain,method,params,finalized)
        for source in self.sources:
            if value is not None: break
            value = source.get(chain,method,params,finalized)
            if value is not None:
                self.reused += 1
                self.put(chain,method,params,value,finalized)
        return value


def prepare(source, directory, wallets):
    """Atomic read snapshot of only the requested wallets; source never mutated."""
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as src:
        src.row_factory = sqlite3.Row
        src.execute('PRAGMA query_only=ON'); src.execute('BEGIN')
        data = []
        for wallet in wallets:
            run = src.execute('SELECT * FROM historical_runs WHERE wallet=?',(wallet,)).fetchone()
            if not run or run['version'] != 1 or run['phase'] not in ('FETCHING','DECODING','COMPLETE'):
                raise ValueError('pilot_requires_existing_version1_history')
            data.append(dict(wallet=wallet,run=dict(run),transactions=[dict(r) for r in src.execute(
                'SELECT * FROM historical_transactions WHERE wallet=?',(wallet,))]))
    save_new(directory/'initial_evidence.json',data)
    db = sqlite3.connect(directory/'pilot.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    with db:
        for entry in data:
            run=entry['run'];columns=','.join(run)
            db.execute(f"INSERT INTO historical_runs({columns}) VALUES({','.join('?' for _ in run)})",list(run.values()))
            for tx in entry['transactions']:
                db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',
                           (entry['wallet'],tx['tx_hash'],tx['payload']))
    return db


def process_wallet(db, wallet, rpc, arc):
    run=dict(db.execute('SELECT * FROM historical_runs WHERE wallet=?',(wallet,)).fetchone())
    expected=run['cutoff_hash'].lower()
    if rpc.get_block(run['cutoff_block'],False)['hash'].hex().removeprefix('0x') != expected.removeprefix('0x'):
        raise ValueError('pilot_anchor_mismatch')
    while run['phase']=='FETCHING':
        seen=json.loads(run['seen'])
        page=arc.address_activity(wallet,limit=100,cursor=run['cursor'])
        hashes,nxt=parse_page(page,run['cutoff_block'],seen)
        with db:
            for h in hashes: db.execute('INSERT OR IGNORE INTO historical_transactions VALUES(?,?,NULL)',(wallet,h))
            run.update(cursor=nxt,seen=json.dumps(seen+[nxt] if nxt else seen),phase='FETCHING' if nxt else 'DECODING')
            db.execute('UPDATE historical_runs SET cursor=?,seen=?,phase=? WHERE wallet=?',
                       (run['cursor'],run['seen'],run['phase'],wallet))
    # Direct transaction decoder only: never invoke collector.start/step/identity.
    decoder=object.__new__(HistoricalCollector)
    with db: db.execute("UPDATE historical_runs SET phase='DECODING',error=NULL WHERE wallet=?",(wallet,))
    decoded = reused = 0
    for row in db.execute('SELECT tx_hash,payload FROM historical_transactions WHERE wallet=? ORDER BY tx_hash',(wallet,)).fetchall():
        old=json.loads(row['payload']) if row['payload'] else None
        if old and not old.get('error') and not old.get('inventory_gap'):
            reused += 1
            continue
        with rpc.w3.provider.historical_headers():
            result=decoder.transaction(wallet,row['tx_hash'],run,rpc=rpc)
        with db: db.execute('UPDATE historical_transactions SET payload=? WHERE wallet=? AND tx_hash=?',
                            (json.dumps(result,allow_nan=False),wallet,row['tx_hash']))
        decoded += 1
    if rpc.get_block(run['cutoff_block'],False)['hash'].hex().removeprefix('0x') != expected.removeprefix('0x'):
        raise ValueError('pilot_anchor_mismatch')
    with db: db.execute("UPDATE historical_runs SET phase='COMPLETE',completed_at=?,error=NULL WHERE wallet=?",(int(time.time()),wallet))
    return dict(decoded_this_pilot=decoded,reused_decoded=reused)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scope',required=True)
    p.add_argument('--output-dir',required=True)
    p.add_argument('--db',default='data/arc_bot.sqlite3')
    p.add_argument('--rpc-url',choices=sorted(PUBLIC_RPC_URLS),default=RPC_URL)
    p.add_argument('--reuse-cache',action='append',default=[])
    p.add_argument('--max-rpc',type=int,default=300)
    p.add_argument('--max-arcscan',type=int,default=12)
    p.add_argument('--seconds',type=int,default=600)
    args=p.parse_args(argv)
    wallets=shortlist(args.scope)
    if len(wallets)!=3 or not 0<=args.max_rpc<=300 or not 0<=args.max_arcscan<=12 or not 1<=args.seconds<=600:
        p.error('Exactly 3 wallets; maximum 300 RPC / 12 Arcscan / 600 seconds')
    from config import SETTINGS
    from wallet_policy import infrastructure_addresses
    if set(wallets)&infrastructure_addresses(SETTINGS): p.error('Infrastructure excluded')
    out=Path(args.output_dir).resolve();out.mkdir(parents=True,exist_ok=False)
    db=prepare(args.db,out,wallets)
    budget=Budget(out,args.max_rpc,args.max_arcscan,args.seconds)
    from rpc import ArcRPC
    from web3 import Web3
    from dex_registry import TrustedDexRegistry
    cache=PilotCache(out/'rpc_cache.sqlite3',[*args.reuse_cache,SETTINGS.rpc_cache_file])
    rpc=object.__new__(ArcRPC);rpc.chain_id=5042
    rpc.dex_registry=TrustedDexRegistry(SETTINGS.trusted_dex_registry,5042)
    rpc.w3=Web3(ResilientRPCProvider(args.rpc_url,5042,
        SharedRateLimiter(SETTINGS.rpc_rate_limit_file+'.fallback',1),
        session=BoundedSession(budget,'rpc',args.rpc_url),cache=cache))
    arc=ArcscanClient(ARC_URL);arc.s.close();arc.s=BoundedSession(budget,'arcscan')
    results=[]
    try:
        for wallet in wallets:
            budget.wallet=wallet;started=time.monotonic();before=dict(budget.counts)
            result=dict(wallet=wallet,live_signal_authority=False,paper_selected=False)
            try:
                result.update(process_wallet(db,wallet,rpc,arc),state='COLLECTED')
            except Exception as error:
                # Report category only, never raw transport exception/URL.
                category=error.category if isinstance(error,RPCError) else type(error).__name__
                result.update(state='INCOMPLETE',category=category)
                with db: db.execute('UPDATE historical_runs SET error=? WHERE wallet=?',('pilot_'+category,wallet))
            result.update(stored_holding(db,wallet),elapsed_seconds=round(time.monotonic()-started,2),
                http_attempts={s:budget.counts[s]-before.get(s,0) for s in budget.limits})
            results.append(result)
            save_new(out/(wallet+'.json'),result)
            print(json.dumps(result),flush=True)
    finally:
        db.close();rpc.w3.provider.session.close();arc.s.close()
        summary=dict(rows=results,limits=budget.limits,http_attempts=dict(budget.counts),
                     methods=dict(budget.methods),source_cache_hits=cache.reused,
                     elapsed_seconds=round(time.monotonic()-budget.started,2),
                     nansen_http_calls=0,alchemy_http_calls=0,paid_provider_credits_consumed=0,
                     production_database_modified=False)
        save_new(out/'summary.json',summary)
        paths=[f for f in out.iterdir() if f.is_file()]
        save_new(out/'manifest.json',{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in paths})
        print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__': main()
