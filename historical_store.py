"""Versioned historical evidence, isolated from forward promotion and coverage."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_runs (
 wallet TEXT PRIMARY KEY, version INTEGER NOT NULL, started_at INTEGER NOT NULL,
 cutoff_block INTEGER NOT NULL, cutoff_at INTEGER NOT NULL, cutoff_hash TEXT NOT NULL,
 phase TEXT NOT NULL, cursor TEXT, seen TEXT NOT NULL DEFAULT '[]',
 last_attempt INTEGER NOT NULL DEFAULT 0, completed_at INTEGER, error TEXT,
 identity TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS historical_transactions (
 wallet TEXT NOT NULL, tx_hash TEXT NOT NULL, payload TEXT,
 PRIMARY KEY(wallet,tx_hash)
);
CREATE TABLE IF NOT EXISTS historical_scheduler (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), next_slot INTEGER NOT NULL
);
INSERT OR IGNORE INTO historical_scheduler VALUES(1,0);
"""
VERSION = 1


def initialize(db):
    db.executescript(SCHEMA)
    db.commit()
