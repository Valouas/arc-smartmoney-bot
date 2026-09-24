"""Observed token multiples are discovery hints, never wallet profit evidence."""
from decimal import Decimal, InvalidOperation
import math


def positive(value):
    if value is None or isinstance(value, bool): return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def observed_sources(db, cfg):
    """First recorded liquid quote -> highest subsequently observed liquid quote.

    LaunchIndexer records snapshots only on the original pair. Missing or
    illiquid prices are not substituted. This is sampled local coverage, not
    all-time price history, guaranteed exit liquidity, or a trader's return.
    """
    minimum = Decimal(str(cfg.discovery_min_multiple))
    priority = Decimal(str(cfg.discovery_priority_multiple))
    liquidity_floor = Decimal(str(cfg.launch_min_liquidity_usd))
    result = {}
    rows = db.execute('''SELECT l.token,l.pair_address,l.discovered_at,l.first_price_usd,
        l.first_liquidity_usd,s.price_usd,s.liquidity_usd,s.observed_at
        FROM launches l JOIN launch_snapshots s ON s.token=lower(l.token)
        WHERE s.observed_at>=l.discovered_at''')
    for row in rows:
        base, peak = positive(row['first_price_usd']), positive(row['price_usd'])
        base_liq, peak_liq = positive(row['first_liquidity_usd']), positive(row['liquidity_usd'])
        if (not row['pair_address'] or base is None or peak is None
                or base_liq is None or peak_liq is None
                or min(base_liq, peak_liq) < liquidity_floor): continue
        multiple = peak/base
        # SQLite REAL cannot safely persist arbitrarily large malformed ratios.
        if multiple < minimum or not math.isfinite(float((multiple-1)*100)): continue
        token = row['token'].lower()
        if token in result and result[token]['multiple'] >= multiple: continue
        result[token] = {'multiple': multiple, 'tier': 2 if multiple >= priority else 1,
                         'reference_at': row['discovered_at'], 'peak_observed_at': row['observed_at']}
    return result
