"""Descriptive forward capital allocation. Never imported by scoring/promotion."""
from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class SizingPolicy:
    medium_buys: int = 5
    high_buys: int = 10
    concentration_threshold: float = .8

    def __post_init__(self):
        if not 1 <= self.medium_buys < self.high_buys or not 0 < self.concentration_threshold <= 1:
            raise ValueError("Invalid reporting sizing policy")


def sizing_policy():
    return SizingPolicy(int(os.getenv('SIZING_MEDIUM_BUYS', '5')),
                        int(os.getenv('SIZING_HIGH_BUYS', '10')),
                        float(os.getenv('SIZING_CONCENTRATION_THRESHOLD', '.8')))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def confidence(count, policy):
    return "HIGH" if count >= policy.high_buys else "MEDIUM" if count >= policy.medium_buys else "LOW"


def describe_buy(amount, prior_amounts, policy=SizingPolicy(), *, cohort_known=True):
    """Causal baseline: prior buys only; current buy enters capital share once."""
    prior = [x for x in prior_amounts if finite(x) and x > 0]
    amount = amount if finite(amount) and amount > 0 else None
    usual = median(prior) if len(prior) >= policy.medium_buys else None
    multiplier = amount/usual if amount is not None and usual else None
    label = None if multiplier is None else (
        "TINY" if multiplier < .25 else "SMALL" if multiplier < .75 else
        "NORMAL" if multiplier < 2 else "LARGE" if multiplier < 5 else "EXCEPTIONAL")
    return dict(buy_usdc=amount, usual_buy_usdc=usual, size_multiplier=multiplier,
                capital_share=amount/(sum(prior)+amount) if amount and cohort_known else None,
                sizing_confidence=confidence(len(prior), policy), conviction_label=label,
                position_size_percentile=100*sum(x <= amount for x in prior)/len(prior)
                    if usual and amount else None)


def forward_buys(db, now, wallet=None):
    """Read observed BUY rows, deduplicated per transaction/wallet/token.

    Outcomes are only the engine's stored fixed-horizon observations. No late
    quote, historical winner max, invented zero, or repeated-entry outcome reuse.
    """
    epoch = db.execute("SELECT started_at FROM observation_epochs WHERE name='launch_universe'").fetchone()
    if not epoch:
        return {}
    sql = """SELECT e.*,o.status AS outcome_status,o.price AS outcome_price,
                    o.entry_at,o.target_at,o.deadline_at,o.observed_at
             FROM events e
             LEFT JOIN discovery_candidates c ON lower(c.wallet)=lower(e.wallet)
             LEFT JOIN trade_outcomes o ON o.wallet=lower(e.wallet)
                  AND o.tx_hash=lower(e.tx_hash) AND o.token=lower(e.token)
             WHERE e.side='BUY' AND e.trade_verified=1
               AND e.timestamp>=max(?,coalesce(c.first_discovered_at,0)) AND e.timestamp<=?"""
    params = [epoch[0], now]
    if wallet is not None:
        sql += " AND lower(e.wallet)=?"
        params.append(wallet.lower())
    sql += " ORDER BY e.timestamp,e.block_number,e.id"
    result, seen = defaultdict(list), set()
    for raw in db.execute(sql, params):
        row = dict(raw)
        key = (row['wallet'].lower(), row['tx_hash'].lower(), row['token'].lower())
        if key in seen:
            continue
        seen.add(key)
        amount, quantity = row['usdc_amount'], row['token_amount']
        if not finite(amount) or amount <= 0 or not finite(quantity) or quantity <= 0:
            continue
        ret = None
        entry_price = amount/quantity
        if (row['outcome_status'] == 'OBSERVED' and row['entry_at'] == row['timestamp']
                and finite(entry_price) and entry_price > 0
                and finite(row['outcome_price']) and row['outcome_price'] >= 0
                and all(finite(row[k]) for k in ('target_at','observed_at','deadline_at'))
                and row['entry_at'] <= row['target_at'] <= row['observed_at'] <= min(row['deadline_at'], now)):
            value = row['outcome_price']/entry_price-1
            if finite(value) and finite(amount*value):
                ret = value
        result[key[0]].append(dict(wallet=key[0], tx_hash=key[1], token=key[2],
            timestamp=row['timestamp'], buy_usdc=amount, return_ratio=ret))
    return dict(result)


def allocation_metrics(trades, policy=SizingPolicy(), winner_return_pct=100):
    sizes = [t['buy_usdc'] for t in trades]
    total = sum(sizes)
    evaluated = [t for t in trades if t.get('return_ratio') is not None]
    evaluated_capital = sum(t['buy_usdc'] for t in evaluated)
    profits = [t['buy_usdc']*t['return_ratio'] for t in evaluated]
    gains, losses = sum(max(p, 0) for p in profits), -sum(min(p, 0) for p in profits)
    net, best = sum(profits), max([0]+profits)
    baseline = median(sizes) if sizes else None
    large = [t for t in evaluated if baseline is not None and t['buy_usdc'] >= 2*baseline]
    large_capital = sum(t['buy_usdc'] for t in large)
    gross_share = best/gains if gains else None
    return dict(
        sizing_forward_buys=len(trades), capital_total_observed=total,
        capital_evaluated=evaluated_capital,
        outcome_capital_coverage=evaluated_capital/total if total else None,
        capital_weighted_hit_rate=sum(t['buy_usdc'] for t in evaluated if t['return_ratio'] > 0)/evaluated_capital if evaluated_capital else None,
        capital_weighted_return=net/evaluated_capital if evaluated_capital else None,
        profit_factor=gains/losses if losses else None,
        profit_factor_status="CALCULABLE" if losses else "NO_LOSSES_OBSERVED" if evaluated else "NO_OUTCOMES",
        median_buy_usdc=baseline, usual_buy_usdc=baseline if len(sizes) >= policy.medium_buys else None,
        largest_buy_usdc=max(sizes) if sizes else None,
        largest_trade_contribution=best/net if net > 0 else None,
        largest_positive_profit_share=gross_share,
        conviction_accuracy=sum(t['buy_usdc'] for t in large if t['return_ratio'] > 0)/large_capital
            if large_capital and len(sizes) >= policy.medium_buys else None,
        sizing_confidence=confidence(len(sizes), policy),
        allocation_evidence_confidence=confidence(len({t['token'] for t in evaluated}), policy)
            if total and evaluated_capital == total and gross_share is not None
                and gross_share < policy.concentration_threshold else 'LOW',
        concentration_risk="HIGH" if gross_share is not None and gross_share >= policy.concentration_threshold
            else "LOW" if gross_share is not None else "UNKNOWN",
        capital_forward_winners=sum(t['return_ratio'] >= winner_return_pct/100 for t in evaluated),
        capital_evaluable_trades=len(evaluated), observed_horizon_pnl=net if evaluated else None,
        # Full per-trade details are exported as JSON within the wallet CSV.
        capital_share_per_trade=[dict(t, capital_share=t['buy_usdc']/total,
            position_size_percentile=100*sum(x <= t['buy_usdc'] for x in sizes)/len(sizes)
                if len(sizes) >= policy.medium_buys else None) for t in trades],
    )


def event_sizing(db, event, policy=SizingPolicy()):
    epoch = db.execute("SELECT started_at FROM observation_epochs WHERE name='launch_universe'").fetchone()
    candidate = db.execute('SELECT first_discovered_at FROM discovery_candidates WHERE lower(wallet)=?',
                           (event.wallet.lower(),)).fetchone()
    known = bool(epoch and event.timestamp >= max(epoch[0], candidate[0] if candidate else 0))
    trades = forward_buys(db, event.timestamp, event.wallet).get(event.wallet.lower(), []) if known else []
    # Same-second *already persisted* trades are causal; exclude replay/current key.
    prior = [t['buy_usdc'] for t in trades if (t['tx_hash'],t['token']) != (event.tx_hash.lower(),event.token.lower())]
    return describe_buy(event.usdc_amount, prior, policy, cohort_known=known)
