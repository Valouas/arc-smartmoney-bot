"""Explicit 20-wallet Nansen trial; never runs in the background or authorizes paper."""
import argparse
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from atomic_json import write_json
from config import SETTINGS
from external_http import ExternalError, digest, stamp
from external_wallet_report import combined_report
from external_wallet_store import ExternalStore
from nansen_client import NansenClient, NansenHTTP
from process_lock import ProcessLock

START = '2026-07-15T00:00:00Z'


def select_sample(rows, limit=20):
    """Compare diverse available references without changing anyone's eligibility."""
    eligible = [r for r in rows if not r['infrastructure_excluded'] and r.get('zerion_metrics')]
    by_wallet = {r['wallet']:r for r in eligible}
    chosen = []
    preferred = '0x447d965a66e419c4a0a912eaad50a2ca9faf954b'
    if preferred in by_wallet: chosen.append(by_wallet[preferred])
    gain = lambda r:(r['zerion_metrics'].get('total_gain') or 0)
    streams = [sorted(eligible,key=lambda r:(-gain(r),r['wallet'])),
               sorted(eligible,key=lambda r:(gain(r),r['wallet'])),
               sorted(eligible,key=lambda r:((r['zerion_metrics'].get('unrealized_gain') or 0),r['wallet'])),
               sorted(eligible,key=lambda r:(r['dune_activity'].get('buy_count') is None,
                                             r['dune_activity'].get('buy_count') or 0,r['wallet']))]
    for index in range(len(eligible)):
        for stream in streams:
            row = stream[index]
            if row['wallet'] not in {r['wallet'] for r in chosen}: chosen.append(row)
            if len(chosen) >= limit: return chosen
    return chosen


def new_state(rows, cutoff, limit):
    if not 1 <= limit <= 100 or not stamp(START) < stamp(cutoff) <= int(time.time()):
        raise ExternalError('invalid_nansen_trial_scope')
    selected = select_sample(rows)
    references = []
    for row in selected:
        references.append({k:row.get(k) for k in ('wallet','status','zerion_metrics',
            'zerion_retrieved_at','zerion_stale','zerion_data_complete','zerion_missing_fields',
            'local_conservative_pnl_usdc','local_data_complete','local_analysis_status',
            'local_strict_blocking_reasons')})
    return {'version':1,'created_at':int(time.time()),'date':{'from':START,'to':cutoff},
            'max_requests':limit,'attempts':0,'credits_used_observed':0,'credits_remaining':None,
            'cache':{},'selected':references,'results':{},'errors':{},'status':'PREPARED',
            'live_signal_authority':False}


def import_result(store, payload, now):
    """Only a provider observation. No candidate status, profile or trade writes."""
    with store.db:
        store.db.execute('''INSERT OR IGNORE INTO external_wallet_metrics
            VALUES(?,'nansen',?,?,?,5042,?,0,NULL)''',
            (payload['wallet'],digest(payload),now,now,json.dumps(payload,allow_nan=False)))


def comparison(state):
    rows = []
    for ref in state['selected']:
        wallet = ref['wallet']; result = state['results'].get(wallet)
        row = {**ref,'nansen':result,'error':state['errors'].get(wallet),
               'comparison_is_approximate':True,'live_signal_authority':False}
        if result:
            z, n = ref['zerion_metrics'], result['metrics']
            row['differences_nansen_minus_zerion'] = {
                k:None if n[k] is None or z.get(k) is None else n[k]-z[k]
                for k in ('realized_gain','unrealized_gain','total_gain')}
            row['comparison_flags'] = ['different_scope_or_accounting_possible']
            if n['total_gain'] is not None and z.get('total_gain') is not None and n['total_gain']*z['total_gain'] < 0:
                row['comparison_flags'].append('opposite_pnl_signs')
            if not any(result['positions'].values()):
                row['comparison_flags'].append('no_positions_returned_not_proof_of_inactivity')
        rows.append(row)
    return {'generated_at':int(time.time()),'status':state['status'],
            'notice':'Research only; scope, cutoff, prices, fees and accounting may differ. No promotion authority.',
            'date':state['date'],'attempts':state['attempts'],
            'credits_used_observed':state['credits_used_observed'],
            'credits_remaining':state['credits_remaining'],
            'wallets_selected':len(rows),'wallets_received':len(state['results']),'wallets':rows}


def show(report):
    print('Nansen / Zerion : recherche uniquement, periodes et comptabilite potentiellement differentes.')
    for row in report['wallets']:
        result = row.get('nansen')
        if not result:
            print(row['wallet']+' | INCOMPLETE | '+str(row.get('error'))); continue
        value = lambda x:'N/A' if x is None else f'{x:.2f}'
        n, z = result['metrics'], row['zerion_metrics']
        print(row['wallet']+' | Nansen $'+value(n['total_gain'])+' | Zerion $'+value(z.get('total_gain'))+
              ' | ouvert $'+value(n['unrealized_gain']))
        print('  Reserves: '+', '.join(row['comparison_flags']+result['flags']))
    print(json.dumps({k:v for k,v in report.items() if k not in ('wallets','notice','date')}))


def collect(state, persist, client, store, excluded):
    state['status'] = 'RUNNING'; persist()
    for ref in state['selected']:
        wallet = ref['wallet']
        if wallet in excluded:
            state['errors'][wallet] = 'infrastructure_excluded'; continue
        if wallet in state['results']:
            import_result(store,state['results'][wallet],state['results'][wallet]['retrieved_at'])
            continue
        try:
            result = client.pnl(wallet,state['date'])
            result['retrieved_at'] = int(time.time())
            # Persist before database import so a restart needs no extra API request.
            state['results'][wallet] = result; state['errors'].pop(wallet,None); persist()
            import_result(store,result,result['retrieved_at'])
            print('Nansen wallets received='+str(len(state['results']))+'/'+str(len(state['selected'])),flush=True)
        except ExternalError as error:
            state['errors'][wallet] = error.category
            # Trial: any unexpected response is investigated before spending more.
            state['status'] = 'DEFERRED'; persist(); return
        persist()
    state['status'] = 'COMPLETE'; persist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--report',action='store_true',help='Read the saved trial without API or database writes')
    parser.add_argument('--db',default='data/arc_bot.sqlite3')
    parser.add_argument('--state',default='data/nansen_audit.json')
    parser.add_argument('--max-requests',type=int,default=60)
    parser.add_argument('--cutoff')
    args = parser.parse_args(argv)
    if args.report:
        try:
            show(comparison(json.loads(Path(args.state).read_text(encoding='utf-8')))); return 0
        except Exception as error:
            print('Nansen report unavailable | category='+type(error).__name__); return 1
    if not args.run:
        print('Nansen trial: up to 20 wallets, summary + realized + open positions; --run required.'); return 0
    try:
        from wallet_policy import infrastructure_addresses
        if SETTINGS.chain_id != 5042: raise ExternalError('wrong_chain')
        path = Path(args.state)
        with ProcessLock('data/nansen_audit.lock'):
            if path.exists():
                state = json.loads(path.read_text(encoding='utf-8'))
                if (state.get('version') != 1 or state['date']['from'] != START
                        or state['max_requests'] != args.max_requests
                        or (args.cutoff and args.cutoff != state['date']['to'])):
                    raise ExternalError('nansen_trial_scope_changed')
            else:
                state = new_state(combined_report(args.db,SETTINGS),args.cutoff or
                                  datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),args.max_requests)
            persist = lambda:write_json(path,state)
            persist()
            with closing(ExternalStore(args.db)) as store, closing(NansenHTTP(SETTINGS.nansen_api_key,state,persist)) as http:
                collect(state,persist,NansenClient(http),store,infrastructure_addresses(SETTINGS))
            report = comparison(state)
            write_json(path.with_suffix('.report.json'),report)
            print(json.dumps({k:v for k,v in report.items() if k not in ('wallets','notice','date')}))
            return 0 if state['status'] == 'COMPLETE' else 1
    except Exception as error:
        print('Nansen trial stopped | category='+(error.category if isinstance(error,ExternalError) else type(error).__name__))
        return 1


if __name__ == '__main__': raise SystemExit(main())
