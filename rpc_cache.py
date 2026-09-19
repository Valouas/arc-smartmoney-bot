"""Regenerable raw RPC evidence; no wallet, scoring or checkpoint state."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

METHODS = {'eth_getTransactionByHash','eth_getTransactionReceipt','eth_getBlockByHash'}


class RPCCache:
    def __init__(self,path):
        self.path=Path(path).resolve(); self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rpc_cache_entries (
                    chain INTEGER,method TEXT,params TEXT,payload TEXT,digest TEXT,
                    PRIMARY KEY(chain,method,params));
                CREATE TABLE IF NOT EXISTS rpc_cache_finality (
                    network TEXT PRIMARY KEY,number INTEGER,hash TEXT,checked REAL,available INTEGER);
                CREATE TABLE IF NOT EXISTS rpc_cache_stats (
                    method TEXT PRIMARY KEY,hits INTEGER DEFAULT 0,misses INTEGER DEFAULT 0,writes INTEGER DEFAULT 0);
            ''')

    def connect(self):
        return sqlite3.connect(self.path,timeout=5)

    @staticmethod
    def key(params):
        return json.dumps(params,sort_keys=True,separators=(',',':'))

    @staticmethod
    def network(chain,url):
        # Credentials/URLs never appear in the cache database or its logs.
        return hashlib.sha256((str(chain)+':'+url).encode()).hexdigest()

    def finality(self,network):
        with closing(self.connect()) as db:
            return db.execute('SELECT number,hash,checked,available FROM rpc_cache_finality WHERE network=?',(network,)).fetchone()

    def save_finality(self,network,number,block_hash,now,available=True):
        with closing(self.connect()) as db,db:
            db.execute('INSERT OR REPLACE INTO rpc_cache_finality VALUES(?,?,?,?,?)',(network,number,block_hash,now,int(available)))

    def stat(self,method,column):
        assert column in ('hits','misses','writes')
        with closing(self.connect()) as db,db:
            db.execute('INSERT OR IGNORE INTO rpc_cache_stats(method) VALUES(?)',(method,))
            db.execute(f'UPDATE rpc_cache_stats SET {column}={column}+1 WHERE method=?',(method,))

    def get(self,chain,method,params,finalized):
        from rpc_transport import _valid_result,_matches_request,_hex
        with closing(self.connect()) as db:
            row=db.execute('SELECT payload,digest FROM rpc_cache_entries WHERE chain=? AND method=? AND params=?',
                           (chain,method,self.key(params))).fetchone()
        result=None
        if row and hashlib.sha256(row[0].encode()).hexdigest()==row[1]:
            try:
                candidate=json.loads(row[0])
                number=candidate.get('number') if method=='eth_getBlockByHash' else candidate.get('blockNumber')
                block_hash=candidate.get('hash') if method=='eth_getBlockByHash' else candidate.get('blockHash')
                if (_valid_result(method,candidate) and _matches_request(method,params,candidate)
                        and _hex(block_hash,32) and int(number,16)<=finalized): result=candidate
            except (ValueError,TypeError,KeyError,AttributeError): pass
        self.stat(method,'hits' if result is not None else 'misses')
        return result

    def put(self,chain,method,params,result,finalized):
        from rpc_transport import _hex
        number=result.get('number') if method=='eth_getBlockByHash' else result.get('blockNumber')
        block_hash=result.get('hash') if method=='eth_getBlockByHash' else result.get('blockHash')
        if not _hex(block_hash,32) or number is None or int(number,16)>finalized: return
        payload=self.key(result)
        with closing(self.connect()) as db,db:
            db.execute('INSERT OR REPLACE INTO rpc_cache_entries VALUES(?,?,?,?,?)',
                       (chain,method,self.key(params),payload,hashlib.sha256(payload.encode()).hexdigest()))
        self.stat(method,'writes')
