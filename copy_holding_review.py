"""Read-only holding-time verification for an explicit, at-most-25 wallet list."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3

from copy_candidate_report import ADDRESS
from copy_holding_duration import stored_holding


def review(database, wallets):
    if (not isinstance(wallets,list) or not 1<=len(wallets)<=25 or
        any(not isinstance(w,str) or not ADDRESS.fullmatch(w) for w in wallets)):
        raise ValueError('invalid_review_wallets')
    wallets=[w.lower() for w in wallets]
    if len(set(wallets))!=len(wallets):raise ValueError('duplicate_review_wallets')
    rows=[]
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for wallet in wallets:
            proof=stored_holding(db,wallet)
            q=db.execute('SELECT state,reason FROM campaign_queue WHERE wallet=?',(wallet,)).fetchone()
            run=db.execute('SELECT phase,error FROM historical_runs WHERE wallet=?',(wallet,)).fetchone()
            counts=db.execute('SELECT COUNT(*),COUNT(payload) FROM historical_transactions WHERE wallet=?',(wallet,)).fetchone()
            issues=Counter()
            for payload, in db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND payload IS NOT NULL',(wallet,)):
                try:
                    tx=json.loads(payload)
                    if not isinstance(tx,dict):raise ValueError('malformed')
                    if tx.get('error'):issues['unsupported_or_ambiguous']+=1
                    if tx.get('inventory_gap'):issues['inventory_gap']+=1
                except (ValueError,TypeError):issues['malformed_payload']+=1
            rows.append(dict(wallet=wallet,**proof,state=q[0] if q else 'OUTSIDE_CAMPAIGN',
                queue_reason=q[1] if q else None,phase=run[0] if run else 'ABSENT',
                transactions=counts[0],decoded=counts[1],missing=counts[0]-counts[1],
                reconstruction_issues=dict(issues),live_signal_authority=False,paper_selected=False))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(),wallets=len(rows),
        verified_duration=sum(r['holding_duration_eligible'] for r in rows),
        too_short=sum(r['holding_duration_status']=='EXCLUDED_SHORT_HOLDING' for r in rows),
        unknown=sum(r['holding_duration_status']=='UNKNOWN_HOLDING_DURATION' for r in rows),
        remaining_transactions=sum(r['missing'] for r in rows),
        wallets_with_reconstruction_issues=sum(bool(r['reconstruction_issues']) for r in rows),
        network_calls=0,rows=rows)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign',required=True);p.add_argument('--wallets',required=True)
    p.add_argument('--output-dir');args=p.parse_args(argv)
    spec=json.loads(Path(args.wallets).read_text(encoding='utf-8'))
    result=review(Path(args.campaign)/'campaign.sqlite3',spec['wallets'])
    if args.output_dir:
        out=Path(args.output_dir);out.mkdir(parents=True,exist_ok=False)
        (out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        esc=lambda v:html.escape(str(v))
        def val(v):return 'Non établi' if v is None else f'{v:.2f}'
        rows=[]
        for r in result['rows']:
            cells=[r['wallet'],r['holding_duration_status'],str(r['decoded'])+'/'+str(r['transactions']),
                val(r['median_first_sale_seconds']),val(r['holding_fast_exit_pct']),
                r['holding_sample_size'],r['holding_unsold_buy_count'],r['holding_duration_reason'],
                json.dumps(r['reconstruction_issues']),r['state']]
            rows.append('<tr>'+''.join('<td>'+esc(c)+'</td>' for c in cells)+'</tr>')
        headers=['Wallet','Résultat','Transactions décodées','Médiane (s)','Sorties <120 s (%)',
                 'Achats avec première vente','Achats sans vente','Motif','Ambiguïtés détectées','Collecte']
        page='''<!doctype html><html lang="fr"><meta charset="utf-8"><title>Vérification des durées — 25 wallets</title>
<style>body{font:15px system-ui;margin:25px;color:#192c46;background:#f3f6fb}section{overflow:auto}table{border-collapse:collapse;white-space:nowrap}td,th{padding:10px;border:1px solid #ccd6e4}th{background:#233e60;color:white}p{max-width:1000px}</style>
<h1>Vérification des durées de détention</h1>'''
        page+=f'<p>État au {esc(result["generated_at"])} : {result["verified_duration"]} médianes conformes, {result["too_short"]} trop courtes, {result["unknown"]} non établies.</p>'
        page+='<p>Une donnée manquante ne signifie jamais zéro sortie rapide. Le pourcentage porte sur les lots d’achat avec première vente observée ; les achats sans vente sont exclus du dénominateur. Le seuil est strictement inférieur à 120 secondes. Les historiques incomplets ou ambigus ne publient aucune statistique partielle. Aucune autorité paper/live.</p><section><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></section></html>'
        (out/'verification.html').write_text(page,encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))


if __name__=='__main__':main()
