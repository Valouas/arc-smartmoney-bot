"""Conservative identity grouping for display only, never engine authority."""
from collections import Counter, defaultdict

from capital_allocation import SizingPolicy, allocation_metrics, confidence


def attach_clusters(rows):
    parent = {r['wallet']: r['wallet'] for r in rows}

    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a, b):
        a, b = find(a), find(b)
        parent[max(a,b)] = min(a,b)

    labels, evidence = {}, {}
    for r in rows:
        w = r['wallet']
        runtime, historical = r.get('last_runtime_identity_cluster'), r.get('historical_identity_cluster')
        r['runtime_identity_cluster'] = runtime
        evidence[w] = bool(r.get('runtime_identity_known') or
                           (runtime and runtime.lower() != w) or
                           (historical and historical.lower() != w))
        # Join both snapshots conservatively: contradictory past/current labels
        # must not split a known relationship into extra apparent confirmations.
        for label in (runtime, historical):
            if not isinstance(label, str) or not label:
                continue
            label = label.lower()
            if label in parent:
                union(w, label)
            if label in labels:
                union(w, labels[label])
            labels[label] = w
    groups = defaultdict(list)
    for r in rows:
        groups[find(r['wallet'])].append(r)
    for root, members in groups.items():
        known = any(evidence[r['wallet']] for r in members) or len(members) > 1
        cluster_id = ('identity:' if known else 'unknown:')+root
        for r in members:
            r.update(identity_cluster_id=cluster_id, identity_cluster_size=len(members),
                     related_wallets_count=len(members)-1, identity_evidence_known=known,
                     independent_cluster_count=1 if known else 0,
                     same_cluster_wallet_count_in_top=0)
            if len(members) > 1:
                old = r.get('relation_funder_flags', 'UNKNOWN')
                r['relation_funder_flags'] = (old+';' if old != 'UNKNOWN' else '')+'linked_identity_snapshot'
    return rows


def select_view(rows, top=None, diverse=False, per_cluster=2):
    if per_cluster < 1:
        raise ValueError('Display cluster cap must be positive')
    selected, counts = [], Counter()
    for row in rows:
        if top is not None and len(selected) >= top:
            break
        cluster = row['identity_cluster_id']
        if diverse and counts[cluster] >= per_cluster:
            continue
        selected.append(row)
        counts[cluster] += 1
    # Pure output copies: neither selection nor top metadata edits the input.
    full = [dict(r, same_cluster_wallet_count_in_top=counts[r['identity_cluster_id']]) for r in rows]
    wallets = {r['wallet'] for r in selected}
    return full, [r for r in full if r['wallet'] in wallets]


def cluster_ranking(rows, policy=SizingPolicy(), winner_return_pct=100):
    groups = defaultdict(list)
    for row in rows:
        groups[row['identity_cluster_id']].append(row)
    result = []
    for cluster_id, members in groups.items():
        trades = [t for r in members for t in r['capital_share_per_trade']]
        metrics = allocation_metrics(trades, policy, winner_return_pct)
        total = metrics['capital_total_observed']
        known = all(r['identity_evidence_known'] for r in members)
        active = sum(r['sizing_forward_buys'] > 0 for r in members)
        returns = [r['capital_weighted_return'] for r in members if r['capital_weighted_return'] is not None]
        result.append(dict(cluster_id=cluster_id, wallet_count=len(members),
            observed_wallet_count=active, independent_cluster_count=int(known),
            winner_sources_distinct=len({t for r in members for t in r['winner_source_tokens']}),
            winner_sources_missing_detail_wallets=sum((r['winner_sources'] or 0) > len(r['winner_source_tokens']) for r in members),
            forward_buys=len(trades), forward_winners=metrics['capital_forward_winners'],
            # Separate count avoids presenting copies of one winning token as independent bets.
            forward_winner_tokens_distinct=len({t['token'] for t in trades if t['return_ratio'] is not None and t['return_ratio'] >= winner_return_pct/100}),
            capital_total_observed=total, capital_evaluated=metrics['capital_evaluated'],
            outcome_capital_coverage=metrics['outcome_capital_coverage'],
            capital_weighted_return=metrics['capital_weighted_return'],
            best_wallet_return=max(returns) if returns else None,
            internal_capital_concentration=max((r['capital_total_observed'] for r in members), default=0)/total if total else None,
            concentration_risk=metrics['concentration_risk'],
            largest_trade_contribution=metrics['largest_trade_contribution'],
            relation_funder_flags=';'.join(sorted({r['relation_funder_flags'] for r in members})),
            historical_funders=sorted({r['historical_funder'] for r in members if r.get('historical_funder')}),
            cluster_confidence=confidence(len({t['token'] for t in trades if t['return_ratio'] is not None}), policy)
                if known and metrics['concentration_risk'] != 'HIGH' and metrics['outcome_capital_coverage'] == 1
                    and all(r['data_status'] == 'COMPLETE' for r in members) else 'LOW',
            identity_evidence_known=known))
    return sorted(result, key=lambda r: (-(r['capital_weighted_return'] or 0), -r['forward_buys'], r['cluster_id']))


def cluster_summary(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['identity_cluster_id']].append(row)
    return dict(total_identity_clusters=len(groups),
        known_identity_clusters=sum(any(r['identity_evidence_known'] for r in group) for group in groups.values()),
        unknown_identity_wallets=sum(not r['identity_evidence_known'] for r in rows),
        largest_cluster=max(map(len, groups.values()), default=0),
        single_wallet_clusters=sum(len(g) == 1 for g in groups.values()),
        multi_wallet_clusters=sum(len(g) >= 2 for g in groups.values()),
        clusters_with_forward_buy=sum(any(r['sizing_forward_buys'] > 0 for r in g) for g in groups.values()),
        clusters_with_multiple_active_wallets=sum(sum(r['sizing_forward_buys'] > 0 for r in g) > 1 for g in groups.values()))
