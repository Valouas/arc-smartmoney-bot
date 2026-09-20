"""Local read-only dashboard. No RPC, engine imports, exports or database writes."""
import csv
import json
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
HOST, PORT = '127.0.0.1', 8001
COMPONENTS = ('launch_indexer', 'wallet_profiler', 'auto_discovery', 'bot',
              'historical_analysis', 'alchemy_webhook', 'paper_copy', 'ngrok', 'dashboard')
HISTORY_FIELDS = ('wallet', 'historical_class', 'analysis_status', 'metrics_status',
                  'buys', 'closed_positions', 'open_positions_count', 'conservative_pnl_usdc',
                  'conservative_return', 'identity_cluster_size', 'paper_review_eligible',
                  'concentration_risk', 'snapshot_at', 'using_retained_snapshot',
                  'refresh_phase', 'working_transactions_decoded', 'working_transactions_queued')


@contextmanager
def read_db(path):
    # mode=ro prevents creation as well as writes. No Store/Storage initializer.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite ledger amount')
    return result


def paper_snapshot(path):
    with read_db(path) as db:
        app = db.execute("SELECT value FROM state WHERE key='application'").fetchone()
        if not app or app[0] != 'arc_paper_copy_v1':
            raise ValueError('Wrong database')
        policy = json.loads(db.execute("SELECT value FROM state WHERE key='policy'").fetchone()[0])
        initial = amount(policy['initial_cash'])
        positions = defaultdict(list)
        for p in db.execute('SELECT wallet,token,quantity,cost FROM positions'):
            if amount(p['quantity']) > 0:
                positions[p['wallet']].append({'token': p['token'], 'quantity': p['quantity'], 'cost': float(amount(p['cost']))})
        pnl, buys, sells, days = defaultdict(Decimal), Counter(), Counter(), defaultdict(Decimal)
        last_fill = None
        for f in db.execute('SELECT wallet,side,realized_pnl,filled_at FROM fills'):
            value = amount(f['realized_pnl'])
            pnl[f['wallet']] += value
            (buys if f['side'] == 'BUY' else sells)[f['wallet']] += 1
            days[int(f['filled_at']) // 86400 * 86400] += value
            last_fill = max(last_fill or 0, f['filled_at'])
        rows = []
        for w in db.execute('SELECT wallet,cash,active,selection FROM wallets'):
            selection = json.loads(w['selection'])
            cash = amount(w['cash'])
            cost = sum((amount(p['cost']) for p in positions[w['wallet']]), Decimal(0))
            rows.append({'wallet': w['wallet'], 'active': bool(w['active']), 'cash': float(cash),
                         'initial_cash': float(initial), 'realized_pnl': float(pnl[w['wallet']]),
                         'open_cost': float(cost), 'cash_only_pnl': float(cash - initial),
                         'market_equity': None, 'unrealized_pnl': None,
                         'buys': buys[w['wallet']], 'sells': sells[w['wallet']],
                         'positions': positions[w['wallet']],
                         'admission': selection.get('admission', 'HISTORICALLY_QUALIFIED')})
        rows.sort(key=lambda r: (-r['realized_pnl'], r['wallet']))
        recent = [dict(r) for r in db.execute('''SELECT wallet,token,side,filled_at,notional,fee,gas,realized_pnl
            FROM fills ORDER BY filled_at DESC,observation_id DESC LIMIT 40''')]
        progress = {r[0]: r[1] for r in db.execute("""SELECT key,value FROM state WHERE key IN
            ('next_block','webhook_source_available','live_next_block','live_head','live_checked_at',
             'live_start_block','live_uncovered_from','live_uncovered_to')""")}
        if 'live_next_block' in progress:
            progress['historical_next_block'] = progress.get('next_block')
            progress['next_block'] = progress['live_next_block']
            if 'live_head' in progress:
                progress['live_lag_blocks'] = max(0,int(progress['live_head'])-int(progress['live_next_block'])+1)
        observations = {r[0]: r[1] for r in db.execute('SELECT status,COUNT(*) FROM observations GROUP BY status')}
        gaps = db.execute('SELECT COUNT(*) FROM gaps').fetchone()[0]
        running = Decimal(0)
        curve = []
        for day, value in sorted(days.items()):
            running += value
            curve.append({'at': day, 'pnl': float(running)})
        return {'wallets': rows, 'fills': recent, 'curve': curve, 'last_fill_at': last_fill,
                'progress': progress, 'observations': observations, 'gaps': gaps,
                'totals': {'wallets': len(rows), 'active': sum(r['active'] for r in rows),
                           'initial_cash': float(initial * len(rows)),
                           **{k: sum(r[k] for r in rows) for k in ('cash', 'realized_pnl', 'open_cost', 'buys', 'sells')},
                           'open_positions': sum(len(r['positions']) for r in rows)}}


def analysis_snapshot(path):
    from historical_store import VERSION
    with read_db(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        candidates = [dict(r) for r in db.execute('''SELECT wallet,status,first_discovered_at,source_winners,
            profile_score,profile_class,history_complete FROM discovery_candidates''')]
        profiles = [dict(r) for r in db.execute('SELECT wallet,smart_score,profile_class FROM wallet_profiles')]
        profile_map = {r['wallet']: r for r in profiles}
        merged = {r['wallet']: r for r in candidates}
        for wallet, p in profile_map.items():
            row = merged.setdefault(wallet, {'wallet': wallet, 'status': 'PROFILE_ONLY'})
            row['profile_score'], row['profile_class'] = p['smart_score'], p['profile_class']
        result = {'wallets': list(merged.values()), 'statuses': dict(Counter(r['status'] for r in merged.values())),
                  'history': {}, 'transactions': None, 'scanner': {}, 'webhook': {}, 'deliveries': None}
        if 'historical_runs' in tables:
            runs = {r['wallet']: dict(r) for r in db.execute('SELECT wallet,version,phase,last_attempt,completed_at FROM historical_runs')}
            result['history'] = dict(Counter('WAITING' if w not in runs or runs[w]['version'] != VERSION else
                'COMPLETE' if runs[w]['phase'] == 'COMPLETE' else 'IN_PROGRESS' for w in merged))
            result['last_analysis_attempt'] = max((r['last_attempt'] for r in runs.values()), default=None)
        if 'historical_transactions' in tables:
            r = db.execute('SELECT COUNT(*),COUNT(payload) FROM historical_transactions').fetchone()
            result['transactions'] = {'queued': r[0], 'decoded': r[1], 'pending': r[0] - r[1]}
            if 'historical_snapshots' in tables and 'historical_snapshot_transactions' in tables:
                retained_wallets = {r[0] for r in db.execute('SELECT wallet FROM historical_snapshots WHERE invalidated=0')}
                retained = len(retained_wallets)
                result['working_history'] = result['history']
                result['history'] = dict(Counter('COMPLETE' if w in retained_wallets else
                    'WAITING' if w not in runs or runs[w]['version'] != VERSION else
                    'COMPLETE' if runs[w]['phase'] == 'COMPLETE' else 'IN_PROGRESS' for w in merged))
                total = db.execute('''SELECT COUNT(*),COALESCE(SUM(decoded),0) FROM (
                    SELECT wallet,tx_hash,MAX(decoded) decoded FROM (
                        SELECT wallet,tx_hash,payload IS NOT NULL decoded FROM historical_transactions
                        UNION ALL
                        SELECT t.wallet,t.tx_hash,1 FROM historical_snapshot_transactions t
                        JOIN historical_snapshots s ON s.wallet=t.wallet WHERE s.invalidated=0
                    ) GROUP BY wallet,tx_hash)''').fetchone()
                result['transactions'].update(working_queued=r[0], working_decoded=r[1], working_pending=r[0]-r[1],
                                              queued=total[0], decoded=total[1], pending=total[0]-total[1],
                                              retained_snapshots=retained)
        if 'scanner_state' in tables:
            result['scanner'] = {r[0]: r[1] for r in db.execute("SELECT key,value FROM scanner_state WHERE key IN ('next_block','pending_block')")}
        if 'alchemy_jobs' in tables:
            result['webhook'] = {r[0]: r[1] for r in db.execute('SELECT status,COUNT(*) FROM alchemy_jobs GROUP BY status')}
        if 'alchemy_deliveries' in tables:
            result['deliveries'] = db.execute('SELECT COUNT(*) FROM alchemy_deliveries').fetchone()[0]
        return result


def history_snapshot(path):
    # Reuse the published ranking; never reconstruct every wallet on each refresh.
    # Detailed positions/trades occupy large CSV cells even though the dashboard
    # only exposes summary columns. Bound the parser without Python's 128 KiB cap.
    csv.field_size_limit(8 * 1024 * 1024)
    with path.open(encoding='utf-8-sig', newline='') as source:
        rows = [{k: r.get(k) or None for k in HISTORY_FIELDS} for r in csv.DictReader(source)]
    for row in rows:
        for key in ('buys', 'closed_positions', 'open_positions_count', 'conservative_pnl_usdc',
                    'conservative_return', 'identity_cluster_size', 'snapshot_at',
                    'working_transactions_decoded', 'working_transactions_queued'):
            row[key] = number(row[key])
        row['paper_review_eligible'] = row['paper_review_eligible'] == 'True'
        row['using_retained_snapshot'] = row['using_retained_snapshot'] == 'True'
    return {'exported_at': path.stat().st_mtime, 'wallets': rows}


def process_snapshot(path, now):
    state = json.loads(path.read_text(encoding='utf-8'))
    fresh = 0 <= now - state['updated_at'] <= 15 and state.get('status') == 'RUNNING'
    # Only whitelisted fields: never publish paths, command lines or tunnel credentials.
    return {'fresh': fresh, 'updated_at': state['updated_at'], 'components': [
        {'name': name, 'status': 'UNKNOWN' if not fresh else 'RUNNING' if p.get('exit_code') is None else 'STOPPED'}
        for name, p in state.get('processes', {}).items() if name in COMPONENTS]}


def snapshot(root=ROOT, now=None):
    now = time.time() if now is None else now
    data = {'at': now, 'errors': {}}
    for name, reader, path in (
        ('paper', paper_snapshot, 'data/paper_copy.sqlite3'),
        ('analysis', analysis_snapshot, 'data/arc_bot.sqlite3'),
        ('history', history_snapshot, 'data/historical_wallet_ranking.csv'),
        ('processes', lambda p: process_snapshot(p, now), 'data/run_all_state.json'),
    ):
        try:
            data[name] = reader(root / path)
        except FileNotFoundError:
            data[name] = None
            data['errors'][name] = 'NOT_INITIALIZED'
        except (OSError, sqlite3.Error, csv.Error, ValueError, TypeError, KeyError, ArithmeticError):
            data[name] = None
            data['errors'][name] = 'UNAVAILABLE'
    return data


class SnapshotCache:
    def __init__(self, root=ROOT, seconds=10):
        self.root, self.seconds = root, seconds
        self.lock, self.expires, self.body = threading.Lock(), 0, None

    def get(self):
        with self.lock:
            if self.body is None or time.monotonic() >= self.expires:
                self.body = json.dumps(snapshot(self.root), allow_nan=False).encode()
                self.expires = time.monotonic() + self.seconds
            return self.body


def make_server(root=ROOT, port=PORT):
    cache = SnapshotCache(root)
    assets = {'/': ('index.html', 'text/html; charset=utf-8'),
              '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
              '/style.css': ('style.css', 'text/css; charset=utf-8')}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            authority = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if authority not in allowed or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.respond(403, b'{"error":"local_only"}')
                return
            path = urlsplit(self.path).path
            try:
                if path == '/api/overview':
                    self.respond(200, cache.get())
                elif path == '/health':
                    self.respond(200, b'{"status":"ok"}')
                elif path in assets:
                    filename, mime = assets[path]
                    self.respond(200, (root / 'dashboard_assets' / filename).read_bytes(), mime)
                else:
                    self.respond(404, b'{"error":"not_found"}')
            except (OSError, ValueError):
                self.respond(503, b'{"error":"unavailable"}')

    return ThreadingHTTPServer((HOST, port), Handler)


def main():
    with make_server() as server:
        print(f'Tableau de bord local : http://{HOST}:{PORT}', flush=True)
        try:
            server.serve_forever(poll_interval=.5)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
