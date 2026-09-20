"""Shared UTC-day request budget. Reserve every HTTP attempt before transport."""
import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from external_http import ExternalError


class ZerionBudget:
    def __init__(self, path, key, limit=280, clock=time.time):
        if not key or type(limit) is not int or limit < 1:
            raise ValueError('Invalid Zerion budget')
        self.path, self.limit, self.clock = Path(path), limit, clock
        self.account = hashlib.sha256(key.encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                account TEXT NOT NULL, utc_day INTEGER NOT NULL, used INTEGER NOT NULL,
                PRIMARY KEY(account,utc_day))''')

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        return db

    def reserve(self):
        now=self.clock(); day=int(now//86400)
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,0)',(self.account,day))
            used=db.execute('SELECT used FROM requests WHERE account=? AND utc_day=?',(self.account,day)).fetchone()[0]
            if used >= self.limit:
                raise ExternalError('daily_request_budget',max(1,(day+1)*86400-now))
            db.execute('UPDATE requests SET used=used+1 WHERE account=? AND utc_day=?',(self.account,day))
            db.commit()
        finally:
            db.close()

    def seed_at_least(self, used):
        """Account for requests made before the ledger existed; never lower usage."""
        if type(used) is not int or used < 0: raise ValueError('Invalid baseline')
        db=self.connect()
        try:
            with db:
                db.execute('''INSERT INTO requests VALUES(?,?,?) ON CONFLICT(account,utc_day)
                    DO UPDATE SET used=MAX(requests.used,excluded.used)''',
                    (self.account,int(self.clock()//86400),used))
        finally: db.close()

    def used(self):
        db=self.connect()
        try:
            row=db.execute('SELECT used FROM requests WHERE account=? AND utc_day=?',
                           (self.account,int(self.clock()//86400))).fetchone()
            return row[0] if row else 0
        finally: db.close()
