"""Collection order only: never modifies decoding, qualification or authority."""
import json
import time

from arcscan import ArcscanError
from historical_store import VERSION
from rpc_transport import RPCError
from wallet_policy import is_dex_infrastructure
from discovery_sources import observed_sources


SCHEMA = '''
CREATE TABLE IF NOT EXISTS historical_priority_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), slot INTEGER NOT NULL DEFAULT 0,
 sequence INTEGER NOT NULL DEFAULT 0, focus TEXT, remaining INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO historical_priority_state(singleton) VALUES(1);
CREATE TABLE IF NOT EXISTS historical_priority_wallets (
 wallet TEXT PRIMARY KEY, rounds INTEGER NOT NULL DEFAULT 0,
 served INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
 activity_generation TEXT, last_activity INTEGER
);
'''


class PriorityCollector:
    """Three focused steps then one oldest-served step, persisted across runs.

    Priority rounds rotate bounded bursts between candidates. Neither a huge
    wallet nor repeated failures may monopolize collection. RPC limits, page
    validation, payload reuse and snapshot publication belong to the unchanged
    HistoricalCollector. This scheduler has no trading or promotion authority.
    """
    def __init__(self, collector):
        self.collector = collector
        self.db, self.cfg = collector.db, collector.cfg
        self.db.executescript(SCHEMA)

    def refresh_interval(self, row):
        base = self.cfg.historical_refresh_seconds
        if row['phase'] != 'COMPLETE' or row['version'] != VERSION or row['error']:
            return base
        generation = json.dumps([row['completed_at'], row['cutoff_block'], row['cutoff_hash']])
        if row['activity_generation'] != generation:
            latest, known = None, True
            for saved in self.db.execute('SELECT payload FROM historical_transactions WHERE wallet=?', (row['wallet'],)):
                try:
                    payload = json.loads(saved[0])
                    if payload.get('outside_snapshot'): continue
                    stamp = payload.get('timestamp')
                    if type(stamp) is not int or not 0 <= stamp <= row['cutoff_at']:
                        known = False; break
                    latest = max(latest or 0, stamp)
                except (ValueError, TypeError, AttributeError):
                    known = False; break
            row['last_activity'] = latest if known else None
            with self.db:
                self.db.execute('''UPDATE historical_priority_wallets
                    SET activity_generation=?,last_activity=? WHERE wallet=?''',
                    (generation, row['last_activity'], row['wallet']))
        latest = row['last_activity']
        # Unknown/empty evidence is never taken as proof of inactivity. Limit
        # deferral to half the existing freshness horizon, without extending it.
        if latest is not None and row['cutoff_at']-latest >= self.cfg.historical_inactive_days*86400:
            return max(base, min(self.cfg.historical_inactive_refresh_seconds,
                                 self.cfg.historical_max_age_hours*1800))
        return base

    def candidates(self, now):
        sources = observed_sources(self.db, self.cfg)
        tiers = {}
        for wallet, token in self.db.execute('SELECT wallet,token FROM candidate_sources'):
            tiers[wallet] = max(tiers.get(wallet, 0), sources.get(token.lower(), {}).get('tier', 0))
        rows = self.db.execute('''SELECT c.wallet,c.first_discovered_at,c.source_winners,c.best_early_rank,
            r.phase,r.version,r.completed_at,r.cutoff_block,r.cutoff_hash,r.cutoff_at,r.error,
            p.rounds,p.served,p.retry_at,p.activity_generation,p.last_activity
            FROM discovery_candidates c LEFT JOIN historical_runs r ON r.wallet=c.wallet
            LEFT JOIN historical_priority_wallets p ON p.wallet=c.wallet''').fetchall()
        due, deferred = {}, 0
        for source in rows:
            row = dict(source)
            row['source_tier'] = tiers.get(row['wallet'], 0)
            if is_dex_infrastructure(row['wallet'], self.cfg): continue
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO historical_priority_wallets(wallet) VALUES(?)', (row['wallet'],))
            for field in ('rounds', 'served', 'retry_at'): row[field] = row[field] or 0
            if row['retry_at'] > now: continue
            interval = self.refresh_interval(row)
            if (row['phase'] == 'COMPLETE' and row['version'] == VERSION
                    and row['completed_at'] is not None and now-row['completed_at'] < interval):
                if now-row['completed_at'] >= self.cfg.historical_refresh_seconds: deferred += 1
                continue
            due[row['wallet']] = row
        return due, deferred

    def cycle(self):
        now = int(time.time())
        due, deferred = self.candidates(now)
        state = dict(self.db.execute('SELECT * FROM historical_priority_state WHERE singleton=1').fetchone())
        steps = errors = fair_steps = 0
        for _ in range(self.cfg.discovery_max_wallets_per_cycle):
            if not due: break
            fair = state['slot'] == 3
            if fair:
                row = min(due.values(), key=lambda r:(r['served'], r['first_discovered_at'], r['wallet']))
            else:
                if state['focus'] not in due or state['remaining'] <= 0:
                    # Discovery strength is a queue preference, never proof of
                    # realized profit or independent identities.
                    row = min(due.values(), key=lambda r:(r['rounds'], -r['source_tier'],
                        -r['source_winners'], r['best_early_rank'] or 10**9,
                        r['phase'] == 'COMPLETE', r['served'], r['first_discovered_at'], r['wallet']))
                    state['focus'], state['remaining'] = row['wallet'], self.cfg.historical_focus_steps
                    row['rounds'] += 1
                row = due[state['focus']]
                state['remaining'] -= 1
            wallet = row['wallet']
            state['slot'] = (state['slot']+1) % 4
            state['sequence'] += 1
            row['served'] = state['sequence']
            # Persist before transport: a crash does not replay a priority burst
            # forever. No evidence/checkpoint is advanced by this reservation.
            with self.db:
                self.db.execute('UPDATE historical_priority_state SET slot=?,sequence=?,focus=?,remaining=? WHERE singleton=1',
                    (state['slot'], state['sequence'], state['focus'], state['remaining']))
                self.db.execute('UPDATE historical_priority_wallets SET served=?,rounds=? WHERE wallet=?',
                    (row['served'], row['rounds'], wallet))
            try:
                self.collector.step(wallet, now)
                steps += 1
                fair_steps += int(fair)
                run = self.db.execute('SELECT phase,version FROM historical_runs WHERE wallet=?', (wallet,)).fetchone()
                if run and run['phase'] == 'COMPLETE' and run['version'] == VERSION:
                    due.pop(wallet)
            except (RPCError, ArcscanError):
                errors += 1
                due.pop(wallet)
                with self.db:
                    self.db.execute('UPDATE historical_priority_wallets SET retry_at=? WHERE wallet=?',
                                    (int(time.time())+self.cfg.historical_retry_seconds, wallet))
                    self.db.execute('''INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,
                        cutoff_at,cutoff_hash,phase,last_attempt,error) VALUES(?,0,?,0,0,'','FETCHING',?,'transport_or_coverage_incomplete')
                        ON CONFLICT(wallet) DO UPDATE SET last_attempt=excluded.last_attempt,error=excluded.error''',
                        (wallet, now, now))
        return {'wallet_steps': steps, 'incomplete_steps': errors, 'queue_mode': 'focused',
                'fair_steps': fair_steps, 'inactive_refreshes_deferred': deferred}
