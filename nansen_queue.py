"""Durable, separate Nansen campaign: bounded state and per-page SQLite cache."""
import json
import sqlite3
import time
from pathlib import Path

from external_http import ExternalError, digest, wallet_address
from nansen_client import SUMMARY, DETAIL, summary, detail_page


class PageCache:
    def __init__(self, db): self.db = db
    def __contains__(self, key):
        return self.db.execute('SELECT 1 FROM pages WHERE key=?',(key,)).fetchone() is not None
    def __getitem__(self, key):
        row = self.db.execute('SELECT payload FROM pages WHERE key=?',(key,)).fetchone()
        if row is None: raise KeyError(key)
        return json.loads(row[0])
    def __setitem__(self, key, value):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO pages VALUES(?,?)',
                            (key,json.dumps(value,allow_nan=False)))


class Campaign:
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(path,timeout=10)
        self.db.row_factory = sqlite3.Row
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and not {'campaign','pages','jobs'} <= tables:
            self.db.close(); raise ExternalError('wrong_nansen_campaign_database')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS campaign(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(wallet TEXT PRIMARY KEY,prior REAL NOT NULL,
                summary TEXT,realized REAL,result TEXT,error TEXT,next_at REAL NOT NULL DEFAULT 0,
                excluded INTEGER NOT NULL DEFAULT 0);
        ''')
        row = self.db.execute('SELECT payload FROM campaign WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else None
        if self.state:
            if self.state.get('application') != 'nansen_arc_campaign_v1':
                self.db.close(); raise ExternalError('wrong_nansen_campaign_database')
            self.state['cache'] = PageCache(self.db)

    def initialize(self, rows, trial, max_requests):
        if self.state:
            if self.state['max_requests'] != max_requests:
                raise ExternalError('nansen_campaign_budget_changed')
            return
        if not 1 <= max_requests <= 25000: raise ExternalError('invalid_nansen_campaign_budget')
        from nansen_audit import START
        from external_http import stamp
        date = trial['date']
        if date['from'] != START or not stamp(START)<stamp(date['to'])<=time.time():
            raise ExternalError('invalid_nansen_campaign_scope')
        # All eligible known wallets are kept, regardless of trade count or PnL.
        with self.db:
            for row in rows:
                wallet = wallet_address(row['wallet'])
                if row['infrastructure_excluded']: continue
                prior = max(0,row.get('local_conservative_pnl_usdc') or 0,
                            (row.get('zerion_metrics') or {}).get('total_gain') or 0)
                self.db.execute('INSERT OR IGNORE INTO jobs(wallet,prior) VALUES(?,?)',(wallet,prior))
            # Reuse the successful trial pages at exactly the same cutoff.
            for key, value in trial.get('cache',{}).items():
                route, body, data = value['route'], value['request'], value['response']
                if body.get('chain')!='arc' or body.get('date')!=date or key!=digest([route,body]):
                    raise ExternalError('invalid_nansen_seed_cache')
                wallet_address(body.get('address'))
                if route==SUMMARY: summary(data)
                elif route==DETAIL: detail_page(data,body['pagination']['page'])
                else: raise ExternalError('invalid_nansen_seed_route')
                self.db.execute('INSERT OR IGNORE INTO pages VALUES(?,?)',(key,json.dumps(value,allow_nan=False)))
            self.state = {'application':'nansen_arc_campaign_v1','date':date,'created_at':int(time.time()),
                'max_requests':max_requests,'attempts':0,'credits_used_observed':0,
                'credits_remaining':None,'credit_reserve':100,'status':'PREPARED',
                'live_signal_authority':False}
            self.db.execute('INSERT INTO campaign VALUES(1,?)',(json.dumps(self.state),))
        self.state['cache'] = PageCache(self.db)

    def persist(self):
        with self.db:
            self.db.execute('UPDATE campaign SET payload=? WHERE id=1',
                (json.dumps({k:v for k,v in self.state.items() if k!='cache'},allow_nan=False),))

    def save_summary(self, wallet, payload):
        with self.db:
            self.db.execute('UPDATE jobs SET summary=?,realized=?,error=NULL,next_at=0 WHERE wallet=?',
                (json.dumps(payload,allow_nan=False),payload['summary']['realized_pnl_usd'],wallet))

    def save_result(self, wallet, payload):
        with self.db:
            self.db.execute('UPDATE jobs SET result=?,error=NULL,next_at=0 WHERE wallet=?',
                (json.dumps(payload,allow_nan=False),wallet))

    def defer(self, wallet, category, delay):
        with self.db:
            self.db.execute('UPDATE jobs SET error=?,next_at=? WHERE wallet=?',
                            (category,time.time()+delay,wallet))

    def exclude(self, addresses):
        with self.db:
            self.db.executemany('UPDATE jobs SET excluded=1,error=? WHERE wallet=?',
                                [('infrastructure_excluded',w) for w in addresses])

    def next_job(self, phase):
        field = 'summary' if phase=='SUMMARIES' else 'result'
        # Priority only; low PnL, rare trades and contract accounts remain queued.
        order = 'prior DESC,wallet' if phase=='SUMMARIES' else 'MAX(COALESCE(realized,0),prior) DESC,wallet'
        return self.db.execute('SELECT * FROM jobs WHERE excluded=0 AND '+field+
            ' IS NULL '+('AND summary IS NOT NULL ' if phase!='SUMMARIES' else '')+
            'AND next_at<=? ORDER BY '+order+' LIMIT 1',(time.time(),)).fetchone()

    def progress(self):
        r = self.db.execute('''SELECT COUNT(*) total,SUM(summary IS NOT NULL) summaries,
            SUM(result IS NOT NULL) details,SUM(error IS NOT NULL) errors,SUM(excluded) excluded FROM jobs''').fetchone()
        return {**{k:v for k,v in self.state.items() if k!='cache'},**{k:(r[k] or 0) for k in r.keys()},
                'updated_at':int(time.time())}

    def close(self): self.db.close()
