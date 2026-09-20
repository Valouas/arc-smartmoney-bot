"""Combined research report. EXTERNAL DATA != LIVE SIGNAL AUTHORITY."""
import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from atomic_json import write_json
from config import SETTINGS
from historical_report import build_report
from paper_copy.selection import load_selection, SelectionError
from wallet_policy import infrastructure_addresses
from wallet_ranking import export_csv

BANNER = 'EXTERNAL DATA != LIVE SIGNAL AUTHORITY'


def combined_report(path, cfg, now=None, selection_path=None, paper_path=None):
    now = int(time.time()) if now is None else now
    path = Path(path).resolve()
    local = {r['wallet']:r for r in build_report(path,cfg,now)
             if r.get('first_discovered_at') is None or r['first_discovered_at'] <= now}
    excluded = infrastructure_addresses(cfg)
    try:
        selected = load_selection(selection_path or path.parent/'paper_review_selection.json',cfg,now)
    except SelectionError: selected = {}
    dune, zerion, attempts, observed, nansen = {}, {}, {}, {}, {}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'external_wallet_candidates' in tables:
            for row in db.execute('SELECT * FROM external_wallet_candidates WHERE observed_at<=? AND retrieved_at<=?',(now,now)):
                if row['provider'] == 'dune': dune[row['wallet']] = dict(row)
                observed[row['wallet']] = min(observed.get(row['wallet'],row['observed_at']),row['observed_at'])
        if 'external_wallet_metrics' in tables:
            for row in db.execute("SELECT * FROM external_wallet_metrics WHERE provider='zerion' AND payload IS NOT NULL AND observed_at<=? AND retrieved_at<=? ORDER BY retrieved_at,observed_at",(now,now)):
                zerion[row['wallet']] = dict(row)
            for row in db.execute("SELECT * FROM external_wallet_metrics WHERE provider='nansen' AND payload IS NOT NULL AND observed_at<=? AND retrieved_at<=? ORDER BY retrieved_at,observed_at",(now,now)):
                nansen[row['wallet']] = dict(row)
        if 'external_fetch_state' in tables:
            attempts = {(r['provider'],r['wallet']):dict(r) for r in db.execute('SELECT * FROM external_fetch_state WHERE attempted_at<=?',(now,))}
    paper = {}
    paper_path = Path(paper_path or path.parent/'paper_copy.sqlite3').resolve()
    if paper_path.exists():
        with closing(sqlite3.connect(paper_path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
            for wallet, in db.execute('SELECT wallet FROM wallets'):
                paper[wallet] = {'forward_copy_buys':0,'forward_copy_sells':0}
            for wallet,buys,sells in db.execute("SELECT wallet,SUM(side='BUY'),SUM(side='SELL') FROM fills WHERE filled_at<=? GROUP BY wallet",(now,)):
                paper[wallet] = {'forward_copy_buys':buys,'forward_copy_sells':sells}
    rows = []
    for wallet in set(local)|set(dune)|set(zerion)|set(nansen):
        own, d, z = local.get(wallet,{}), dune.get(wallet,{}), zerion.get(wallet,{})
        dp, zp = json.loads(d['payload']) if d else {}, json.loads(z['payload']) if z else {}
        np = json.loads(nansen[wallet]['payload']) if wallet in nansen else {}
        reasons = own.get('paper_review_blockers', ['local_audit_required'])
        is_selected = wallet in selected and wallet not in excluded
        state = 'PAPER_SELECTED' if is_selected else 'WATCH' if own else 'EXTERNAL_CANDIDATE'
        last_error = attempts.get(('zerion',wallet),{}).get('error_category') or attempts.get(('zerion',''),{}).get('error_category')
        dune_error = attempts.get(('dune',''),{}).get('error_category')
        rows.append({'wallet':wallet, 'status':state, 'live_signal_authority':False,
            'external_observed_at':observed.get(wallet), 'local_first_discovered_at':own.get('first_discovered_at'),
            'dune_activity':{k:dp.get(k) for k in ('first_seen_at','last_seen_at','buy_count','sell_count','trade_count','distinct_tokens','volume_usd','realized_pnl','roi','win_rate')},
            'dune_provenance':dp.get('provenance'), 'dune_retrieved_at':d.get('retrieved_at'),
            'dune_stale':not d or now-dp.get('provenance',{}).get('execution_ended_at',0)>cfg.dune_refresh_seconds or bool(dune_error),
            'dune_error':dune_error,
            'zerion_metrics':zp.get('metrics'), 'zerion_breakdown':zp.get('breakdown'),
            'zerion_chain':zp.get('chain'), 'zerion_observed_at':z.get('observed_at'),
            'zerion_retrieved_at':z.get('retrieved_at'), 'zerion_data_complete':zp.get('data_complete',False),
            'zerion_missing_fields':zp.get('missing_fields'), 'zerion_error':last_error,
            'zerion_stale':not z or now-z['retrieved_at']>cfg.zerion_refresh_seconds or bool(last_error),
            'nansen_metrics':np.get('metrics'), 'nansen_flags':np.get('flags'),
            'nansen_date':np.get('date'), 'nansen_retrieved_at':np.get('retrieved_at'),
            'nansen_pages_complete':np.get('pages_complete',False),
            'nansen_data_complete':False,
            'infrastructure_excluded':wallet in excluded,
            'identity_cluster_id':own.get('identity_cluster_id'), 'identity_eligible':own.get('identity_eligible'),
            'funder':own.get('funder'), 'related_wallets_count':own.get('related_wallets_count'),
            'concentration_risk':own.get('concentration_risk'),
            'largest_position_contribution':own.get('largest_position_contribution'),
            'historical_class':own.get('historical_class'), 'local_data_complete':own.get('data_complete',False),
            'local_conservative_pnl_usdc':own.get('conservative_pnl_usdc'),
            'local_analysis_status':own.get('analysis_status','WAITING'),
            'historical_evidence_sufficient':None if own.get('analysis_status')!='ANALYZED' else not any(
                reason in own.get('blocking_reasons',[]) for reason in ('insufficient_buys',
                'insufficient_closed_positions','insufficient_profitable_tokens',
                'regularity_not_established','evidence_period_too_short')),
            'local_strict_blocking_reasons':own.get('blocking_reasons'),
            'paper_review_warnings':own.get('paper_review_warnings'),
            'local_blocking_reasons':reasons,
            'paper_forward_evidence':paper.get(wallet),
            'reason_not_paper_selected':[] if is_selected else list(reasons or ['not_in_current_policy_export'])})
    def order(row):
        gain = None if row['zerion_stale'] else (row['zerion_metrics'] or {}).get('total_gain')
        if gain is None: gain = row['local_conservative_pnl_usdc']
        return (row['status']!='PAPER_SELECTED',row['infrastructure_excluded'],
                row['local_analysis_status']!='ANALYZED',gain is None, -(gain or 0),row['wallet'])
    return sorted(rows,key=order)


def show(rows, top):
    def text(value):
        return 'N/A' if value is None else f'{value:.2f}' if isinstance(value,float) else str(value)
    print(BANNER)
    print('N/A = absent, jamais zero presume. PnL externes = historique du fournisseur.')
    for index,row in enumerate(rows[:top],1):
        d, z = row['dune_activity'], row['zerion_metrics'] or {}
        print(f"{index:>3} {row['wallet']} {row['status']} | local={row['historical_class'] or 'N/A'}")
        print('    Dune trades='+text(d['trade_count'])+' tokens='+text(d['distinct_tokens'])+' volume=$'+text(d['volume_usd']))
        print('    Zerion realized=$'+text(z.get('realized_gain'))+' unrealized=$'+text(z.get('unrealized_gain'))+
              ' total=$'+text(z.get('total_gain'))+' ROI='+text(z.get('relative_total_gain_percentage'))+'%'+
              ' fees=$'+text(z.get('total_fee'))+' net invested=$'+text(z.get('net_invested')))
        print('    Audit='+row['local_analysis_status']+' complete='+str(row['local_data_complete'])+
              ' historique_suffisant='+text(row['historical_evidence_sufficient'])+
              ' PnL_prudent=$'+text(row['local_conservative_pnl_usdc'])+
              ' infra='+str(row['infrastructure_excluded'])+' cluster='+text(row['identity_cluster_id'])+
              ' funder='+text(row['funder'])+' concentration='+text(row['concentration_risk'])+
              ' paper_forward='+text(row['paper_forward_evidence']))
        if row.get('nansen_metrics'):
            n = row['nansen_metrics']
            print('    Nansen (recherche): realized=$'+text(n.get('realized_gain'))+
                  ' unrealized=$'+text(n.get('unrealized_gain'))+' total=$'+text(n.get('total_gain')))
            print('    Nansen reserves: '+', '.join(row.get('nansen_flags') or []))
        print('    Blocages: '+', '.join(row['reason_not_paper_selected']))
        print('    Reserves paper: '+', '.join(row['paper_review_warnings'] or []))
        print('    Sources: Dune stale='+str(row['dune_stale'])+' Zerion stale='+str(row['zerion_stale'])+
              ' Zerion complete='+str(row['zerion_data_complete'])+' error='+text(row['zerion_error']))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--top',type=int,default=20)
    parser.add_argument('--db',default='data/arc_bot.sqlite3')
    parser.add_argument('--output-dir',default='data')
    args = parser.parse_args(argv)
    if args.top < 1: parser.error('--top must be positive')
    try:
        rows = combined_report(args.db,SETTINGS)
        output = Path(args.output_dir)
        export_csv(rows,output/'external_wallet_ranking.csv')
        write_json(output/'external_wallet_ranking.json',{'notice':BANNER,'generated_at':int(time.time()),'wallets':rows})
        show(rows,args.top)
        print(f'Total: {len(rows)} | PAPER_SELECTED: {sum(r["status"]=="PAPER_SELECTED" for r in rows)}')
        return 0
    except Exception as error:
        print('External report unavailable | category='+type(error).__name__); return 1


if __name__ == '__main__': raise SystemExit(main())
