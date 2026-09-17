"""Isolated, transactional paper ledger. All money/quantities stored as decimals."""
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wallets(
 wallet TEXT PRIMARY KEY, cash TEXT NOT NULL, started_block INTEGER NOT NULL,buy_after_block INTEGER NOT NULL,
 baseline TEXT NOT NULL, selection TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS positions(
 wallet TEXT NOT NULL,token TEXT NOT NULL,quantity TEXT NOT NULL,cost TEXT NOT NULL,
 PRIMARY KEY(wallet,token));
CREATE TABLE IF NOT EXISTS observations(
 id TEXT PRIMARY KEY,wallet TEXT NOT NULL,token TEXT NOT NULL,side TEXT NOT NULL,
 block INTEGER NOT NULL,tx_index INTEGER NOT NULL,source_at INTEGER NOT NULL,
 detected_at INTEGER NOT NULL,source_usdc TEXT NOT NULL,source_qty TEXT NOT NULL,
 sell_fraction TEXT,budget TEXT,sizing TEXT NOT NULL,status TEXT NOT NULL,
 reason TEXT,attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fills(
 observation_id TEXT PRIMARY KEY,wallet TEXT NOT NULL,token TEXT NOT NULL,side TEXT NOT NULL,
 filled_at INTEGER NOT NULL,quantity TEXT NOT NULL,price TEXT NOT NULL,notional TEXT NOT NULL,
 fee TEXT NOT NULL,gas TEXT NOT NULL,realized_pnl TEXT NOT NULL,quote TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gaps(
 id TEXT PRIMARY KEY,block INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scanned_transactions(
 tx_hash TEXT NOT NULL,wallet TEXT NOT NULL,block INTEGER NOT NULL,
 PRIMARY KEY(tx_hash,wallet));
CREATE TABLE IF NOT EXISTS webhook_triggers(
 tx_hash TEXT NOT NULL,wallet TEXT NOT NULL,received_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',PRIMARY KEY(tx_hash,wallet));
'''


class Store:
    def __init__(self, path, policy):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        # Never initialize/mutate an analyzer database passed by mistake.
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and ('state' not in tables or self.get('application') != 'arc_paper_copy_v1'):
            self.db.close(); raise ValueError('Not a paper copy database')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        if 'buy_after_block' not in {r[1] for r in self.db.execute('PRAGMA table_info(wallets)')}:
            with self.db:
                self.db.execute('ALTER TABLE wallets ADD COLUMN buy_after_block INTEGER NOT NULL DEFAULT 0')
                self.db.execute('UPDATE wallets SET buy_after_block=started_block')
        previous = self.get('policy')
        encoded = json.dumps(asdict(policy), sort_keys=True)
        if previous is not None and previous != encoded:
            self.db.close(); raise ValueError('Simulation policy changed: use a separate database')
        with self.db:
            self.set('policy', encoded)
            self.set('application','arc_paper_copy_v1')

    def get(self, key):
        row = self.db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,str(value)))

    def close(self): self.db.close()
