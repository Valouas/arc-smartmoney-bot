"""Offline research scenarios for incomplete Nansen positions. Never engine authority."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import sqlite3
import statistics

from copy_candidate_report import ADDRESS, number, report, wilson


def zero_value_position(realized, opened):
    """Validate cash and quantity accounting before assuming remaining inventory = 0.

    sold - realized_pnl is the provider's implied sold cost. Remaining cost is
    bought - sold_cost. Do not infer cost from transfers, a zero-cost acquisition,
    a price from another date, or disagreeing realized/open views.
    """
    row = realized if realized is not None else opened
    if realized is not None and opened is not None:
        for key in ('bought_usd', 'sold_usd', 'pnl_usd_realised',
                    'bought_amount', 'sold_amount', 'holding_amount'):
            if (not number(realized.get(key)) or not number(opened.get(key))
                    or not math.isclose(realized[key], opened[key], rel_tol=1e-6, abs_tol=1e-8)):
                raise ValueError('conflicting_or_missing_accounting_views')
    keys = ('bought_usd', 'sold_usd', 'pnl_usd_realised',
            'bought_amount', 'sold_amount', 'holding_amount')
    if any(not number(row.get(k)) for k in keys):
        raise ValueError('missing_accounting_numbers')
    buy, sold, pnl, bought_qty, sold_qty, holding = (row[k] for k in keys)
    if buy <= 0 or bought_qty <= 0 or sold < 0 or sold_qty < 0 or holding <= 0:
        raise ValueError('invalid_cash_or_quantities')
    # Evidence of transfers/rebases/incomplete inventory prevents a safe cost inference.
    if not math.isclose(bought_qty, sold_qty + holding, rel_tol=1e-6, abs_tol=1e-10):
        raise ValueError('unreconciled_inventory')
    sold_cost = sold - pnl
    remaining = buy - sold_cost
    if not all(number(x) for x in (sold_cost, remaining)) or sold_cost < 0 or remaining < 0:
        raise ValueError('inconsistent_remaining_cost')
    if sold_qty == 0 and (sold != 0 or pnl != 0):
        raise ValueError('sales_without_sold_quantity')
    scenario_pnl = pnl - remaining
    if not number(scenario_pnl):
        raise ValueError('non_finite_scenario')
    return dict(bought_usd=buy, realized_pnl_usd=pnl, sold_usd=sold,
                implied_sold_cost_usd=sold_cost, remaining_cost_usd=remaining,
                assumed_remaining_value_usd=0.0, pnl=scenario_pnl)


def outcome_metrics(outcomes):
    """Position-level descriptive metrics, never trade-size or live-copy metrics."""
    if not outcomes:
        return None
    cost = sum(r['bought_usd'] for r in outcomes)
    pnl = sum(r['pnl'] for r in outcomes)
    if not number(cost) or cost <= 0 or not number(pnl):
        return None
    multiples = [1 + r['pnl'] / r['bought_usd'] for r in outcomes]
    if not all(number(v) for v in multiples) or not number(100 * pnl / cost):
        return None
    positives = [r['pnl'] for r in outcomes if r['pnl'] > 0]
    gains, losses = sum(positives), -sum(r['pnl'] for r in outcomes if r['pnl'] < 0)
    if not number(gains) or not number(losses):
        return None
    concentration = max(positives) / gains if gains else 0.0
    wins, n = len(positives), len(outcomes)
    counts = {f'x{x}':sum(v >= x for v in multiples) for x in (2, 5, 10)}
    raw = 100 * (.5 * wilson(wins, n) + .2 * wilson(counts['x2'], n)
                 + .2 * wilson(counts['x5'], n) + .1 * wilson(counts['x10'], n)) * (1-.5*concentration)
    factor = gains/losses if losses else None
    if factor is not None and not number(factor):
        factor = None
    return dict(roi_pct=100*pnl/cost, pnl_usd=pnl, buy_usd=cost,
                position_count=n, win_rate=wins/n, **counts,
                historical_scenario_score=raw, wilson=wilson(wins,n),
                concentration=concentration, position_profit_factor=factor,
                capital_weighted_hit_rate=sum(r['bought_usd'] for r in outcomes if r['pnl']>0)/cost,
                capital_weighted_return=pnl/cost,
                median_position_buy_usd=statistics.median(r['bought_usd'] for r in outcomes))


def analyze(payload, strict):
    """Separate hypothetical scenario; strict row and source payload remain untouched."""
    omitted = {p['token']:p['reason'] for p in strict.get('excluded_positions', [])}
    indexed = {v:{r['token_address'].lower():r for r in payload['positions'][v]}
               for v in ('realized','open')}
    tokens = sorted(indexed['realized'].keys() | indexed['open'].keys())
    known, assumed, unresolved, diagnostics = [], [], [], []
    cost_total = 0.0
    cost_known = True
    for token in tokens:
        r, o = indexed['realized'].get(token), indexed['open'].get(token)
        row = r if r is not None else o
        buy = row.get('bought_usd')
        cost_valid = number(buy) and buy > 0
        if cost_valid:
            cost_total += buy
        else:
            cost_known = False
        entry = dict(token=token, source_reason=omitted.get(token), bought_usd=buy if number(buy) else None)
        if token not in omitted:
            latent = (o if o is not None else row)['pnl_usd_unrealised']
            outcome=dict(token=token,bought_usd=buy,pnl=row['pnl_usd_realised']+latent)
            known.append(outcome)
            entry.update(status='SOURCE_EVALUABLE',pnl=outcome['pnl'])
        elif omitted[token]=='unpriced_or_missing_open_position':
            try:
                evidence=zero_value_position(r,o)
                assumed.append(dict(token=token,**evidence))
                entry.update(status='ZERO_VALUE_SCENARIO',**evidence)
            except ValueError as error:
                entry.update(status='UNRESOLVED',reason=str(error))
                unresolved.append(entry)
        else:
            entry.update(status='UNRESOLVED',reason=omitted[token])
            if omitted[token]=='zero_observed_buy_cost':
                entry['review_action']='identify_transfer_airdrop_or_missing_cost; never infer infinite ROI'
            unresolved.append(entry)
        diagnostics.append(entry)
    known_capital=sum(r['bought_usd'] for r in known)
    usable_denominator=cost_known and number(cost_total) and cost_total>0
    scenario=outcome_metrics(known+assumed) if assumed and not unresolved else None
    if assumed and not unresolved and scenario is None:
        unresolved.append(dict(token=None,reason='invalid_aggregate_scenario'))
    data_status=('PRUDENT_SCENARIO' if scenario else 'UNRESOLVED' if omitted else 'STRICT_ONLY')
    qualifies=(scenario is not None and scenario['roi_pct']>=strict['min_roi_pct']
               and strict['frequency_status']=='WITHIN_FREQUENCY_LIMIT')
    return dict(recovery_status=data_status, omitted_position_count=len(omitted),
                audit_priority='ONE_POSITION' if len(omitted)==1 else 'FEW_POSITIONS' if 1<len(omitted)<=3 else 'OTHER',
                known_position_count=len(known), zero_value_position_count=len(assumed),
                unresolved_position_count=len(unresolved),
                known_buy_capital_usd=known_capital,
                known_buy_capital_coverage=known_capital/cost_total if usable_denominator else None,
                capital_coverage_status='KNOWN_POSITIVE_COSTS' if usable_denominator else 'UNKNOWN_OR_ZERO_COST',
                # No replacement of strict historical_copy_score, ROI, or eligibility.
                prudent_scenario=scenario, performance_recovery_candidate=qualifies,
                research_recovery_candidate=qualifies and strict.get('holding_duration_eligible', False),
                unresolved_positions=unresolved, positions=diagnostics,
                copyability_checks=dict(current_funds='NOT_CHECKED',liquidity='NOT_CHECKED',
                    executable_slippage='NOT_CHECKED',delay_sensitivity='NOT_CHECKED',
                    trade_timing_and_holding_duration=strict.get('holding_duration_status', 'UNKNOWN_HOLDING_DURATION')),
                live_signal_authority=False,paper_selected=False)


def recovery_report(db_path, shortlist_path, excluded=(), maximum=50, minimum_roi=20, minimum_holding=120):
    strict=report(db_path,shortlist_path,excluded,maximum,minimum_roi,minimum_holding)
    rows=[]
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for original in strict['rows']:
            row=dict(original)
            row.update(recovery_status='NOT_ANALYZABLE',research_recovery_candidate=False,performance_recovery_candidate=False,
                       live_signal_authority=False,paper_selected=False)
            if original['status'] in ('AVAILABLE','PARTIAL_POSITION_DATA','NO_EVALUABLE_POSITIONS'):
                p=db.execute("SELECT payload FROM external_wallet_metrics WHERE wallet=? AND provider='nansen' AND snapshot_key=?",
                             (row['wallet'],row['snapshot_key'])).fetchone()
                if p is not None:
                    row.update(analyze(json.loads(p[0]),original))
            if 'copyability_checks' in row:
                row['copyability_checks']['trade_timing_and_holding_duration'] = row['holding_duration_status']
            rows.append(row)
    priority={'ONE_POSITION':0,'FEW_POSITIONS':1,'OTHER':2}
    rows.sort(key=lambda r:(not r['research_recovery_candidate'],priority.get(r.get('audit_priority'),3),
                            -(r.get('prudent_scenario') or {}).get('historical_scenario_score',-1),r['wallet']))
    partial=[r for r in rows if r['status']=='PARTIAL_POSITION_DATA']
    summary=dict(wallets_analyzed=len(rows),strict_scored=strict['wallets_scored'],partial_wallets=len(partial),
                 single_position_wallets=sum(r.get('omitted_position_count')==1 for r in partial),
                 prudent_scenarios=sum(r['recovery_status']=='PRUDENT_SCENARIO' for r in rows),
                 recovered_research_candidates=sum(r['research_recovery_candidate'] for r in rows),
                 wallets_excluded_short_holding=strict['wallets_excluded_short_holding'],
                 wallets_unknown_holding=strict['wallets_unknown_holding'],
                 single_position_candidates=sum(r['research_recovery_candidate'] and r.get('omitted_position_count')==1 for r in rows),
                 unresolved_wallets=sum(r['recovery_status']=='UNRESOLVED' for r in rows),
                 blockers=dict(Counter(p['reason'] for r in rows for p in r.get('unresolved_positions',[]))))
    # Shared token repair queue is diagnostic only, dated to the original snapshot window.
    queue={}
    for row in partial:
        for p in row.get('positions',[]):
            if p['status']=='SOURCE_EVALUABLE':
                continue
            key=(p['token'],json.dumps(row.get('date'),sort_keys=True))
            if key not in queue:
                queue[key]=dict(token=p['token'],date=row.get('date'),wallets=[],reasons=set())
            queue[key]['wallets'].append(row['wallet'])
            queue[key]['reasons'].add(p.get('reason') or p['source_reason'])
    repair=[]
    for v in queue.values():
        v['reasons']=sorted(v['reasons']);v['wallet_count']=len(v['wallets']);repair.append(v)
    repair.sort(key=lambda r:(-r['wallet_count'],r['token']))
    return dict(scope='OFFLINE INCOMPLETE-WALLET RESEARCH; NO AUTHORITY',
                generated_at_utc=datetime.now(timezone.utc).isoformat(),summary=summary,
                nansen_http_calls=0,nansen_credits_consumed=0,rows=rows,token_review_queue=repair)


def render_html(result):
    esc=lambda x:html.escape(str(x))
    fmt=lambda v:'N/D' if v is None else f'{v:,.2f}'
    rows=[]
    for r in result['rows']:
        if r['status']!='PARTIAL_POSITION_DATA':
            continue
        p=r.get('prudent_scenario') or {}
        fields=[r['wallet'],r.get('audit_priority'),r['recovery_status'],
                'À auditer' if r['research_recovery_candidate'] else 'Critères non établis',
                fmt(p.get('roi_pct')),fmt(p.get('historical_scenario_score')),
                r.get('omitted_position_count'),r.get('zero_value_position_count'),
                fmt(100*r['known_buy_capital_coverage']) if r.get('known_buy_capital_coverage') is not None else 'N/D',
                fmt(r['operations_per_day']),fmt(100*p['capital_weighted_hit_rate']) if p else 'N/D',
                fmt(100*p['concentration']) if p else 'N/D',
                ', '.join(sorted({x['reason'] for x in r.get('unresolved_positions',[])})),
                fmt(r.get('median_first_sale_seconds')), r['holding_duration_status'],r['holding_sample_size']]
        rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in fields)+'</tr>')
    headers=['Wallet','Priorité','État','Recherche','ROI scénario %','Score scénario /100',
             'Positions bloquantes','Valeurs supposées nulles','Capital achats exploitable %',
             'Opérations/jour','Réussite pondérée capital %','Concentration %','Blocages restants',
             'Détention médiane (s)','Filtre détention','Achats avec première vente']
    s=result['summary']
    return '''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Scénarios prudents des wallets incomplets</title>
<style>body{font:15px system-ui;margin:30px;color:#17324d;background:#f5f8fb}section{background:white;padding:22px;margin:20px 0;border-radius:10px}.scroll{overflow:auto;max-height:700px}table{border-collapse:collapse;white-space:nowrap;font-size:13px}th,td{padding:10px;border-bottom:1px solid #dce4ec}th{background:#203a5b;color:white;position:sticky;top:0}tr:nth-child(even){background:#edf4f8}input{padding:12px;width:420px}p{max-width:1050px}</style>
<h1>Scénarios prudents des dossiers incomplets</h1><section>'''+f'<b>{s["partial_wallets"]} dossiers partiels ; {s["prudent_scenarios"]} scénarios calculables ; {s["recovered_research_candidates"]} candidats à auditer, dont {s["single_position_candidates"]} bloqués par une seule position.</b>'+'''
<p>Les scores stricts, seuils moteur et statuts paper/live restent inchangés. Aucun wallet n’est promu par ce rapport. Fonds actuels, liquidité et exécution de copie non vérifiés.</p>
<p>Hypothèse : valeur restante nulle pour les positions sans prix, uniquement après rapprochement des coûts et quantités. ROI scénario = PnL du scénario / achats cumulés. Ce n’est pas un rendement vérifié ni une garantie de gain. Le score scénario n’est pas une borne mathématique du score futur.</p>
<p>Les pertes ouvertes restent incluses. Les coûts inconnus/zéro, transferts non rapprochés et contradictions ne sont pas effacés. La couverture mesure les achats connus, pas la qualité de l’ensemble du portefeuille. Les métriques pondérées sont par position token, pas par trade.</p>
<p>Priorité aux dossiers avec une seule position bloquante, puis 2 à 3. Pas de filtre d’activité minimale. Les prix actuels ne remplacent pas les prix historiques.</p>
<p>0 appel réseau Nansen ; 0 crédit Nansen. Résultats fondés sur les snapshots identifiés dans le JSON.</p></section>
<section><input id="q" placeholder="Adresse, état ou motif"><div class="scroll"><table><thead><tr>'''+''.join('<th>'+h+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(rows)+'''</tbody></table></div></section>
<script>document.getElementById('q').addEventListener('input',e=>{for(const r of document.querySelectorAll('tbody tr'))r.hidden=!r.textContent.toLowerCase().includes(e.target.value.toLowerCase())})</script></html>'''


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default='data/arc_bot.sqlite3')
    p.add_argument('--shortlist',default='data/copy_research_shortlist.json')
    p.add_argument('--registry',default='trusted_dex.json')
    p.add_argument('--max-operations-per-day',type=float,default=50)
    p.add_argument('--min-roi-pct',type=float,default=20)
    p.add_argument('--min-holding-seconds',type=float,default=120)
    p.add_argument('--output-dir',required=True,help='New directory, never overwrite existing evidence')
    args=p.parse_args(argv)
    registry=json.loads(Path(args.registry).read_text(encoding='utf-8-sig'))
    if (registry.get('chain_id')!=5042 or not isinstance(registry.get('emitters'),list)
            or any(not isinstance(r,dict) or not isinstance(r.get('address'),str)
                   or not ADDRESS.fullmatch(r['address']) for r in registry['emitters'])):
        p.error('Invalid infrastructure registry')
    directory=Path(args.output_dir)
    if directory.exists():
        p.error('Output exists; choose a new directory to preserve evidence')
    result=recovery_report(args.db,args.shortlist,{r['address'].lower() for r in registry['emitters']},
                           args.max_operations_per_day,args.min_roi_pct,args.min_holding_seconds)
    result['source_shortlist_sha256']=hashlib.sha256(Path(args.shortlist).read_bytes()).hexdigest()
    result['implementation_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result['strict_scoring_sha256']=hashlib.sha256(Path(report.__code__.co_filename).read_bytes()).hexdigest()
    result['infrastructure_registry_sha256']=hashlib.sha256(Path(args.registry).read_bytes()).hexdigest()
    directory.mkdir(parents=True,exist_ok=False)
    for name,content in [('report.json',json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2)),
                         ('report.html',render_html(result))]:
        with (directory/name).open('x',encoding='utf-8') as stream:stream.write(content)
    print(json.dumps(result['summary'],indent=2))
    print('Nansen HTTP calls during scoring: 0\nNansen credits consumed: 0')


if __name__=='__main__':
    main()
