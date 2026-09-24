"""Archived Dune raw reads for the existing decoder, with canonical RPC checks.

No wallet discovery, scoring, or fabricated trades. An incomplete/invalid bundle
is a cache miss. RPC errors during canonical verification are NOT cache misses.
"""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from hexbytes import HexBytes
from provider_history_comparison import hex_value, dune_rows


SCHEMA = '''
CREATE TABLE IF NOT EXISTS bundles(tx_hash TEXT PRIMARY KEY,payload TEXT NOT NULL,
 digest TEXT NOT NULL,source TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,state TEXT NOT NULL,
 hashes TEXT NOT NULL,execution TEXT,cost REAL DEFAULT 0,error TEXT);
'''


def connect(path):
    db=sqlite3.connect(path,timeout=20);db.row_factory=sqlite3.Row
    return db


def integer(v):
    if type(v) is int and v>=0:return v
    if isinstance(v,str) and re.fullmatch(r'[0-9]+',v):return int(v)
    raise ValueError('invalid_exact_integer')


def timestamp(v):
    if not isinstance(v,str):raise ValueError('invalid_time')
    d=datetime.fromisoformat(v.replace(' UTC','+00:00').replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timezone_missing')
    n=d.timestamp()
    if n<0 or not n.is_integer():raise ValueError('non_integral_timestamp')
    return int(n)


def data_hex(v):
    if not isinstance(v,str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*',v):
        raise ValueError('invalid_data')
    return v.lower()


def parse_rows(rows, expected):
    expected={hex_value(h,64) for h in expected};txs={};logs=defaultdict(list)
    for r in rows:
        h=hex_value(r['tx_hash'],64)
        if h not in expected:raise ValueError('unexpected_hash')
        if r['kind']=='tx':
            if h in txs:raise ValueError('duplicate_transaction')
            txs[h]=r
        elif r['kind']=='log':logs[h].append(r)
        else:raise ValueError('unexpected_kind')
    bundles={};rejected={}
    for h in expected:
        try:
            r=txs[h]
            if r['chain_id'] not in ('0x13b2','5042',5042):raise ValueError('wrong_chain')
            block=integer(r['block_number']);index=integer(r['tx_index'])
            bh=hex_value(r['block_hash'],64);ts=timestamp(r['block_time'])
            if type(r['success']) is not bool:raise ValueError('missing_status')
            if integer(r['log_count'])!=len(logs[h]):raise ValueError('incomplete_logs')
            tx=dict(hash=h,blockNumber=block,blockHash=bh,transactionIndex=index,
                    value=integer(r['raw_value']),to=hex_value(r['recipient'],40) if r['recipient'] else None)
            tx['from']=hex_value(r['sender'],40)
            receipt=dict(transactionHash=h,blockNumber=block,blockHash=bh,transactionIndex=index,
                         status=int(r['success']),gasUsed=integer(r['gas_used']),
                         effectiveGasPrice=integer(r['gas_price']),logs=[])
            indices=set()
            for l in logs[h]:
                i=integer(l['log_index'])
                if i in indices:raise ValueError('duplicate_log')
                indices.add(i)
                if (integer(l['block_number']),integer(l['tx_index']),hex_value(l['block_hash'],64),timestamp(l['block_time']))!=(block,index,bh,ts):
                    raise ValueError('log_block_mismatch')
                topics=[];ended=False
                for key in ('topic0','topic1','topic2','topic3'):
                    if l[key] is None:ended=True
                    elif ended:raise ValueError('noncontiguous_topics')
                    else:topics.append(hex_value(l[key],64))
                receipt['logs'].append(dict(address=hex_value(l['emitter'],40),topics=topics,
                    data=data_hex(l['data']),logIndex=i,transactionHash=h,transactionIndex=index,
                    blockHash=bh,blockNumber=block,removed=False))
            if not r['success'] and receipt['logs']:raise ValueError('failed_transaction_with_logs')
            receipt['logs'].sort(key=lambda l:l['logIndex'])
            bundles[h]=dict(transaction=tx,receipt=receipt,timestamp=ts,chain_id=5042)
        except (ValueError,KeyError,TypeError,OverflowError):
            rejected[h]='missing_or_invalid_raw_bundle'
    return bundles,rejected


def import_result(db, result, expected, source):
    # No database publication from a truncated/pending result, even valid prefixes.
    rows=dune_rows(result)
    bundles,rejected=parse_rows(rows,expected)
    with db:
        for h,bundle in bundles.items():
            raw=json.dumps(bundle,sort_keys=True,separators=(',',':'),allow_nan=False)
            digest=hashlib.sha256(raw.encode()).hexdigest()
            prior=db.execute('SELECT digest FROM bundles WHERE tx_hash=?',(h,)).fetchone()
            if prior and prior[0]!=digest:raise ValueError('conflicting_preserved_bundle')
            db.execute('INSERT OR IGNORE INTO bundles VALUES(?,?,?,?,?)',(h,raw,digest,source,time.time()))
    return dict(imported=len(bundles),rejected=rejected)


class HybridReads:
    def __init__(self,path):
        self.path=Path(path).resolve();self.hits=0;self.misses=0

    def prioritize(self,hashes):
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
                available={r[0] for r in db.execute('SELECT tx_hash FROM bundles')}
            return sorted(hashes,key=lambda h:h not in available)
        except (OSError,sqlite3.Error):return hashes

    def available(self,hashes):
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
                present={r[0] for r in db.execute('SELECT tx_hash FROM bundles')}
            return [h for h in hashes if h in present]
        except (OSError,sqlite3.Error):return []

    def get(self,h,run,rpc):
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
                row=db.execute('SELECT payload,digest FROM bundles WHERE tx_hash=?',(h,)).fetchone()
            if not row:
                self.misses+=1;return None
            if hashlib.sha256(row[0].encode()).hexdigest()!=row[1]:raise ValueError('integrity')
            b=json.loads(row[0]);tx=b['transaction'];receipt=b['receipt']
            if b['chain_id']!=5042 or tx['hash']!=h or receipt['transactionHash']!=h:
                raise ValueError('scope')
            if type(tx['blockNumber']) is not int or tx['blockNumber']>run['cutoff_block']:
                raise ValueError('outside_snapshot')
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            self.misses+=1;return None
        # Still use the normal RPC cache and budget for a canonical block header.
        # A timeout/reorg must not silently become an empty/accepted transaction.
        block=rpc.get_block(tx['blockNumber'],False)
        if (HexBytes(block['hash'])!=HexBytes(tx['blockHash']) or
                int(block['timestamp'])!=b['timestamp'] or
                int(block['number'])!=tx['blockNumber']):
            self.misses+=1;return None
        tx['blockHash']=HexBytes(tx['blockHash']);receipt['blockHash']=HexBytes(receipt['blockHash'])
        self.hits+=1
        return tx,receipt,block


def ready_wallet(campaign,path,now):
    """Prefer ready batches; caller alternates with normal scheduling for fairness."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            hashes={r[0] for r in db.execute('SELECT tx_hash FROM bundles')}
        for r in campaign.execute('''SELECT q.* FROM campaign_queue q JOIN historical_runs h ON h.wallet=q.wallet
            WHERE q.state='PENDING' AND q.next_attempt<=? AND h.phase='DECODING' ORDER BY q.turns,q.priority''',(now,)):
            if any(t[0] in hashes for t in campaign.execute('SELECT tx_hash FROM historical_transactions WHERE wallet=? AND payload IS NULL',(r['wallet'],))):
                return r
    except (OSError,sqlite3.Error):pass
    return None
