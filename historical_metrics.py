"""Historical FIFO positions; no hindsight prices and no forward authority."""
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
import statistics


def amount(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid amount') from None
    if not number.is_finite() or number < 0:
        raise ValueError('invalid amount')
    return number


def analyze(transactions, cfg, as_of=None):
    inventory = defaultdict(deque)
    rounds = defaultdict(lambda: {'basis': Decimal(0), 'profit': Decimal(0), 'gas': Decimal(0)})
    closed, buys, issues = [], [], set()
    total_buy = total_sell = fees = realized = Decimal(0)
    start = end = None
    seen = set()
    for tx in sorted(transactions, key=lambda t: (t.get('block', 0), t.get('index', 0), t['tx_hash'])):
        if tx['tx_hash'] in seen:
            continue
        seen.add(tx['tx_hash'])
        if tx.get('outside_snapshot'):
            continue
        if tx.get('error'):
            issues.add(tx['error'])
            continue
        ts = tx['timestamp']
        start = ts if start is None else min(start, ts)
        end = ts if end is None else max(end, ts)
        if tx.get('gas_usdc') is None:
            issues.add('gas_cost_unknown')
        else:
            fees += amount(tx['gas_usdc'])
        if tx.get('inventory_gap'):
            issues.add('external_or_unexplained_token_movement')
        for trade in tx.get('trades', []):
            if trade.get('confirmation_eligible') is not True:
                issues.add('unverified_trade'); continue
            qty, cash = amount(trade['token_amount']), amount(trade['usdc_amount'])
            if not qty or not cash:
                issues.add('invalid_trade_amount'); continue
            token = trade['token'].lower()
            rounds[token]['gas'] += amount(tx['gas_usdc']) if tx.get('gas_usdc') is not None else Decimal(0)
            if trade['side'] == 'BUY':
                buys.append({'token': token, 'buy_usdc': float(cash), 'timestamp': ts})
                inventory[token].append([qty, cash, ts])
                total_buy += cash
                rounds[token]['basis'] += cash
            elif trade['side'] == 'SELL':
                available = sum((lot[0] for lot in inventory[token]), Decimal(0))
                tolerance = available * Decimal('1e-12')
                if qty > available + tolerance:
                    issues.add('sell_without_complete_cost_basis'); continue
                # Tolerance only for decoder floating point representation.
                remaining = available if abs(qty-available) <= tolerance else qty
                basis = Decimal(0)
                while remaining > 0 and inventory[token]:
                    lot = inventory[token][0]
                    used = min(lot[0], remaining)
                    cost = lot[1] * used / lot[0]
                    basis += cost; lot[0] -= used; lot[1] -= cost; remaining -= used
                    if lot[0] == 0:
                        inventory[token].popleft()
                profit = cash - basis
                total_sell += cash; realized += profit
                rounds[token]['profit'] += profit
                if not inventory[token]:
                    closed.append({'token': token, 'closed_at': ts,
                                   'basis_usdc': float(rounds[token]['basis']),
                                   'profit_usdc': float(rounds[token]['profit']-rounds[token]['gas'])})
                    del rounds[token]
            else:
                issues.add('invalid_trade_side')
    # Age belongs to the historical cutoff, never to the report's wall clock.
    # A partial FIFO sale removes the oldest lots and their original cost first.
    age_at = end if as_of is None else as_of
    open_positions, open_cost, weighted_age = [], Decimal(0), Decimal(0)
    for token, lots in inventory.items():
        if not lots: continue
        cost = sum((lot[1] for lot in lots), Decimal(0))
        oldest = min(lot[2] for lot in lots)
        age_known = age_at is not None and all(age_at >= lot[2] for lot in lots)
        age_cost = sum((lot[1]*Decimal(str(age_at-lot[2]))/3600 for lot in lots), Decimal(0)) if age_known else None
        open_cost += cost
        weighted_age = weighted_age+age_cost if weighted_age is not None and age_cost is not None else None
        open_positions.append({'token': token, 'quantity': str(sum((lot[0] for lot in lots), Decimal(0))),
                               'cost_basis_usdc': float(cost), 'oldest_remaining_buy_at': oldest,
                               'oldest_lot_age_hours': (age_at-oldest)/3600 if age_known else None,
                               'capital_weighted_age_hours': float(age_cost/cost) if age_cost is not None and cost else None,
                               'market_value_usdc': None, 'unrealized_pnl_usdc': None})
    gross_profit = sum(max(0, p['profit_usdc']) for p in closed)
    gross_loss = sum(max(0, -p['profit_usdc']) for p in closed)
    best = max((p['profit_usdc'] for p in closed), default=0)
    contribution = best / gross_profit if gross_profit else None
    weighted_basis = sum(p['basis_usdc'] for p in closed)
    won_basis = sum(p['basis_usdc'] for p in closed if p['profit_usdc'] > 0)
    periods = defaultdict(float)
    token_profits = defaultdict(float)
    for p in closed:
        periods[p['closed_at'] // (cfg.historical_period_hours * 3600)] += p['profit_usdc']
        token_profits[p['token']] += p['profit_usdc']
    for buy in buys:
        buy['capital_share'] = buy['buy_usdc'] / float(total_buy) if total_buy else None
    # Assign every observed top-level gas cost, including failed/non-trade txs.
    # Open inventory is assigned zero only for this conservative lower bound.
    conservative = total_sell - total_buy - fees
    cash_outlay = total_buy + fees
    # These are stress indicators, not claims that the tokens actually lost value.
    open_risk = ('INCOMPLETE_DATA' if issues else 'NO_OPEN_POSITION' if not open_positions else
                 'PROFITS_COVER_OPEN_COST' if conservative > 0 else 'OPEN_COST_NOT_COVERED')
    return {'buys': len(buys), 'closed_positions': len(closed),
            'open_positions_count': len(open_positions), 'open_cost_basis_usdc': float(open_cost),
            'open_capital_share': float(open_cost/total_buy) if total_buy else None,
            'capital_recovery_ratio': float(total_sell/cash_outlay) if cash_outlay else None,
            'conservative_return': float(conservative/cash_outlay) if cash_outlay else None,
            'open_inventory_risk': open_risk,
            'closed_only_bias': None if issues else bool(open_positions and gross_profit > gross_loss and conservative <= 0),
            'oldest_open_position_age_hours': max((p['oldest_lot_age_hours'] for p in open_positions), default=None)
                if all(p['oldest_lot_age_hours'] is not None for p in open_positions) else None,
            'open_capital_weighted_age_hours': float(weighted_age/open_cost) if weighted_age is not None and open_cost else None,
            'position_age_as_of': age_at,
            'profitable_positions': sum(p['profit_usdc'] > 0 for p in closed),
            'profitable_tokens': sorted(t for t, profit in token_profits.items() if profit > 0),
            'closed_evidence_span_seconds': max((p['closed_at'] for p in closed), default=0)-min((p['closed_at'] for p in closed), default=0),
            'profitable_periods': sum(v > 0 for v in periods.values()),
            'observed_periods': len(periods), 'period_profit_usdc': dict(periods),
            'gross_realized_pnl_usdc': float(realized), 'observed_gas_usdc': float(fees),
            'realized_less_all_observed_gas_usdc': float(realized-fees),
            'conservative_pnl_usdc': float(conservative),
            'capital_observed_usdc': float(total_buy), 'sale_proceeds_usdc': float(total_sell),
            'capital_weighted_hit_rate': won_basis/weighted_basis if weighted_basis else None,
            'capital_weighted_return': (gross_profit-gross_loss)/weighted_basis if weighted_basis else None,
            'profit_factor': gross_profit/gross_loss if gross_loss else None,
            'no_realized_losses': bool(closed and not gross_loss),
            'largest_position_contribution': contribution,
            'concentration_risk': 'HIGH' if contribution is not None and contribution > cfg.historical_max_concentration else 'LOW' if len(closed) >= cfg.profile_min_evaluable_trades else 'UNKNOWN',
            'median_buy_usdc': statistics.median(b['buy_usdc'] for b in buys) if buys else None,
            'open_positions': open_positions, 'closed_position_details': closed, 'buy_details': buys,
            'period_start': start, 'period_end': end, 'issues': sorted(issues)}


def performance_reasons(metrics, cfg):
    """Existing performance requirements, shared by qualification and reporting."""
    reasons = []
    if metrics['buys'] < cfg.promotion_min_launch_buys: reasons.append('insufficient_buys')
    if metrics['closed_positions'] < cfg.profile_min_evaluable_trades: reasons.append('insufficient_closed_positions')
    if len(metrics['profitable_tokens']) < cfg.promotion_min_winners: reasons.append('insufficient_profitable_tokens')
    if metrics['profitable_periods'] < cfg.historical_min_profitable_periods: reasons.append('regularity_not_established')
    if metrics['closed_evidence_span_seconds'] < cfg.historical_period_hours*3600: reasons.append('evidence_period_too_short')
    if metrics['conservative_pnl_usdc'] <= 0: reasons.append('conservative_profit_not_positive')
    if metrics['open_inventory_risk'] == 'OPEN_COST_NOT_COVERED':
        reasons.append('open_inventory_not_covered_by_realized_profit')
    if metrics['concentration_risk'] == 'HIGH': reasons.append('profit_concentrated_in_one_position')
    return reasons


def performance_stage(metrics, cfg):
    if not metrics['closed_positions']: return 'NO_CLOSED_POSITION'
    if metrics['realized_less_all_observed_gas_usdc'] <= 0: return 'NO_NET_PROFIT'
    if not metrics['issues'] and not performance_reasons(metrics, cfg): return 'REPEATED_POSITIVE'
    return 'PRELIMINARY_POSITIVE'


def qualify(metrics, run, identity, excluded, cfg, now):
    reasons = list(metrics['issues']) + performance_reasons(metrics, cfg)
    if excluded: reasons.append('dex_infrastructure_excluded')
    complete = bool(run and run.get('phase') == 'COMPLETE' and run.get('version') == 1)
    if not complete: reasons.append('historical_snapshot_incomplete')
    if run and run.get('error'): reasons.append('historical_collection_error')
    if not run or not 0 <= now-run.get('cutoff_at', 0) <= cfg.historical_max_age_hours*3600:
        reasons.append('historical_snapshot_stale')
    if not identity.get('eligible'): reasons.append('identity_unknown_or_shared_service')
    if identity.get('independent_profitable_deployers', 0) < cfg.promotion_min_winners:
        reasons.append('insufficient_independent_profitable_tokens')
    if identity.get('unknown_profitable_deployers', 0): reasons.append('profitable_deployer_identity_unknown')
    # No raw hit-rate cutoff, no rejection for few trades or weak sizing.
    # PROMISING is descriptive only, now reserved for repeated positive evidence.
    category = ('EXCLUDED' if excluded else 'HISTORICALLY_QUALIFIED' if not reasons else
                'PROMISING' if complete and not run.get('error')
                and performance_stage(metrics, cfg) == 'REPEATED_POSITIVE' else 'WATCH')
    return category, sorted(set(reasons))
