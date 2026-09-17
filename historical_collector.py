"""Resumable, fair historical reconstruction independent of scanner checkpoints."""
import json
import time
from decimal import Decimal

from arcscan import ArcscanError, require_complete_history
from decoder import classify_trade, wallet_token_deltas, hex_value
from discovery_parser import txhash
from historical_metrics import analyze
from historical_store import VERSION, initialize
from identity_graph import IdentityGraph
from rpc import USDC_ERC20, NATIVE_MIRROR
from rpc_transport import RPCError
from wallet_policy import is_dex_infrastructure


def parse_page(data, cutoff, seen):
    if (not isinstance(data, dict) or not isinstance(data.get('page'), dict)
            or not isinstance(data['page'].get('coverage'), dict)):
        raise ArcscanError('historical page invalid')
    coverage = data['page']['coverage']
    if (type(coverage.get('to_block')) is not int
            or type(coverage.get('last_contiguous_block')) is not int):
        raise ArcscanError('historical coverage invalid')
    require_complete_history(data)
    page = data['page']
    nxt = page.get('next')
    if (type(page.get('has_more')) is not bool
            or (page['has_more'] and (not isinstance(nxt, str) or not nxt))
            or (not page['has_more'] and nxt is not None)
            or page['coverage']['to_block'] < cutoff
            or page.get('capped') or page.get('truncated')):
        raise ArcscanError('historical page invalid')
    if nxt is not None and nxt in seen:
        raise ArcscanError('historical cursor cycle')
    hashes = []
    for row in data['items']:
        h = txhash(row)
        if not h: raise ArcscanError('historical transaction hash missing')
        hashes.append(h)
    return sorted(set(hashes)), nxt


class HistoricalCollector:
    def __init__(self, storage, rpc, arc, cfg):
        self.storage, self.db, self.rpc, self.arc, self.cfg = storage, storage.db, rpc, arc, cfg
        initialize(self.db)

    def start(self, wallet, now):
        data = self.arc.address_activity(wallet, limit=self.cfg.discovery_page_size, cursor=None)
        hashes, nxt = parse_page(data, 0, [])
        cutoff = min(max(0, self.rpc.block_number()-2), data['page']['coverage']['to_block'])
        block = self.rpc.get_block(cutoff, False)
        with self.db:
            self.db.execute('DELETE FROM historical_transactions WHERE wallet=?', (wallet,))
            self.db.execute('INSERT OR REPLACE INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,cursor,seen,last_attempt) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (wallet, VERSION, now, cutoff, int(block['timestamp']), hex_value(block['hash']),
                 'FETCHING' if nxt else 'DECODING', nxt, json.dumps([nxt] if nxt else []), now))
            self.db.executemany('INSERT OR IGNORE INTO historical_transactions(wallet,tx_hash) VALUES(?,?)',
                                [(wallet, h) for h in hashes])

    def transaction(self, wallet, h, run):
        tx = self.rpc.w3.eth.get_transaction(h)
        if int(tx['blockNumber']) > run['cutoff_block']:
            return {'tx_hash': h, 'outside_snapshot': True}
        receipt = self.rpc.get_receipt(h)
        block = self.rpc.get_block(int(tx['blockNumber']), False)
        if (receipt['blockHash'] != block['hash'] or receipt['blockNumber'] != tx['blockNumber']
                or tx.get('blockHash') != block['hash']):
            raise ArcscanError('historical block mismatch')
        gas = None
        if str(tx.get('from', '')).lower() == wallet:
            used, price = receipt.get('gasUsed'), receipt.get('effectiveGasPrice')
            if isinstance(used, int) and isinstance(price, int) and used >= 0 and price >= 0:
                gas = str(Decimal(used) * Decimal(price) / Decimal(10**18))
        out = {'tx_hash': h, 'block': int(tx['blockNumber']), 'index': int(receipt['transactionIndex']),
               'timestamp': int(block['timestamp']), 'gas_usdc': gas, 'trades': []}
        if int(receipt['status']) != 1:
            if str(tx.get('from', '')).lower() != wallet: out['gas_usdc'] = '0'
            return out
        try:
            trades = classify_trade(self.rpc, receipt, wallet, 1e-18,
                native_value_wei=int(tx.get('value', 0)) if str(tx.get('from', '')).lower() == wallet else 0,
                strict=True)
            if any(t['confirmation_eligible'] is not True for t in trades):
                raise ValueError('unverified swap')
            deltas = wallet_token_deltas(receipt, wallet)
            # Incoming-only transfers are paid by the transaction sender.
            # A relayed swap/outflow still has unknown wallet-level gas costs.
            if out['gas_usdc'] is None and not trades and not any(v < 0 for v in deltas.values()):
                out['gas_usdc'] = '0'
            non_cash = {t.lower() for t, v in deltas.items()
                        if v and t not in (USDC_ERC20, NATIVE_MIRROR)}
            out['inventory_gap'] = bool(non_cash - {t['token'].lower() for t in trades})
            out['trades'] = trades
        except RPCError:
            raise
        except (ValueError, KeyError, TypeError, OverflowError):
            out['error'] = 'unsupported_or_ambiguous_reconstruction'
        return out

    def identity(self, wallet, metrics):
        deployers = {}
        for token in metrics['profitable_tokens']:
            row = self.db.execute('SELECT deployer,deployed_at,deployment_block FROM launch_candidates WHERE token=?', (token,)).fetchone()
            first_buy = min(b['timestamp'] for b in metrics['buy_details'] if b['token'] == token)
            if row and row[0] and row[1] is not None and row[2] is not None and row[1] <= first_buy:
                deployers[token] = row[0].lower()
        graph = IdentityGraph(self.arc, self.cfg.identity_cache_seconds, self.cfg.identity_workers)
        graph.refresh(sorted({wallet, *deployers.values()}))
        components = graph.cluster_map()
        component = lambda w: sorted(components.get(graph.identity_id(w), [w]))
        independent = {min(component(d)) for d in deployers.values() if graph.eligible(d)}
        facts = graph.facts.get(wallet)
        return {'eligible': graph.eligible(wallet), 'members': component(wallet),
                'cluster': min(component(wallet)), 'funder': facts.funded_by if facts else None,
                'shared_service': facts.shared_service if facts else False,
                'independent_profitable_deployers': len(independent),
                'unknown_profitable_deployers': len(metrics['profitable_tokens'])-sum(graph.eligible(d) for d in deployers.values()),
                'checked_at': int(time.time())}

    def step(self, wallet, now=None):
        now = int(time.time()) if now is None else now
        if is_dex_infrastructure(wallet, self.cfg): return
        run = self.db.execute('SELECT * FROM historical_runs WHERE wallet=?', (wallet,)).fetchone()
        if not run or run['version'] != VERSION or (run['phase'] == 'COMPLETE'
                and now-run['completed_at'] >= self.cfg.historical_refresh_seconds):
            self.start(wallet, now)
            return
        if run['phase'] == 'COMPLETE': return
        # Fairness is persisted even on a transport failure. A difficult wallet
        # cannot occupy the first N positions forever.
        with self.db:
            self.db.execute('UPDATE historical_runs SET last_attempt=?,error=NULL WHERE wallet=?', (now, wallet))
        try:
            if run['phase'] == 'FETCHING':
                data = self.arc.address_activity(wallet, limit=self.cfg.discovery_page_size, cursor=run['cursor'])
                seen = json.loads(run['seen'])
                hashes, nxt = parse_page(data, run['cutoff_block'], seen)
                with self.db:
                    self.db.executemany('INSERT OR IGNORE INTO historical_transactions(wallet,tx_hash) VALUES(?,?)',
                                        [(wallet, h) for h in hashes])
                    self.db.execute('UPDATE historical_runs SET cursor=?,seen=?,phase=? WHERE wallet=?',
                        (nxt, json.dumps(seen+[nxt] if nxt else seen), 'FETCHING' if nxt else 'DECODING', wallet))
            else:
                queue = self.db.execute('SELECT tx_hash FROM historical_transactions WHERE wallet=? AND payload IS NULL ORDER BY tx_hash LIMIT ?',
                                        (wallet, self.cfg.historical_transactions_per_step)).fetchall()
                for row in queue:
                    payload = self.transaction(wallet, row[0], run)
                    with self.db:
                        self.db.execute('UPDATE historical_transactions SET payload=? WHERE wallet=? AND tx_hash=?',
                                        (json.dumps(payload, allow_nan=False), wallet, row[0]))
                pending = self.db.execute('SELECT 1 FROM historical_transactions WHERE wallet=? AND payload IS NULL LIMIT 1', (wallet,)).fetchone()
                if not pending:
                    block = self.rpc.get_block(run['cutoff_block'], False)
                    if hex_value(block['hash']) != run['cutoff_hash']:
                        with self.db:
                            self.db.execute('UPDATE historical_runs SET version=0 WHERE wallet=?', (wallet,))
                        raise ArcscanError('historical snapshot reorganization')
                    rows = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM historical_transactions WHERE wallet=?', (wallet,))]
                    identity = self.identity(wallet, analyze(rows, self.cfg))
                    with self.db:
                        self.db.execute('UPDATE historical_runs SET phase=?,completed_at=?,identity=? WHERE wallet=?',
                                        ('COMPLETE', now, json.dumps(identity), wallet))
        except (RPCError, ArcscanError):
            with self.db:
                self.db.execute('UPDATE historical_runs SET error=? WHERE wallet=?', ('transport_or_coverage_incomplete', wallet))
            raise

    def cycle(self):
        now = int(time.time())
        rows = self.db.execute('''SELECT c.wallet FROM discovery_candidates c
            LEFT JOIN historical_runs r ON r.wallet=c.wallet
            WHERE r.wallet IS NULL OR r.version<>? OR r.phase<>'COMPLETE' OR r.completed_at<=?
            ORDER BY COALESCE(r.last_attempt,0),c.first_discovered_at,c.wallet''',
            (VERSION, now-self.cfg.historical_refresh_seconds)).fetchall()
        selected = [r[0] for r in rows if not is_dex_infrastructure(r[0], self.cfg)][:self.cfg.discovery_max_wallets_per_cycle]
        completed = errors = 0
        for wallet in selected:
            try: self.step(wallet, now); completed += 1
            except (RPCError, ArcscanError):
                errors += 1
                with self.db:
                    self.db.execute('''INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,last_attempt,error)
                        VALUES(?,0,?,0,0,'','FETCHING',?,'transport_or_coverage_incomplete')
                        ON CONFLICT(wallet) DO UPDATE SET last_attempt=excluded.last_attempt,error=excluded.error''', (wallet,now,now))
        return {'wallet_steps': completed, 'incomplete_steps': errors}
