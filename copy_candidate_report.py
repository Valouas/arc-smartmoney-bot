"""Offline shortlist research. No provider clients, HTTP, or engine writes."""
import argparse
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')


def shortlist(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('purpose') != 'copy_research_shortlist'
            or not isinstance(data.get('wallets'), list)):
        raise ValueError('invalid_shortlist_schema')
    wallets = data['wallets']
    if any(not isinstance(w, str) or not ADDRESS.fullmatch(w) for w in wallets):
        raise ValueError('invalid_shortlist_address')
    return list(dict.fromkeys(w.lower() for w in wallets))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def wilson(wins, n):
    """95% Wilson lower bound, descriptive: token outcomes are not independent trials."""
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    return (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)


def frequency(payload, maximum=50):
    """Mean over the exact stored UTC window, not active days or peak frequency."""
    result = dict(frequency_status='UNKNOWN_FREQUENCY', operations=None,
                  observation_days=None, operations_per_day=None,
                  max_operations_per_day=maximum)
    try:
        count = payload['summary']['traded_times']
        start = datetime.fromisoformat(payload['date']['from'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(payload['date']['to'].replace('Z', '+00:00'))
        if (not number(count) or count < 0 or int(count) != count
                or start.utcoffset() is None or end.utcoffset() is None):
            return result
        days = (end-start).total_seconds()/86400
        if days <= 0:
            return result
        rate = count/days
        result.update(operations=int(count), observation_days=days, operations_per_day=rate,
                      frequency_status='EXCLUDED_FREQUENCY' if rate > maximum else 'WITHIN_FREQUENCY_LIMIT')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        pass
    return result


def score(payload, wallet):
    if (not isinstance(payload, dict) or payload.get('wallet', '').lower() != wallet
            or payload.get('chain_id') != 5042 or payload.get('source') != 'nansen'
            or payload.get('pages_complete') is not True):
        raise ValueError('invalid_or_incomplete_nansen_detail')
    views = payload.get('positions')
    if not isinstance(views, dict) or any(not isinstance(views.get(k), list) for k in ('realized', 'open')):
        raise ValueError('missing_position_views')
    indexed = {}
    for view in ('realized', 'open'):
        indexed[view] = {}
        for row in views[view]:
            if not isinstance(row, dict):
                raise ValueError('invalid_position')
            token = row.get('token_address')
            if not isinstance(token, str) or not ADDRESS.fullmatch(token):
                raise ValueError('invalid_token_address')
            token = token.lower()
            if token in indexed[view]:
                raise ValueError('duplicate_token')
            indexed[view][token] = row
    outcomes, omitted = [], []
    for token in sorted(indexed['realized'].keys() | indexed['open'].keys()):
        r, o = indexed['realized'].get(token), indexed['open'].get(token)
        row = r if r is not None else o
        cost = row.get('bought_usd')
        realized = row.get('pnl_usd_realised')
        latent = o.get('pnl_usd_unrealised') if o is not None else row.get('pnl_usd_unrealised')
        holding = (o if o is not None else row).get('holding_amount')
        price = (o if o is not None else row).get('token_price')
        reason = None
        if number(cost) and cost == 0:
            reason = 'zero_observed_buy_cost'
        elif not all(number(v) for v in (cost, realized, latent, holding)) or cost < 0 or holding < 0:
            reason = 'missing_or_invalid_position_numbers'
        elif holding > 0 and (not number(price) or price <= 0 or o is None):
            reason = 'unpriced_or_missing_open_position'
        elif r is not None and o is not None and any(
                not number(o.get(k)) or not math.isclose(r[k], o[k], rel_tol=1e-6, abs_tol=.01)
                for k in ('bought_usd', 'pnl_usd_realised')):
            reason = 'conflicting_views'
        if reason:
            omitted.append({'token': token, 'reason': reason})
            continue
        pnl = realized + latent
        multiple = 1 + pnl / cost
        if not math.isfinite(multiple) or not math.isfinite(pnl):
            omitted.append({'token': token, 'reason': 'non_finite_outcome'})
            continue
        outcomes.append({'token': token, 'multiple': multiple, 'pnl': pnl, 'bought_usd': cost})
    n = len(outcomes)
    wins = sum(r['pnl'] > 0 for r in outcomes)
    positives = [r['pnl'] for r in outcomes if r['pnl'] > 0]
    concentration = max(positives) / sum(positives) if positives else (0.0 if n else None)
    counts = {f'x{x}': sum(r['multiple'] >= x for r in outcomes) for x in (2, 5, 10)}
    lower = wilson(wins, n)
    # New research-only formula; intentionally unrelated to engine promotion thresholds.
    raw_score = (100 * (.5*lower + .2*wilson(counts['x2'], n)
                      + .2*wilson(counts['x5'], n) + .1*wilson(counts['x10'], n))
                 * (1 - .5*concentration)) if n else None
    # Same deduplicated token scope as outcomes, including latent losses.
    # Not ROI on account equity: reinvested capital can be counted on each buy.
    bought = sum(r['bought_usd'] for r in outcomes)
    total_pnl = sum(r['pnl'] for r in outcomes)
    complete_roi = (not omitted and n > 0 and number(bought) and bought > 0
                    and number(total_pnl) and number(100 * (total_pnl / bought)))
    roi = 100 * (total_pnl / bought) if complete_roi else None
    return dict(status='PARTIAL_POSITION_DATA' if omitted else ('AVAILABLE' if n else 'NO_EVALUABLE_POSITIONS'),
                historical_copy_score=raw_score if not omitted else None,
                sample_size=n, excluded_positions=omitted, **counts,
                wilson=lower, win_rate=wins/n if n else None, concentration=concentration,
                roi_pct=roi, capital_multiple=1+roi/100 if roi is not None else None,
                total_buy_usd=bought if complete_roi else None,
                pnl_informative=payload.get('metrics'), date=payload.get('date'),
                source_flags=payload.get('flags', []))


def report(db_path, shortlist_path, excluded=(), max_operations_per_day=50):
    if not number(max_operations_per_day) or max_operations_per_day <= 0:
        raise ValueError('invalid_max_operations_per_day')
    wallets = shortlist(shortlist_path)
    rows = []
    # mode=ro and query_only prohibit migrations, checkpoints and authority changes.
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for wallet in wallets:
            row = dict(wallet=wallet, status='MISSING_NANSEN_DETAIL', historical_copy_score=None,
                       detail_available=False, live_signal_authority=False, paper_selected=False)
            row.update(frequency({}, max_operations_per_day))
            stored = db.execute('''SELECT payload,chain_id,error_category,retrieved_at,snapshot_key
                FROM external_wallet_metrics WHERE wallet=? AND provider='nansen'
                ORDER BY retrieved_at DESC, observed_at DESC, snapshot_key DESC LIMIT 1''', (wallet,)).fetchone()
            if stored:
                row.update(retrieved_at=stored['retrieved_at'], snapshot_key=stored['snapshot_key'])
                try:
                    if stored['chain_id'] != 5042 or stored['error_category']:
                        raise ValueError('stored_detail_error_or_wrong_chain')
                    payload = json.loads(stored['payload'])
                    row.update(score(payload, wallet), detail_available=True)
                    row.update(frequency(payload, max_operations_per_day))
                except (ValueError, TypeError, KeyError, AttributeError):
                    row['status'] = 'INVALID_NANSEN_DETAIL'
            if wallet in excluded:
                row.update(status='EXCLUDED_INFRASTRUCTURE', historical_copy_score=None)
            row['copy_research_eligible'] = (row['historical_copy_score'] is not None
                                            and row['frequency_status'] == 'WITHIN_FREQUENCY_LIMIT')
            rows.append(row)
    rows.sort(key=lambda r: (not r['copy_research_eligible'],
                            r['frequency_status'] != 'WITHIN_FREQUENCY_LIMIT',
                            r['historical_copy_score'] is None,
                            -(r['historical_copy_score'] or 0), r['wallet']))
    return {'scope': 'SHORTLIST COPY RESEARCH ONLY', 'wallets_analyzed': len(rows),
            'wallets_with_detail': sum(r['detail_available'] for r in rows),
            'wallets_missing_stored_detail': sum(not r['detail_available'] for r in rows),
            'wallets_scored': sum(r['historical_copy_score'] is not None for r in rows),
            'wallets_excluded_frequency': sum(r['frequency_status'] == 'EXCLUDED_FREQUENCY' for r in rows),
            'wallets_unknown_frequency': sum(r['frequency_status'] == 'UNKNOWN_FREQUENCY' for r in rows),
            'copy_research_candidates': sum(r['copy_research_eligible'] for r in rows),
            'nansen_http_calls': 0, 'nansen_credits_consumed': 0, 'rows': rows}


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--db', default='data/arc_bot.sqlite3')
    parser.add_argument('--shortlist', default='data/copy_research_shortlist.json')
    parser.add_argument('--json', action='store_true', help='Print full shortlist with provenance')
    args = parser.parse_args(argv)
    if os.getenv('COPY_SHORTLIST_ONLY', 'true').lower() != 'true':
        parser.error('COPY_SHORTLIST_ONLY must be true; broad scoring is not supported')
    if args.top <= 0:
        parser.error('--top must be positive')
    from wallet_policy import infrastructure_addresses
    try:
        maximum = float(os.getenv('COPY_MAX_OPERATIONS_PER_DAY', '50'))
        if not number(maximum) or maximum <= 0:
            raise ValueError()
    except ValueError:
        parser.error('COPY_MAX_OPERATIONS_PER_DAY must be finite and positive')
    result = report(args.db, args.shortlist, infrastructure_addresses(), maximum)
    if args.json:
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
        return
    print(result['scope'])
    print(f"{result['wallets_analyzed']} wallets analyses | {result['wallets_with_detail']} avec details Nansen | "
          f"{result['wallets_missing_stored_detail']} sans details exploitables")
    print(f"Plafond: {maximum:g} operations/jour en moyenne | {result['wallets_excluded_frequency']} exclus pour frequence | "
          f"{result['wallets_unknown_frequency']} frequence inconnue | {result['copy_research_candidates']} candidats audit copy")
    print('wallet                                      score   x2/x5/x10  ROI%  multiple  Wilson  win%   n  concentration  PnL USD(info) status')
    for r in result['rows'][:args.top]:
        fmt = lambda k, pct=False: '-' if r.get(k) is None else f"{r[k]*(100 if pct else 1):.2f}"
        counts = '/'.join(str(r.get(f'x{x}', '-')) for x in (2, 5, 10))
        pnl = (r.get('pnl_informative') or {}).get('total_gain')
        pnl_text = f'{pnl:.2f}' if number(pnl) else '-'
        print(f"{r['wallet']}  {fmt('historical_copy_score'):>6}  {counts:>10}  {fmt('roi_pct'):>8}  {fmt('capital_multiple'):>8}x  {fmt('wilson', True):>6}  "
              f"{fmt('win_rate', True):>6} {r.get('sample_size', '-'):>3}  {fmt('concentration', True):>6}%  {pnl_text:>13} {r['status']}")
        print(f"  Operations: {r['operations']} | jours: {fmt('observation_days')} | op/j: {fmt('operations_per_day')} | {r['frequency_status']}")
        if r.get('excluded_positions'):
            reasons = sorted({p['reason'] for p in r['excluded_positions']})
            print(f"  {len(r['excluded_positions'])} positions non evaluables: {', '.join(reasons)}")
    print('Positions par token; PnL latent inclus si exploitable. Wilson descriptif, pas une probabilite de copie rentable.')
    print('Nansen HTTP calls during scoring: 0\nNansen credits consumed: 0')
    print(f"Wallets scored: {result['wallets_scored']}\nWallets missing stored detail: {result['wallets_missing_stored_detail']}")


if __name__ == '__main__':
    main()
