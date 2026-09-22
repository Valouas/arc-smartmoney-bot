"""Offline research gate: median BUY-to-first-FIFO-sale, never engine authority."""
import json
import math
import re
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
from statistics import median

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')
HASH = re.compile(r'0x[0-9a-fA-F]{64}\Z')


def unknown(reason, minimum=120):
    return dict(holding_duration_status='UNKNOWN_HOLDING_DURATION',
                holding_duration_reason=reason, median_first_sale_seconds=None,
                min_holding_seconds=minimum, holding_sample_size=0,
                holding_buy_count=0, holding_unsold_buy_count=0,
                holding_fast_exit_count=None, holding_fast_exit_pct=None,
                holding_min_first_sale_seconds=None, holding_max_first_sale_seconds=None,
                holding_method='MEDIAN_BUY_TO_FIRST_FIFO_SALE',
                holding_duration_eligible=False)


def valid_minimum(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_min_holding_seconds')


def measure(transactions, cutoff_at, minimum=120):
    """One unweighted observation per acquisition lot, at its first partial exit.

    FIFO is an attribution convention, not identification of fungible units.
    Unsold lots are censored and never fabricated as zero-duration sales.
    The median describes buys with an observed exit, not still-open buys.
    Caller must provide a completely collected, non-invalidated snapshot.
    """
    valid_minimum(minimum)
    out = unknown('NO_OBSERVED_FIRST_SALE', minimum)
    lots, durations, seen, order = defaultdict(deque), [], {}, {}
    buys = 0
    try:
        if type(cutoff_at) is not int or cutoff_at <= 0:
            raise ValueError('INVALID_CUTOFF')
        records = []
        for tx in transactions:
            if not isinstance(tx, dict):
                raise ValueError('MALFORMED_TRANSACTION')
            h = tx.get('tx_hash')
            if not isinstance(h, str) or not HASH.fullmatch(h):
                raise ValueError('INVALID_TRANSACTION_HASH')
            h = h.lower()
            if h in seen:
                if seen[h] != tx:
                    raise ValueError('CONFLICTING_DUPLICATE_TRANSACTION')
                continue
            seen[h] = tx
            if tx.get('outside_snapshot') is True:
                continue
            if tx.get('error') or tx.get('inventory_gap'):
                raise ValueError('INCOMPLETE_OR_AMBIGUOUS_HISTORY')
            if any(type(tx.get(k)) is not int or tx[k] < 0 for k in ('timestamp', 'block', 'index')):
                raise ValueError('MISSING_TRANSACTION_TIMING')
            if tx['timestamp'] > cutoff_at:
                raise ValueError('TRANSACTION_AFTER_CUTOFF')
            key = tx['block'], tx['index']
            if key in order:
                raise ValueError('AMBIGUOUS_TRANSACTION_ORDER')
            order[key] = h
            records.append(tx)
        previous = -1
        for tx in sorted(records, key=lambda t: (t['block'], t['index'])):
            ts = tx['timestamp']
            if ts < previous:
                raise ValueError('NON_MONOTONIC_TIMESTAMPS')
            previous = ts
            trades = tx.get('trades')
            if not isinstance(trades, list):
                raise ValueError('MISSING_TRADE_LIST')
            tokens = set()
            for trade in trades:
                if not isinstance(trade, dict) or trade.get('confirmation_eligible') is not True:
                    raise ValueError('UNCONFIRMED_TRADE')
                token = trade.get('token')
                if not isinstance(token, str) or not ADDRESS.fullmatch(token):
                    raise ValueError('INVALID_TOKEN')
                token = token.lower()
                if token in tokens:
                    raise ValueError('AMBIGUOUS_INTRATRANSACTION_ORDER')
                tokens.add(token)
                quantity = Decimal(str(trade.get('token_amount')))
                if not quantity.is_finite() or quantity <= 0:
                    raise ValueError('INVALID_TRADE_QUANTITY')
                if trade.get('side') == 'BUY':
                    lots[token].append(dict(remaining=quantity, bought_at=ts, exited=False))
                    buys += 1
                elif trade.get('side') == 'SELL':
                    available = sum((lot['remaining'] for lot in lots[token]), Decimal(0))
                    # Same rounding tolerance as historical inventory accounting.
                    if quantity > available + available * Decimal('1e-12') or not available:
                        raise ValueError('SELL_WITHOUT_COMPLETE_BUY_HISTORY')
                    quantity = min(quantity, available)
                    while quantity > 0:
                        lot = lots[token][0]
                        if not lot['exited']:
                            durations.append(ts - lot['bought_at'])
                            lot['exited'] = True
                        used = min(quantity, lot['remaining'])
                        lot['remaining'] -= used
                        quantity -= used
                        if not lot['remaining']:
                            lots[token].popleft()
                else:
                    raise ValueError('INVALID_TRADE_SIDE')
        out.update(holding_buy_count=buys, holding_sample_size=len(durations),
                   holding_unsold_buy_count=buys-len(durations))
        if durations:
            value = median(durations)
            passed = value >= minimum
            out.update(median_first_sale_seconds=value, holding_duration_eligible=passed,
                       holding_fast_exit_count=sum(d < minimum for d in durations),
                       holding_fast_exit_pct=100 * sum(d < minimum for d in durations) / len(durations),
                       holding_min_first_sale_seconds=min(durations),
                       holding_max_first_sale_seconds=max(durations),
                       holding_duration_status='WITHIN_HOLDING_LIMIT' if passed else 'EXCLUDED_SHORT_HOLDING',
                       holding_duration_reason=None)
    except (ValueError, TypeError, KeyError, InvalidOperation) as error:
        # Only our fixed categories; never echo payloads or exception bodies.
        reason = str(error) if type(error) is ValueError else 'MALFORMED_HISTORY'
        out = unknown(reason, minimum)
    return out


def stored_holding(db, wallet, minimum=120, tables=None):
    """Use the current complete generation, else last non-invalidated completion.

    No migrations, HTTP, RPC, writes, or mixing transactions across generations.
    The evidence cutoff and retained status are exposed because timing may age.
    """
    valid_minimum(minimum)
    tables = tables if tables is not None else {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    run, table, retained = None, None, False
    if 'historical_runs' in tables:
        cursor = db.execute('SELECT * FROM historical_runs WHERE wallet=?', (wallet,))
        record = cursor.fetchone()
        if record:
            candidate = dict(zip((c[0] for c in cursor.description), record))
            if candidate.get('version') == 1 and candidate.get('phase') == 'COMPLETE':
                run, table = candidate, 'historical_transactions'
            elif candidate.get('version') != 1:
                return unknown('INVALIDATED_HISTORY', minimum)
    if run is None and 'historical_snapshots' in tables:
        record = db.execute('SELECT run,invalidated FROM historical_snapshots WHERE wallet=?', (wallet,)).fetchone()
        if record and not record[1]:
            try:
                run = json.loads(record[0])
            except (ValueError, TypeError):
                return unknown('MALFORMED_SNAPSHOT_HEADER', minimum)
            table, retained = 'historical_snapshot_transactions', True
    if not isinstance(run, dict) or table not in tables:
        return unknown('MISSING_COMPLETE_LOCAL_HISTORY', minimum)
    if run.get('version') != 1 or run.get('phase') != 'COMPLETE' or run.get('error') or run.get('cursor'):
        return unknown('INCOMPLETE_LOCAL_HISTORY', minimum)
    if run.get('wallet', wallet) != wallet or type(run.get('cutoff_block')) is not int or run['cutoff_block'] < 0:
        return unknown('INVALID_SNAPSHOT_HEADER', minimum)
    try:
        transactions = []
        for h, payload in db.execute(f'SELECT tx_hash,payload FROM {table} WHERE wallet=?', (wallet,)):
            if payload is None:
                return unknown('MISSING_TRANSACTION_PAYLOAD', minimum)
            tx = json.loads(payload)
            if not isinstance(tx, dict) or tx.get('tx_hash') != h:
                return unknown('TRANSACTION_KEY_MISMATCH', minimum)
            if not tx.get('outside_snapshot') and (type(tx.get('block')) is not int or tx['block'] > run['cutoff_block']):
                return unknown('TRANSACTION_OUTSIDE_BLOCK_COVERAGE', minimum)
            transactions.append(tx)
        out = measure(transactions, run.get('cutoff_at'), minimum)
    except (ValueError, TypeError):
        out = unknown('MALFORMED_STORED_HISTORY', minimum)
    out.update(holding_source=table, holding_cutoff_at=run.get('cutoff_at'),
               holding_cutoff_block=run.get('cutoff_block'), holding_retained_snapshot=retained)
    return out
