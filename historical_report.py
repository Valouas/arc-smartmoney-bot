"""Versioned historical ranking and expiring paper-selection contract."""
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from atomic_json import write_json
from historical_metrics import analyze, qualify
from wallet_policy import infrastructure_addresses
from wallet_ranking import export_csv


def build_report(path, cfg, now=None):
    now = int(time.time()) if now is None else now
    excluded = infrastructure_addresses(cfg)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')  # One consistent read snapshot, no engine mutations.
        candidates = {r['wallet'].lower(): dict(r) for r in db.execute('SELECT * FROM discovery_candidates')}
        wallets = set(candidates) | {r[0].lower() for r in db.execute('SELECT wallet FROM wallet_profiles')}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        runs = {r['wallet']: dict(r) for r in db.execute('SELECT * FROM historical_runs')} if 'historical_runs' in tables else {}
        rows = []
        for wallet in sorted(wallets):
            run = runs.get(wallet, {})
            transactions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND payload IS NOT NULL', (wallet,))] if run else []
            metrics = analyze(transactions, cfg)
            identity = json.loads(run.get('identity', '{}'))
            category, reasons = qualify(metrics, run, identity, wallet in excluded, cfg, now)
            if wallet not in candidates:
                category = 'EXCLUDED' if wallet in excluded else 'WATCH'
                reasons.append('no_discovery_candidate')
            rows.append({'wallet': wallet, 'historical_class': category,
                         'discovery_status': candidates.get(wallet, {}).get('status', 'PROFILE_ONLY'),
                         'first_discovered_at': candidates.get(wallet, {}).get('first_discovered_at'),
                         'winner_sources': candidates.get(wallet, {}).get('source_winners'),
                         'snapshot_phase': run.get('phase', 'NOT_STARTED'),
                         'snapshot_at': run.get('cutoff_at'), 'snapshot_block': run.get('cutoff_block'),
                         'snapshot_age_hours': (now-run['cutoff_at'])/3600 if run.get('cutoff_at') else None,
                         'completed_at': run.get('completed_at'), 'collection_error': run.get('error'),
                         'historical_identity_cluster': identity.get('cluster'),
                         'identity_members': identity.get('members', []),
                         'identity_eligible': bool(identity.get('eligible')),
                         'independent_profitable_deployers': identity.get('independent_profitable_deployers', 0),
                         'funder': identity.get('funder'), 'shared_service': identity.get('shared_service'),
                         'infrastructure_excluded': wallet in excluded,
                         'paper_selection_eligible': category == 'HISTORICALLY_QUALIFIED',
                         'live_signal_authority': False, 'blocking_reasons': reasons,
                         'expires_at': min(now+cfg.historical_refresh_seconds, run.get('cutoff_at', 0)+cfg.historical_max_age_hours*3600),
                         **metrics})
    # Merge persisted components rather than treating different representatives
    # of the same funding component as independent identities.
    parent = {}
    def root(w):
        parent.setdefault(w, w)
        if parent[w] != w: parent[w] = root(parent[w])
        return parent[w]
    for r in rows:
        for member in r['identity_members']:
            a, b = root(r['wallet']), root(member)
            parent[max(a,b)] = min(a,b)
    for r in rows:
        r['identity_cluster_id'] = root(r['wallet'])
    counts = Counter(r['identity_cluster_id'] for r in rows)
    for r in rows:
        r['identity_cluster_size'] = counts[r['identity_cluster_id']]
        r['related_wallets_count'] = counts[r['identity_cluster_id']]-1
    order = {'HISTORICALLY_QUALIFIED': 0, 'PROMISING': 1, 'WATCH': 2, 'EXCLUDED': 3}
    phases = {'COMPLETE': 0, 'DECODING': 1, 'FETCHING': 2, 'NOT_STARTED': 3}
    rows.sort(key=lambda r: (order[r['historical_class']], phases.get(r['snapshot_phase'], 3), -r['profitable_periods'],
                            -r['conservative_pnl_usdc'], -r['closed_positions'], r['wallet']))
    return rows


def publish(rows, cfg, output_dir='data', top=20, diverse=False, now=None):
    now = int(time.time()) if now is None else now
    path = Path(output_dir)
    export_csv(rows, path/'historical_wallet_ranking.csv')
    summary = {'total': len(rows), **dict(Counter(r['historical_class'] for r in rows)),
               'completed_snapshots': sum(r['snapshot_phase'] == 'COMPLETE' for r in rows),
               'independent_qualified_clusters': len({r['identity_cluster_id'] for r in rows if r['paper_selection_eligible']})}
    selected, clusters = [], set()
    for r in rows:
        if (r['paper_selection_eligible'] and r['expires_at'] > now
                and r['identity_cluster_id'] not in clusters):
            selected.append(r); clusters.add(r['identity_cluster_id'])
    contract = {'schema_version': 1, 'chain_id': cfg.chain_id, 'generated_at': now,
                'purpose': 'historical_selection_for_paper', 'live_signal_authority': False,
                'requires_consumer_validation': True, 'one_wallet_per_identity_cluster': True,
                'wallets': selected}
    write_json(path/'historical_selection.json', contract)
    write_json(path/'historical_wallet_ranking.json', {'generated_at': now, 'summary': summary, 'wallets': rows})
    print('HISTORIQUE | FIFO, gas observe, positions ouvertes non valorisees | aucune autorite live')
    print('Wallet | Classe historique | Buys | Positions fermees | PnL realise brut | PnL prudent | Etat')
    shown, seen = 0, set()
    for r in rows:
        if diverse and r['identity_cluster_id'] in seen: continue
        if top is not None and shown >= top: break
        seen.add(r['identity_cluster_id']); shown += 1
        print(f"{r['wallet']} | {r['historical_class']} | {r['buys']} | {r['closed_positions']} | "
              f"{r['gross_realized_pnl_usdc']:.2f} | {r['conservative_pnl_usdc']:.2f} | {r['snapshot_phase']}")
        print('  '+(', '.join(r['blocking_reasons']) or 'qualifie historiquement; selection paper seulement'))
    print('Resume: '+json.dumps(summary))
    print(f'Selection paper: {len(selected)} wallets | {path / "historical_selection.json"}')
    return contract
