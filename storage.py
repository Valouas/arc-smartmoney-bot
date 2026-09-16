from __future__ import annotations
import sqlite3, json, time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_txs (
    tx_hash TEXT PRIMARY KEY,
    block_number INTEGER NOT NULL,
    seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_hash TEXT NOT NULL,
    block_number INTEGER,
    timestamp INTEGER,
    wallet TEXT,
    side TEXT,
    token TEXT,
    symbol TEXT,
    token_amount REAL,
    usdc_amount REAL
);
CREATE INDEX IF NOT EXISTS idx_events_wallet_ts ON events(wallet, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_token_ts ON events(token, timestamp);

CREATE TABLE IF NOT EXISTS trade_context (
    tx_hash TEXT NOT NULL,
    token TEXT NOT NULL,
    wallet TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    price_usd REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    fdv_usd REAL,
    pair_created_at_ms INTEGER,
    pair_address TEXT,
    PRIMARY KEY(tx_hash, token, wallet)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    token TEXT,
    side TEXT,
    status TEXT,
    score INTEGER,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    token TEXT PRIMARY KEY,
    symbol TEXT,
    opened_at INTEGER,
    entry_price REAL,
    notional_usd REAL,
    quantity REAL,
    status TEXT,
    closed_at INTEGER,
    exit_price REAL,
    pnl_usd REAL,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS launch_candidates (
    token TEXT PRIMARY KEY,
    first_seen_at INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    symbol TEXT,
    name TEXT,
    deployer TEXT,
    deployment_block INTEGER,
    deployed_at INTEGER,
    status TEXT NOT NULL DEFAULT 'CANDIDATE'
);
CREATE INDEX IF NOT EXISTS idx_launch_candidates_status ON launch_candidates(status, last_checked_at);

CREATE TABLE IF NOT EXISTS launches (
    token TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    deployer TEXT,
    deployment_block INTEGER,
    deployed_at INTEGER,
    discovered_at INTEGER NOT NULL,
    pair_address TEXT,
    pair_created_at INTEGER NOT NULL,
    dex_id TEXT,
    first_price_usd REAL,
    first_liquidity_usd REAL,
    first_market_cap_usd REAL,
    first_fdv_usd REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_launches_pair_created ON launches(pair_created_at);

CREATE TABLE IF NOT EXISTS launch_snapshots (
    token TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL,
    target_at INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    delay_seconds INTEGER NOT NULL,
    price_usd REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    fdv_usd REAL,
    volume_m5 REAL,
    volume_h1 REAL,
    buys_m5 INTEGER,
    sells_m5 INTEGER,
    PRIMARY KEY(token, horizon_minutes)
);

CREATE TABLE IF NOT EXISTS wallet_profiles (
    wallet TEXT PRIMARY KEY,
    generated_at INTEGER NOT NULL,
    smart_score INTEGER NOT NULL,
    profile_class TEXT NOT NULL,
    recommended_weight INTEGER NOT NULL,
    confirmation_eligible INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""

class Storage:
    def __init__(self, path="data/arc_bot.sqlite3"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(SCHEMA + V4_SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        # Serialize check-and-alter across all four processes (WAL is not a
        # substitute for a migration lock). Additive migration preserves V3/V4.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(discovery_candidates)")}
            if "history_complete" not in columns:
                self.db.execute("ALTER TABLE discovery_candidates ADD COLUMN history_complete INTEGER NOT NULL DEFAULT 0")
                self.db.execute("UPDATE discovery_candidates SET status='QUARANTINE',hydrated_at=NULL,promotion_reason='requires_audit_revalidation'")
                self.db.execute("UPDATE promoted_wallets SET enabled=0")
            research_columns = {r[1] for r in self.db.execute("PRAGMA table_info(research_trades)")}
            if "trade_verified" not in research_columns:
                self.db.execute("ALTER TABLE research_trades ADD COLUMN trade_verified INTEGER NOT NULL DEFAULT 0")
            event_columns = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
            if "trade_verified" not in event_columns:
                self.db.execute("ALTER TABLE events ADD COLUMN trade_verified INTEGER NOT NULL DEFAULT 0")
            self.db.execute("CREATE TABLE IF NOT EXISTS processed_events (tx_hash TEXT, wallet TEXT, token TEXT, side TEXT, PRIMARY KEY(tx_hash,wallet,token,side))")
            self.db.execute("CREATE TABLE IF NOT EXISTS scanner_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS snapshot_attempts (token TEXT, horizon INTEGER, checked_at INTEGER NOT NULL, PRIMARY KEY(token,horizon))")
            self.db.execute("PRAGMA user_version=1")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def wallet_eligible(self, wallet):
        row = self.db.execute("SELECT status,history_complete FROM discovery_candidates WHERE wallet=?", (wallet.lower(),)).fetchone()
        if row is None: return True  # Manually curated seed.
        if row["status"] != "PROMOTED" or not row["history_complete"]: return False
        return self.db.execute("SELECT 1 FROM promoted_wallets WHERE wallet=? AND enabled=1", (wallet.lower(),)).fetchone() is not None

    def event_processed(self, e):
        return self.db.execute("SELECT 1 FROM processed_events WHERE tx_hash=? AND wallet=? AND token=? AND side=?",
                               (e.tx_hash.lower(), e.wallet.lower(), e.token.lower(), e.side)).fetchone() is not None

    def mark_event_processed(self, e):
        self.db.execute("INSERT OR IGNORE INTO processed_events VALUES(?,?,?,?)",
                        (e.tx_hash.lower(), e.wallet.lower(), e.token.lower(), e.side))
        self.db.commit()

    def checkpoint(self, block):
        self.db.execute("INSERT OR REPLACE INTO scanner_state VALUES('next_block', ?)", (block,))
        self.db.commit()

    def next_block(self):
        row = self.db.execute("SELECT value FROM scanner_state WHERE key='next_block'").fetchone()
        return row[0] if row else None

    def tx_seen(self, tx_hash: str) -> bool:
        canonical = tx_hash.lower().removeprefix("0x")
        return self.db.execute("SELECT 1 FROM seen_txs WHERE lower(tx_hash) IN (?,?)", (canonical, "0x"+canonical)).fetchone() is not None

    def mark_tx(self, tx_hash: str, block_number: int, ts: int):
        self.db.execute("INSERT OR IGNORE INTO seen_txs VALUES (?,?,?)", (tx_hash, block_number, ts))
        self.db.commit()

    def add_event(self, e):
        if self.db.execute("SELECT 1 FROM events WHERE lower(tx_hash)=? AND lower(wallet)=? AND lower(token)=? AND side=?",
                           (e.tx_hash.lower(), e.wallet.lower(), e.token.lower(), e.side)).fetchone():
            return
        self.db.execute(
            """INSERT INTO events(tx_hash,block_number,timestamp,wallet,side,token,symbol,token_amount,usdc_amount,trade_verified)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (e.tx_hash,e.block_number,e.timestamp,e.wallet,e.side,e.token,e.token_symbol,e.token_amount,e.usdc_amount,int(e.trade_verified))
        )
        self.db.commit()

    def add_trade_context(self, e, market):
        if market is None:
            return
        self.db.execute(
            """INSERT OR REPLACE INTO trade_context(
                tx_hash,token,wallet,observed_at,price_usd,liquidity_usd,
                market_cap_usd,fdv_usd,pair_created_at_ms,pair_address
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                e.tx_hash, e.token.lower(), e.wallet.lower(), int(time.time()),
                market.price_usd, market.liquidity_usd, market.market_cap_usd,
                market.fdv_usd, market.pair_created_at_ms, market.pair_address
            )
        )
        self.db.commit()

    def add_signal(self, s):
        self.db.execute(
            "INSERT INTO signals(created_at,token,side,status,score,payload) VALUES(?,?,?,?,?,?)",
            (int(time.time()), s.token, s.side, s.status, s.score, json.dumps(s.to_dict(), default=str))
        )
        self.db.commit()

    def get_position(self, token: str):
        return self.db.execute("SELECT * FROM paper_positions WHERE token=?", (token,)).fetchone()

    def open_position(self, token, symbol, ts, price, notional, qty):
        changed = self.db.execute(
            """INSERT OR IGNORE INTO paper_positions(token,symbol,opened_at,entry_price,notional_usd,quantity,status)
               VALUES(?,?,?,?,?,?, 'OPEN')""",
            (token,symbol,ts,price,notional,qty)
        ).rowcount
        self.db.commit()
        return changed == 1

    def close_position(self, token, ts, price, pnl, reason):
        changed = self.db.execute(
            """UPDATE paper_positions SET status='CLOSED',closed_at=?,exit_price=?,pnl_usd=?,exit_reason=?
               WHERE token=? AND status='OPEN'""",
            (ts,price,pnl,reason,token)
        ).rowcount
        self.db.commit()
        return changed == 1

    def open_positions(self):
        return self.db.execute("SELECT * FROM paper_positions WHERE status='OPEN'").fetchall()

    def upsert_candidate(self, token, source, symbol=None, name=None, deployer=None,
                         deployment_block=None, deployed_at=None):
        now = int(time.time())
        self.db.execute(
            """INSERT INTO launch_candidates(
                token,first_seen_at,last_checked_at,source,symbol,name,deployer,
                deployment_block,deployed_at,status
            ) VALUES(?,?,0,?,?,?,?,?,?,'CANDIDATE')
            ON CONFLICT(token) DO UPDATE SET
                source=COALESCE(launch_candidates.source, excluded.source),
                symbol=COALESCE(launch_candidates.symbol, excluded.symbol),
                name=COALESCE(launch_candidates.name, excluded.name),
                deployer=COALESCE(launch_candidates.deployer, excluded.deployer),
                deployment_block=COALESCE(launch_candidates.deployment_block, excluded.deployment_block),
                deployed_at=COALESCE(launch_candidates.deployed_at, excluded.deployed_at)
            """,
            (token.lower(), now, source, symbol, name, deployer, deployment_block, deployed_at)
        )
        self.db.commit()

    def due_candidates(self, recheck_seconds, expire_hours, limit=40):
        now = int(time.time())
        cutoff = now - expire_hours * 3600
        return self.db.execute(
            """SELECT * FROM launch_candidates
               WHERE status='CANDIDATE'
                 AND first_seen_at >= ?
                 AND last_checked_at <= ?
               ORDER BY last_checked_at ASC, first_seen_at ASC LIMIT ?""",
            (cutoff, now - recheck_seconds, limit)
        ).fetchall()

    def mark_candidate_checked(self, token):
        self.db.execute("UPDATE launch_candidates SET last_checked_at=? WHERE token=?", (int(time.time()), token.lower()))
        self.db.commit()

    def mark_candidate_status(self, token, status):
        self.db.execute("UPDATE launch_candidates SET status=?,last_checked_at=? WHERE token=?", (status, int(time.time()), token.lower()))
        self.db.commit()

    def add_launch(self, candidate, market, source):
        pair_created_at = int((market.pair_created_at_ms or int(time.time()*1000)) / 1000)
        self.db.execute(
            """INSERT OR IGNORE INTO launches(
                token,symbol,name,deployer,deployment_block,deployed_at,discovered_at,
                pair_address,pair_created_at,dex_id,first_price_usd,first_liquidity_usd,
                first_market_cap_usd,first_fdv_usd,source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate["token"].lower(), market.symbol or candidate["symbol"], candidate["name"],
                candidate["deployer"], candidate["deployment_block"], candidate["deployed_at"], int(time.time()),
                market.pair_address, pair_created_at, market.dex_id, market.price_usd, market.liquidity_usd,
                market.market_cap_usd, market.fdv_usd, source
            )
        )
        self.db.execute("UPDATE launch_candidates SET status='LIVE',last_checked_at=? WHERE token=?", (int(time.time()), candidate["token"].lower()))
        self.db.commit()

    def launches_due_snapshot(self, horizons=(5,15,60,360,1440), limit=100):
        now = int(time.time())
        out = []
        attempts = {(r["token"], r["horizon"]): r["checked_at"]
                    for r in self.db.execute("SELECT * FROM snapshot_attempts")}
        rows = self.db.execute("SELECT * FROM launches ORDER BY pair_created_at ASC").fetchall()
        for row in rows:
            for h in horizons:
                target = int(row["pair_created_at"]) + h*60
                if now < target:
                    continue
                exists = self.db.execute("SELECT 1 FROM launch_snapshots WHERE token=? AND horizon_minutes=?", (row["token"], h)).fetchone()
                if not exists:
                    out.append((row, h, target))
        out.sort(key=lambda item: (attempts.get((item[0]["token"], item[1]), 0), item[2]))
        selected = out[:limit]
        self.db.executemany("INSERT OR REPLACE INTO snapshot_attempts VALUES(?,?,?)",
                            [(r["token"], h, now) for r,h,_ in selected])
        self.db.commit()
        return selected

    def save_launch_snapshot(self, token, horizon, target_at, market):
        now = int(time.time())
        self.db.execute(
            """INSERT OR IGNORE INTO launch_snapshots(
                token,horizon_minutes,target_at,observed_at,delay_seconds,
                price_usd,liquidity_usd,market_cap_usd,fdv_usd,
                volume_m5,volume_h1,buys_m5,sells_m5
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token.lower(), horizon, target_at, now, max(0, now-target_at), market.price_usd,
             market.liquidity_usd, market.market_cap_usd, market.fdv_usd, market.volume_m5,
             market.volume_h1, market.buys_m5, market.sells_m5)
        )
        self.db.commit()

    def save_wallet_profile(self, profile):
        self.db.execute(
            """INSERT OR REPLACE INTO wallet_profiles(
                wallet,generated_at,smart_score,profile_class,recommended_weight,
                confirmation_eligible,payload
            ) VALUES(?,?,?,?,?,?,?)""",
            (profile["wallet"].lower(), profile["generated_at"], profile["smart_score"],
             profile["profile_class"], profile["recommended_weight"],
             1 if profile["confirmation_eligible"] else 0, json.dumps(profile, default=str))
        )
        self.db.commit()

    def add_research_trade(self, e):
        price = (e.usdc_amount / e.token_amount) if e.token_amount else None
        self.db.execute(
            """INSERT INTO research_trades(
               tx_hash,wallet,block_number,timestamp,side,token,symbol,
               token_amount,usdc_amount,execution_price_usd,trade_verified
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tx_hash,wallet,token,side) DO UPDATE SET
                token_amount=excluded.token_amount,usdc_amount=excluded.usdc_amount,
                execution_price_usd=excluded.execution_price_usd,trade_verified=excluded.trade_verified""",
            (e.tx_hash,e.wallet.lower(),e.block_number,e.timestamp,e.side,e.token.lower(),
             e.token_symbol,e.token_amount,e.usdc_amount,price,int(e.trade_verified))
        )
        self.db.commit()

    def upsert_discovery_candidate(self, wallet, token, rank):
        now=int(time.time())
        w=wallet.lower(); t=token.lower()
        self.db.execute(
            """INSERT OR IGNORE INTO discovery_candidates(
               wallet,first_discovered_at,last_discovered_at,source_winners,best_early_rank,status
            ) VALUES(?,?,?,0,?,'QUARANTINE')""", (w,now,now,rank)
        )
        inserted = self.db.execute("INSERT OR IGNORE INTO candidate_sources(wallet,token,early_rank,discovered_at) VALUES(?,?,?,?)", (w,t,rank,now)).rowcount
        if inserted:
            self.db.execute(
                """UPDATE discovery_candidates SET source_winners=source_winners+1,last_discovered_at=?,
                       best_early_rank=CASE WHEN best_early_rank IS NULL THEN ? WHEN ? < best_early_rank THEN ? ELSE best_early_rank END
                   WHERE wallet=?""", (now,rank,rank,rank,w)
            )
        self.db.commit()

    def save_promoted_wallet(self, wallet, score, profile_class, weight, label):
        now=int(time.time())
        self.db.execute(
            """INSERT OR REPLACE INTO promoted_wallets(wallet,promoted_at,smart_score,profile_class,weight,label,enabled)
            VALUES(?,?,?,?,?,?,1)""", (wallet.lower(),now,score,profile_class,weight,label)
        )
        self.db.execute(
            """UPDATE discovery_candidates SET status='PROMOTED',profile_score=?,profile_class=?,promotion_reason='passed_v4_gates' WHERE wallet=?""",
            (score,profile_class,wallet.lower())
        )
        self.db.commit()

V4_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_winners (
    token TEXT PRIMARY KEY,
    detected_at INTEGER NOT NULL,
    max_return_pct REAL NOT NULL,
    processed_at INTEGER
);
CREATE TABLE IF NOT EXISTS discovery_candidates (
    wallet TEXT PRIMARY KEY,
    first_discovered_at INTEGER NOT NULL,
    last_discovered_at INTEGER NOT NULL,
    source_winners INTEGER NOT NULL DEFAULT 0,
    best_early_rank INTEGER,
    status TEXT NOT NULL DEFAULT 'QUARANTINE',
    hydrated_at INTEGER,
    profile_score INTEGER,
    profile_class TEXT,
    promotion_reason TEXT
);
CREATE TABLE IF NOT EXISTS candidate_sources (
    wallet TEXT NOT NULL,
    token TEXT NOT NULL,
    early_rank INTEGER,
    discovered_at INTEGER NOT NULL,
    PRIMARY KEY(wallet, token)
);
CREATE TABLE IF NOT EXISTS research_trades (
    tx_hash TEXT NOT NULL,
    wallet TEXT NOT NULL,
    block_number INTEGER,
    timestamp INTEGER,
    side TEXT NOT NULL,
    token TEXT NOT NULL,
    symbol TEXT,
    token_amount REAL,
    usdc_amount REAL,
    execution_price_usd REAL,
    PRIMARY KEY(tx_hash, wallet, token, side)
);
CREATE INDEX IF NOT EXISTS idx_research_wallet_ts ON research_trades(wallet,timestamp);
CREATE INDEX IF NOT EXISTS idx_research_token_ts ON research_trades(token,timestamp);
CREATE TABLE IF NOT EXISTS promoted_wallets (
    wallet TEXT PRIMARY KEY,
    promoted_at INTEGER NOT NULL,
    smart_score INTEGER NOT NULL,
    profile_class TEXT NOT NULL,
    weight INTEGER NOT NULL,
    label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""
