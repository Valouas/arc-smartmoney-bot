"""Durable Alchemy triggers; never writes trades, authority or scanner checkpoints."""
import hashlib
import json
import re
import time

MAX_BODY = 1024*1024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS alchemy_deliveries (
 event_id TEXT PRIMARY KEY, body_digest TEXT NOT NULL, received_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alchemy_items (
 event_id TEXT NOT NULL,tx_hash TEXT NOT NULL,log_index INTEGER NOT NULL,
 PRIMARY KEY(event_id,tx_hash,log_index));
CREATE TABLE IF NOT EXISTS alchemy_jobs (
 tx_hash TEXT NOT NULL,wallet TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,error TEXT,
 PRIMARY KEY(tx_hash,wallet));
CREATE TABLE IF NOT EXISTS alchemy_paper_triggers (
 tx_hash TEXT NOT NULL,wallet TEXT NOT NULL,received_at INTEGER NOT NULL,
 PRIMARY KEY(tx_hash,wallet));
'''


class InvalidWebhook(ValueError): pass


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise InvalidWebhook('Duplicate JSON field')
        result[key] = value
    return result


def parse(body):
    try:
        data = json.loads(body,object_pairs_hook=unique_object)
        event_id = data['id']
        if (not isinstance(event_id,str) or not re.fullmatch(r'[A-Za-z0-9_:\-]{1,200}',event_id)
                or data['type'] != 'ADDRESS_ACTIVITY' or data['event']['network'] != 'ARC_MAINNET'
                or not isinstance(data['event']['activity'],list) or len(data['event']['activity'])>2000):
            raise ValueError()
        items=[]
        for activity in data['event']['activity']:
            tx_hash = activity['hash']
            if not isinstance(tx_hash,str) or not re.fullmatch('0x[0-9a-fA-F]{64}',tx_hash): raise ValueError()
            wallets=[]
            for key in ('fromAddress','toAddress'):
                value=activity[key]
                if not isinstance(value,str) or not re.fullmatch('0x[0-9a-fA-F]{40}',value): raise ValueError()
                wallets.append(value.lower())
            log=activity.get('log') or {}
            if not isinstance(log,dict): raise ValueError()
            if log.get('transactionHash',tx_hash).lower()!=tx_hash.lower(): raise ValueError()
            removed=log.get('removed',False)
            if type(removed) is not bool: raise ValueError()
            index=log.get('logIndex')
            if index is None: index=-1
            elif isinstance(index,str) and re.fullmatch('0x[0-9a-fA-F]+',index): index=int(index,16)
            elif type(index) is not int: raise ValueError()
            if not -1 <= index <= 2147483647: raise ValueError()
            items.append((tx_hash.lower(),index,wallets,removed))
        return event_id,items
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError):
        raise InvalidWebhook('Invalid Address Activity payload') from None


def accept(db,body,watch,now=None,paper_watch=None):
    event_id,items=parse(body)
    now=int(time.time()) if now is None else now
    digest=hashlib.sha256(body).hexdigest()
    with db:
        # The write transaction precedes dedupe, including concurrent deliveries.
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT body_digest FROM alchemy_deliveries WHERE event_id=?',(event_id,)).fetchone()
        if previous:
            if previous[0]!=digest: raise InvalidWebhook('Event ID reused with different body')
            return {'accepted':True,'duplicate':True,'queued':0,'paper_queued':0}
        db.execute('INSERT INTO alchemy_deliveries VALUES(?,?,?)',(event_id,digest,now))
        count=paper_count=0
        for tx,index,wallets,removed in items:
            db.execute('INSERT OR IGNORE INTO alchemy_items VALUES(?,?,?)',(event_id,tx,index))
            if removed: continue
            for wallet in set(wallets)&set(watch):
                cur=db.execute('INSERT OR IGNORE INTO alchemy_jobs(tx_hash,wallet) VALUES(?,?)',(tx,wallet))
                count+=cur.rowcount
            for wallet in set(wallets)&set(paper_watch or ()):
                cur=db.execute('INSERT OR IGNORE INTO alchemy_paper_triggers VALUES(?,?,?)',(tx,wallet,now))
                paper_count+=cur.rowcount
    return {'accepted':True,'duplicate':False,'queued':count,'paper_queued':paper_count}
