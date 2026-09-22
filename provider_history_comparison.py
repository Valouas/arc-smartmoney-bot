"""Explicit small research pilot; never imports evidence into trading authority.

prepare/report are offline. nansen fetches ONE page per sampled wallet using the
existing campaign budget; dune executes ONE query of sampled hashes.
Every request intent/body is retained. Uncertain requests are never retried.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time

import requests

from nansen_holding_campaign import backup, connect, publish, readonly, unresolved_attempts, write_new
from process_lock import ProcessLock

DEX_URL = 'https://api.nansen.ai/api/v1/profiler/dex-trades'
DUNE_URL = 'https://api.dune.com/api/v1/'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
EMITTER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
SWAP = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
USDC = '0x3600000000000000000000000000000000000000'
CASH_TOKENS = {USDC, '0xfffffffffffffffffffffffffffffffffffffffe'}


def stamp(n):
    return datetime.fromtimestamp(n, timezone.utc).isoformat()


def hex_value(value, size):
    if not isinstance(value, str) or not re.fullmatch('0x[0-9a-fA-F]{'+str(size)+'}', value):
        raise ValueError('invalid_hex')
    return value.lower()


def prepare(source, cache, out, wallets):
    wallets = list(dict.fromkeys(hex_value(w, 40) for w in wallets))
    if not 1 <= len(wallets) <= 5:
        raise ValueError('sample_one_to_five_wallets')
    snapshot = {'created_at': time.time(), 'wallets': [], 'rpc_receipts': {}}
    with closing(readonly(source)) as db, closing(readonly(cache)) as rpc:
        db.execute('BEGIN')
        for wallet in wallets:
            run = db.execute('SELECT * FROM historical_runs WHERE wallet=?', (wallet,)).fetchone()
            if not run:
                raise ValueError('missing_local_run')
            payloads = [json.loads(r[0]) for r in db.execute(
                'SELECT payload FROM historical_transactions WHERE wallet=? AND payload IS NOT NULL', (wallet,))]
            if not payloads:
                raise ValueError('missing_local_reference')
            snapshot['wallets'].append({'wallet': wallet, 'run': dict(run), 'transactions': payloads})
            for tx in payloads:
                h = hex_value(tx['tx_hash'], 64)
                r = rpc.execute("SELECT payload,digest FROM rpc_cache_entries WHERE chain=5042 AND method='eth_getTransactionReceipt' AND params=?",
                                (json.dumps([h], separators=(',', ':')),)).fetchone()
                if r:
                    if hashlib.sha256(r['payload'].encode()).hexdigest() != r['digest']:
                        raise ValueError('cache_integrity')
                    snapshot['rpc_receipts'][h] = json.loads(r['payload'])
    if sum(len(w['transactions']) for w in snapshot['wallets']) > 1000:
        raise ValueError('sample_too_large')
    Path(out).mkdir(parents=True, exist_ok=False)
    write_new(Path(out)/'reference.json', snapshot)


def reserve_research(db, request):
    """Same atomic cap as the running supplement, separate response namespace."""
    with db:
        db.execute('BEGIN IMMEDIATE')
        p = db.execute('SELECT * FROM policy WHERE id=1').fetchone()
        if not p or p['halt'] or p['attempts'] >= p['cap'] or unresolved_attempts(db):
            raise ValueError('shared_budget_or_halt')
        db.execute('''CREATE TABLE IF NOT EXISTS research_attempts(
            id INTEGER PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL,
            status INTEGER, billing TEXT, body BLOB, digest TEXT)''')
        attempt = p['attempts']+1
        db.execute("UPDATE policy SET attempts=?,halt='research_inflight' WHERE id=1", (attempt,))
        db.execute("INSERT INTO research_attempts(id,request,state) VALUES(?,?,'INFLIGHT')",
                   (attempt, json.dumps(request)))
    return attempt


def capture(session, method, url, headers, out, name, body=None, params=None):
    """Intent before request, append-only streamed body, sanitized metadata."""
    write_new(out/(name+'.intent.json'), dict(method=method, body=body, params=params, at=time.time()))
    raw = bytearray(); status = None; billing = {}; error = None
    try:
        with session.request(method, url, headers=headers, json=body, params=params,
                             timeout=(5, 60), allow_redirects=False, stream=True) as r:
            status = r.status_code
            billing = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith('x-nansen-credits-')}
            with (out/(name+'.body')).open('xb') as f:
                for chunk in r.iter_content(16384):
                    f.write(chunk); f.flush(); raw.extend(chunk)
                    if len(raw) > 8*1024*1024:
                        raise ValueError('payload_limit')
    except (requests.RequestException, ValueError):
        error = 'transport_or_payload_uncertain'
    meta = dict(status=status, billing=billing, error=error, bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), at=time.time())
    write_new(out/(name+'.response.json'), meta)
    return bytes(raw), meta


def nansen(out, campaign, key):
    if not key:
        raise ValueError('missing_key')
    ref = json.loads((out/'reference.json').read_text())
    with ProcessLock(campaign/'worker.lock'), closing(connect(campaign/'campaign.sqlite3')) as db, requests.Session() as s:
        backup(db, campaign)
        try:
            for entry in ref['wallets']:
                w = entry['wallet']; name = 'nansen_'+w
                if (out/(name+'.intent.json')).exists():
                    # A retry requires explicit review, even if the body was received.
                    continue
                body = dict(address=w, chain='arc', date={'from': '2026-07-15T00:00:00Z',
                            'to': stamp(entry['run']['cutoff_at'])},
                            pagination={'page': 1, 'per_page': 1000},
                            order_by=[{'field': 'block_timestamp', 'direction': 'ASC'}])
                attempt = reserve_research(db, dict(route='/profiler/dex-trades', body=body))
                raw, meta = capture(s, 'POST', DEX_URL, {'apikey': key}, out, name, body)
                failure = meta['error']
                used = 0
                try:
                    used = int(meta['billing']['x-nansen-credits-used'])
                    quoted = int(meta['billing']['x-nansen-credits-cost'])
                    if not 0 <= used <= 1 or not 0 <= quoted <= 1:
                        raise ValueError()
                except (KeyError, ValueError):
                    failure = failure or 'billing_unverified'
                if meta['status'] != 200:
                    failure = failure or 'http_failure'
                if int(meta['billing'].get('x-nansen-credits-remaining', '0')) <= 100:
                    failure = failure or 'account_reserve_100'
                with db:
                    db.execute('UPDATE research_attempts SET state=?,status=?,billing=?,body=?,digest=? WHERE id=?',
                        ('UNCERTAIN' if failure else 'RECEIVED', meta['status'], json.dumps(meta['billing']), raw, meta['sha256'], attempt))
                    db.execute('UPDATE policy SET used=used+?,halt=? WHERE id=1',
                               (max(0, used), 'research_'+failure if failure else None))
                if failure:
                    raise ValueError(failure)
                time.sleep(1)
        finally:
            backup(db, campaign)
            publish(db, campaign)
            write_new(out/('budget_'+str(time.time_ns())+'.json'), dict(db.execute('SELECT * FROM policy').fetchone()))


def dune_sql(ref):
    txs = {hex_value(t['tx_hash'], 64): t for w in ref['wallets'] for t in w['transactions'] if t.get('timestamp')}
    if not txs or len(txs)>1000:
        raise ValueError('invalid_sample')
    hashes = ','.join(sorted(txs))
    first = stamp(min(t['timestamp'] for t in txs.values()))[:10]
    last = stamp(max(t['timestamp'] for t in txs.values()))[:10]
    where = f"evt_block_date BETWEEN DATE '{first}' AND DATE '{last}' AND evt_tx_hash IN ({hashes})"
    return f'''-- Research sample only. No winner filter; no wallet authority.
SELECT 'transfer' AS kind, cast(evt_tx_hash as varchar) AS tx_hash,
 evt_index AS log_index, evt_tx_index AS tx_index, evt_block_number AS block_number,
 cast(evt_block_time as varchar) AS block_time, cast(contract_address as varchar) AS emitter,
 cast("from" as varchar) AS sender, cast("to" as varchar) AS recipient, cast(value as varchar) AS raw_value
FROM erc20_arc.evt_transfer WHERE {where}
UNION ALL
SELECT 'swap', cast(evt_tx_hash as varchar), evt_index, evt_tx_index, evt_block_number,
 cast(evt_block_time as varchar), cast(contract_address as varchar),
 cast(sender as varchar), '', ''
FROM uniswap_v4_arc.poolmanager_evt_swap
WHERE {where} AND contract_address={EMITTER}
'''


def dune(out, key):
    """One explicit query, no submission retries or automated purchases."""
    if not key:
        raise ValueError('missing_key')
    if (out/'dune_execute.intent.json').exists():
        raise ValueError('already_submitted_review_saved_execution')
    headers = {'X-Dune-Api-Key': key}
    with requests.Session() as s:
        raw, meta = capture(s, 'POST', DUNE_URL+'usage', headers, out, 'dune_usage_before', {})
        if meta['error'] or meta['status'] != 200:
            raise ValueError('usage_unavailable')
        today = datetime.now(timezone.utc).date().isoformat()
        periods = [p for p in json.loads(raw)['billing_periods'] if p['start_date'] <= today < p['end_date']]
        if len(periods)!=1 or periods[0]['credits_included']-periods[0]['credits_used'] < 100:
            raise ValueError('included_credit_reserve')
        sql = dune_sql(json.loads((out/'reference.json').read_text()))
        (out/'dune_sample.sql').write_text(sql, encoding='utf-8')
        raw, meta = capture(s, 'POST', DUNE_URL+'sql/execute', headers, out, 'dune_execute',
                            {'sql': sql, 'performance': 'medium'})
        if meta['error'] or meta['status'] != 200:
            raise ValueError('submission_failed_or_uncertain')
        execution = json.loads(raw)['execution_id']
        if not re.fullmatch('[A-Z0-9]{26}', execution):
            raise ValueError('invalid_execution')
        for poll in range(40):
            time.sleep(3)
            raw, meta = capture(s, 'GET', DUNE_URL+f'execution/{execution}/status', headers, out, f'dune_status_{poll}')
            if meta['error'] or meta['status'] != 200:
                capture(s, 'POST', DUNE_URL+f'execution/{execution}/cancel', headers, out, 'dune_cancel', {})
                break
            state = json.loads(raw)['state']
            if state == 'QUERY_STATE_COMPLETED':
                capture(s, 'GET', DUNE_URL+f'execution/{execution}/results', headers, out, 'dune_results', params={'limit': 10000})
                break
            if state in ('QUERY_STATE_FAILED', 'QUERY_STATE_CANCELLED'):
                break
        else:
            capture(s, 'POST', DUNE_URL+f'execution/{execution}/cancel', headers, out, 'dune_cancel', {})
        capture(s, 'POST', DUNE_URL+'usage', headers, out, 'dune_usage_after', {})


def load_response(out, name):
    meta = json.loads((out/(name+'.response.json')).read_text())
    raw = (out/(name+'.body')).read_bytes()
    if meta['error'] or meta['status']!=200 or hashlib.sha256(raw).hexdigest()!=meta['sha256']:
        raise ValueError('response_not_valid')
    return json.loads(raw)


def nansen_rows(data, wallet):
    p = data.get('pagination', {})
    rows = data.get('data')
    if p.get('page')!=1 or p.get('per_page')!=1000 or type(p.get('is_last_page')) is not bool or not isinstance(rows, list) or len(rows)>1000:
        raise ValueError('invalid_page')
    for r in rows:
        if r.get('chain')!='arc' or hex_value(r.get('trader_address'),40)!=wallet:
            raise ValueError('wrong_trader_or_chain')
        hex_value(r.get('transaction_hash'),64)
        hex_value(r.get('token_bought_address'),40); hex_value(r.get('token_sold_address'),40)
    # Multiple legs in one transaction are allowed, identical rows aren't proof.
    duplicate_rows = len(rows)-len({json.dumps(r, sort_keys=True) for r in rows})
    return rows, p['is_last_page'] and not duplicate_rows, duplicate_rows


def near(a, b):
    try:
        x,y=Decimal(str(a)),Decimal(str(b))
        return x.is_finite() and y.is_finite() and abs(x-y)<=max(Decimal('0.000001'),abs(y)*Decimal('0.000001'))
    except Exception:
        return False


def compare_wallet(entry, data):
    w=entry['wallet']; rows, complete, duplicates=nansen_rows(data,w)
    local=[(tx,t) for tx in entry['transactions'] if not tx.get('error') and not tx.get('outside_snapshot')
           for t in tx.get('trades',[]) if t.get('confirmation_eligible') is True]
    matches=[]
    for tx,t in local:
        pair=('token_bought','token_sold') if t['side']=='BUY' else ('token_sold','token_bought')
        found=[r for r in rows if r['transaction_hash'].lower()==tx['tx_hash'].lower()
               and r[pair[0]+'_address'].lower()==t['token'].lower()]
        amounts=any(near(r.get(pair[0]+'_amount'),t['token_amount']) and
                    r[pair[1]+'_address'].lower() in CASH_TOKENS and near(r.get(pair[1]+'_amount'),t['usdc_amount']) for r in found)
        times=[]
        for r in found:
            try:
                dt=datetime.fromisoformat(r['block_timestamp'].replace('Z','+00:00'))
                times.append(dict(offset_seconds=dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()-tx['timestamp'],
                                  timezone_explicit=dt.tzinfo is not None))
            except (KeyError,ValueError):
                pass
        matches.append(dict(tx_hash=tx['tx_hash'],side=t['side'],token=t['token'],
                            hash_direction_token_match=bool(found),amounts_match=amounts,timestamps=times))
    local_hashes={tx['tx_hash'].lower() for tx,t in local}
    return dict(wallet=w,local_confirmed_trades=len(local),nansen_rows=len(rows),
                provider_page_complete=complete,duplicate_rows=duplicates,
                hash_direction_token_matches=sum(m['hash_direction_token_match'] for m in matches),
                amount_matches=sum(m['amounts_match'] for m in matches),
                nansen_extra_hashes=sorted({r['transaction_hash'].lower() for r in rows}-local_hashes),
                comparisons=matches,history_completeness_authority=False,paper_selected=False)


def dune_rows(data):
    rows=data.get('result',{}).get('rows')
    total=data.get('result',{}).get('metadata',{}).get('total_row_count')
    if data.get('state')!='QUERY_STATE_COMPLETED' or not isinstance(rows,list) or total!=len(rows) or data.get('next_offset') is not None:
        raise ValueError('partial_dune_result')
    return rows


def compare_dune(ref, rows):
    local=[]
    for h,receipt in ref['rpc_receipts'].items():
        for log in receipt.get('logs',[]):
            topics=[x.lower() for x in log.get('topics',[])]
            if topics and topics[0]==TRANSFER and len(topics)==3:
                local.append(('transfer',h,int(log['logIndex'],16),log['address'].lower(),
                              '0x'+topics[1][-40:],'0x'+topics[2][-40:],str(int(log['data'],16))))
            elif topics and topics[0]==SWAP and log['address'].lower()==EMITTER:
                local.append(('swap',h,int(log['logIndex'],16),EMITTER,'','',''))
    remote=[]; timing_mismatches=[]
    txs={t['tx_hash']:t for w in ref['wallets'] for t in w['transactions']}
    for r in rows:
        kind=r['kind']
        if kind not in ('transfer','swap'):raise ValueError('unexpected_kind')
        remote.append((kind,hex_value(r['tx_hash'],64),int(r['log_index']),hex_value(r['emitter'],40),
                       r['sender'].lower() if kind=='transfer' else '',
                       r['recipient'].lower() if kind=='transfer' else '',str(r['raw_value']) if kind=='transfer' else ''))
        tx=txs.get(r['tx_hash'].lower())
        if tx and all(k in r for k in ('block_number','tx_index','block_time')):
            dt=datetime.fromisoformat(r['block_time'].replace(' UTC','+00:00').replace('Z','+00:00'))
            ts=dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
            if (r['block_number'],r['tx_index'],ts)!=(tx['block'],tx['index'],tx['timestamp']):
                timing_mismatches.append(r['tx_hash'])
        else:
            timing_mismatches.append(r['tx_hash'])
    a,b=Counter(local),Counter(remote)
    comparable=Counter(r for r in remote if r[1] in ref['rpc_receipts'])
    return dict(local_cached_receipts=len(ref['rpc_receipts']),local_events=len(local),dune_events=len(remote),
                exact_matches=sum((a&b).values()),missing_from_dune=list((a-b).elements()),
                extra_in_dune=list((comparable-a).elements()),events_without_local_receipt=sum((b-comparable).values()),
                timing_mismatch_hashes=sorted(set(timing_mismatches)),
                history_completeness_authority=False)


def report(out, dune_directory=None):
    ref=json.loads((out/'reference.json').read_text())
    results=[]
    for entry in ref['wallets']:
        try: results.append(compare_wallet(entry,load_response(out,'nansen_'+entry['wallet'])))
        except (ValueError,KeyError,TypeError,OSError):
            results.append(dict(wallet=entry['wallet'],error='missing_or_invalid_provider_evidence'))
    dune_out=Path(dune_directory) if dune_directory else out
    try: d=compare_dune(ref,dune_rows(load_response(dune_out,'dune_results')))
    except (ValueError,KeyError,TypeError,OSError): d={'error':'missing_or_invalid_provider_evidence'}
    result=dict(wallets=results,dune=d,read_only=True,production_modified=False,
                history_completeness_authority=False,paper_selected=False,
                limitations=['small_selected_sample_not_chain_coverage','Nansen_history_from_2026_07_15',
                             'DEX_trades_do_not_prove_external_transfers_or_initial_inventory',
                             'Dune_sample_is_ERC20_and_reviewed_V4_only_no_native_flows_or_gas',
                             'local_reconstruction_is_comparison_reference_not_infallible_truth'])
    path=out/f'comparison_{time.time_ns()}.json';write_new(path,result)
    print(json.dumps(dict(report=str(path),wallets=[{k:v for k,v in r.items() if k not in ('comparisons','nansen_extra_hashes')} for r in results],
                         dune={k:v for k,v in d.items() if k not in ('missing_from_dune','extra_in_dune')})))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','nansen','dune','report'])
    p.add_argument('--directory',required=True);p.add_argument('--source');p.add_argument('--cache')
    p.add_argument('--wallet',action='append',default=[]);p.add_argument('--budget-campaign')
    p.add_argument('--dune-directory')
    a=p.parse_args();out=Path(a.directory)
    if a.action=='prepare':prepare(a.source,a.cache,out,a.wallet)
    elif a.action=='report':report(out,a.dune_directory)
    else:
        from config import SETTINGS
        if a.action=='nansen':nansen(out,Path(a.budget_campaign),SETTINGS.nansen_api_key)
        else:dune(out,SETTINGS.dune_api_key)


if __name__=='__main__':main()
