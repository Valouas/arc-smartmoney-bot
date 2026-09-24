"""Additive external observations; existing local evidence is never overwritten."""
import json
import sqlite3
from pathlib import Path

from external_http import ExternalError, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS external_wallet_candidates (
 wallet TEXT NOT NULL, provider TEXT NOT NULL, observed_at INTEGER NOT NULL,
 retrieved_at INTEGER NOT NULL, chain_id INTEGER NOT NULL,
 payload TEXT NOT NULL, data_complete INTEGER NOT NULL, error_category TEXT,
 status TEXT NOT NULL DEFAULT 'EXTERNAL_CANDIDATE', PRIMARY KEY(wallet,provider)
);
CREATE TABLE IF NOT EXISTS external_wallet_metrics (
 wallet TEXT NOT NULL, provider TEXT NOT NULL, snapshot_key TEXT NOT NULL,
 observed_at INTEGER NOT NULL, retrieved_at INTEGER NOT NULL, chain_id INTEGER NOT NULL,
 payload TEXT, data_complete INTEGER NOT NULL, error_category TEXT,
 PRIMARY KEY(wallet,provider,snapshot_key)
);
CREATE TABLE IF NOT EXISTS external_fetch_state (
 provider TEXT NOT NULL, wallet TEXT NOT NULL DEFAULT '', attempted_at INTEGER NOT NULL,
 next_at REAL NOT NULL, error_category TEXT, PRIMARY KEY(provider,wallet)
);
CREATE INDEX IF NOT EXISTS external_metrics_latest ON external_wallet_metrics(wallet,provider,retrieved_at);
'''


class ExternalStore:
    def __init__(self, path):
        path = Path(path).resolve()
        if not path.is_file(): raise ExternalError('local_database_missing')
        self.db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'discovery_candidates','wallet_profiles','scanner_state'} <= tables:
            self.db.close()
            raise ExternalError('wrong_local_database')
        self.db.executescript(SCHEMA)

    def due(self, provider, wallet, now):
        row = self.db.execute('SELECT next_at FROM external_fetch_state WHERE provider=? AND wallet=?', (provider,wallet)).fetchone()
        return not row or row[0] <= now

    def record_attempt(self, provider, wallet, now, delay, error=None):
        self.db.execute('''INSERT INTO external_fetch_state VALUES(?,?,?,?,?)
            ON CONFLICT(provider,wallet) DO UPDATE SET attempted_at=excluded.attempted_at,
            next_at=excluded.next_at,error_category=excluded.error_category''',
            (provider,wallet,now,now+delay,error))

    def dune(self, result, now, cfg):
        with self.db:
            for row in result['rows']:
                provenance = {k:v for k,v in result.items() if k not in ('rows','rejected')}
                payload = json.dumps({**row, 'source':'dune', 'provenance':provenance}, allow_nan=False)
                self.db.execute('''INSERT INTO external_wallet_candidates
                    (wallet,provider,observed_at,retrieved_at,chain_id,payload,data_complete)
                    VALUES(?,'dune',?,?,5042,?,1) ON CONFLICT(wallet,provider) DO UPDATE SET
                    retrieved_at=excluded.retrieved_at,payload=excluded.payload,data_complete=1,error_category=NULL''',
                    (row['wallet'],now,now,payload))
                self.db.execute('''INSERT INTO external_wallet_metrics VALUES(?,'dune',?,?,?,5042,?,1,NULL)
                    ON CONFLICT(wallet,provider,snapshot_key) DO UPDATE SET retrieved_at=excluded.retrieved_at''',
                    (row['wallet'],result['execution_id']+':'+row['raw_hash'],now,now,payload))
            self.record_attempt('dune','',now,cfg.dune_refresh_seconds)

    def bridge(self, cfg, now, excluded):
        if not cfg.external_discovery_enabled: return
        # Queue identity only. No winner, trade, profile, score or authority is imported.
        # Exhaust the SELECT before beginning writes. A live read cursor keeps an
        # older WAL snapshot that cannot be upgraded after another worker commits.
        rows = self.db.execute('SELECT wallet,observed_at FROM external_wallet_candidates WHERE data_complete=1 AND chain_id=5042').fetchall()
        with self.db:
            for row in rows:
                if row['wallet'] in excluded: continue
                self.db.execute('''INSERT OR IGNORE INTO discovery_candidates
                    (wallet,first_discovered_at,last_discovered_at,status)
                    VALUES(?,?,?,'EXTERNAL_CANDIDATE')''', (row['wallet'],row['observed_at'],now))

    def enrichment_wallets(self, now, limit, excluded):
        rows = self.db.execute('''SELECT w.wallet FROM (
            SELECT wallet FROM external_wallet_candidates UNION SELECT wallet FROM discovery_candidates
            UNION SELECT wallet FROM wallet_profiles) w
            LEFT JOIN external_fetch_state f ON f.wallet=w.wallet AND f.provider='zerion'
            WHERE f.next_at IS NULL OR f.next_at<=? ORDER BY COALESCE(f.attempted_at,0),w.wallet''', (now,))
        result = []
        for row in rows:
            if row[0] not in excluded:
                result.append(row[0])
                if len(result) == limit: break
        return result

    def metric(self, wallet, payload, now, cfg, error=None):
        category = error.category if error else None
        key = 'error:'+str(now) if error else digest(payload)
        with self.db:
            self.db.execute('''INSERT INTO external_wallet_metrics VALUES(?,'zerion',?,?,?,5042,?,?,?)
                ON CONFLICT(wallet,provider,snapshot_key) DO UPDATE SET retrieved_at=excluded.retrieved_at''',
                (wallet,key,now,now,json.dumps(payload,allow_nan=False) if payload is not None else None,
                 int(bool(payload and payload['data_complete'])),category))
            self.record_attempt('zerion',wallet,now,max(cfg.zerion_refresh_seconds,error.retry_after if error else 0),category)

    def close(self): self.db.close()
