"""Read-only bridge from signed ingress to the paper scanner's ordered pipeline."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from web3 import Web3
from wallet_policy import infrastructure_addresses
from .selection import load_selection, SelectionError
from .policy import decimal
from paper_admission import DEFAULT_SELECTION


def watch_addresses(cfg, selection_path=DEFAULT_SELECTION,
                    paper_path='data/paper_copy.sqlite3', now=None):
    """Paper-admitted buys plus existing exits, without creating a database."""
    now=int(time.time()) if now is None else now
    try: wallets=set(load_selection(selection_path,cfg,now))
    except SelectionError: wallets=set()
    path=Path(paper_path).resolve()
    if path.exists():
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.execute('BEGIN')
            marker=db.execute("SELECT value FROM state WHERE key='application'").fetchone()
            if not marker or marker[0]!='arc_paper_copy_v1': raise ValueError('Not a paper copy database')
            for wallet,quantity in db.execute('SELECT wallet,quantity FROM positions'):
                if decimal(quantity)>0: wallets.add(Web3.to_checksum_address(wallet).lower())
            wallets.update(r[0] for r in db.execute("SELECT DISTINCT wallet FROM observations WHERE status='PENDING'"))
    return wallets-infrastructure_addresses(cfg)


class LiveWakeup:
    """Hints wake RPC verification; they never advance either scanner checkpoint.

    A full sequential scan remains necessary: notifications can be missing or
    reordered and a sale fraction depends on *all* earlier token transfers.
    """
    def __init__(self, store, source, policy, fallback_seconds=15):
        self.store,self.db,self.source,self.policy=store,store.db,Path(source).resolve(),policy
        if self.source==store.path: raise ValueError('Webhook source must differ from paper ledger')
        self.fallback_seconds=fallback_seconds
        self.next_scan=0
        self.selection=None
        self.available=False

    def refresh(self, watched, now):
        cursor=int(self.store.get('webhook_source_cursor') or 0)
        if self.store.get('webhook_source_path')!=str(self.source): cursor=0
        try:
            with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
                # Read one consistent snapshot. An absent/old receiver is simply
                # unavailable; the original polling scanner remains functional.
                source.execute('BEGIN')
                maximum=source.execute('SELECT COALESCE(MAX(rowid),0) FROM alchemy_paper_triggers').fetchone()[0]
                if maximum<cursor: cursor=0
                rows=source.execute('SELECT rowid,tx_hash,wallet,received_at FROM alchemy_paper_triggers WHERE rowid>? ORDER BY rowid LIMIT 200',(cursor,)).fetchall()
            self.available=True
        except (sqlite3.Error,OSError):
            rows=[]; self.available=False
        with self.db:
            for rowid,tx,wallet,received in rows:
                self.db.execute('INSERT OR IGNORE INTO webhook_triggers(tx_hash,wallet,received_at) VALUES(?,?,?)',(tx,wallet,received))
                cursor=rowid
            if self.available:
                self.store.set('webhook_source_cursor',cursor)
                self.store.set('webhook_source_path',str(self.source))
            self.store.set('webhook_source_available',int(self.available))
            for tx,wallet,received in self.db.execute("SELECT tx_hash,wallet,received_at FROM webhook_triggers WHERE status='PENDING'").fetchall():
                status=None
                if self.db.execute('SELECT 1 FROM scanned_transactions WHERE tx_hash=? AND wallet=?',(tx,wallet)).fetchone():
                    status='SCANNED'
                elif wallet not in watched: status='IGNORED_NOT_WATCHED'
                elif not 0 <= now-received <= self.policy.max_signal_age: status='EXPIRED'
                if status:
                    self.db.execute('UPDATE webhook_triggers SET status=? WHERE tx_hash=? AND wallet=?',(status,tx,wallet))
        return self.db.execute("SELECT 1 FROM webhook_triggers WHERE status='PENDING' LIMIT 1").fetchone() is not None

    def due(self, selected, watched, now):
        pending=self.refresh(watched,now)
        return (pending or not self.available or now>=self.next_scan
                or json.dumps(selected,sort_keys=True)!=self.selection)

    def scanned(self, selected, blocks, now):
        self.selection=json.dumps(selected,sort_keys=True)
        # Continue catching up without waiting for another notification.
        delay=0 if blocks>=self.policy.block_window else self.fallback_seconds
        self.next_scan=now+delay
