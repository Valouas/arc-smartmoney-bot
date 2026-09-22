"""Explicit three-wallet, ten-attempt Nansen timing probe; evidence only."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import requests

from copy_holding_pilot import save_new

URL='https://api.nansen.ai/api/v1/profiler/address/transactions'
HASH=re.compile(r'0x[0-9a-fA-F]{64}\Z')
ADDRESS=re.compile(r'0x[0-9a-fA-F]{40}\Z')


def timestamp(value):
    if not isinstance(value,str):raise ValueError('missing_timestamp')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('timestamp_timezone_unverified')
    return int(result.timestamp())


def validate_page(data,page,start,end,allow_unverified_time=False):
    if not isinstance(data,dict) or data.get('error'):raise ValueError('invalid_envelope')
    p=data.get('pagination')
    if (not isinstance(p,dict) or type(p.get('page')) is not int or p['page']!=page
        or type(p.get('is_last_page')) is not bool or p.get('per_page')!=100):
        raise ValueError('invalid_pagination')
    rows=data.get('data')
    if not isinstance(rows,list) or len(rows)>100 or (not rows and not p['is_last_page']):raise ValueError('invalid_rows')
    records=[]
    for r in rows:
        if not isinstance(r,dict) or r.get('chain')!='arc' or not HASH.fullmatch(str(r.get('transaction_hash',''))):raise ValueError('invalid_chain_or_hash')
        raw_time=r.get('block_timestamp')
        try:ts=timestamp(raw_time)
        except ValueError as exc:
            if str(exc)!='timestamp_timezone_unverified' or not allow_unverified_time:raise
            # Preserve naive provider dates as observations, never UTC evidence.
            ts=None
        if ts is not None and not start<=ts<=end:raise ValueError('timestamp_outside_scope')
        records.append(dict(tx_hash=r['transaction_hash'].lower(),timestamp=ts,
            timestamp_raw=raw_time,timestamp_timezone_verified=ts is not None,
            source_type=r.get('source_type'),method=r.get('method'),
            sent_count=len(r.get('tokens_sent') or []),received_count=len(r.get('tokens_received') or [])))
    return records,p['is_last_page']


class Probe:
    def __init__(self,key,out,session=None,prior=None):
        if not key:raise ValueError('missing_nansen_key')
        self.key,self.out=key,Path(out)
        self.session=session or requests.Session()
        self.attempts=self.credits=0;self.billing_verified=True
        self.last_request=0;self.deadline=time.monotonic()+300
        self.cached=[]
        if prior:
            prior=Path(prior)
            manifest=json.loads((prior/'manifest.json').read_text())
            for name,digest in manifest.items():
                if Path(name).name!=name or hashlib.sha256((prior/name).read_bytes()).hexdigest()!=digest:
                    raise ValueError('prior_evidence_integrity')
            summary=json.loads((prior/'summary.json').read_text())
            if not summary['billing_verified']:raise ValueError('prior_billing_uncertain')
            self.attempts=summary['attempts'];self.credits=summary['credits_used_observed']
            attempts=sorted(prior.glob('attempt_*.json'))
            if len(attempts)!=self.attempts:raise ValueError('prior_attempt_count')
            for p in attempts:
                suffix=p.stem.split('_')[1]
                metadata=json.loads((prior/f'response_{suffix}.json').read_text())
                if metadata['status']!=200:raise ValueError('prior_http_failure')
                body=json.loads(p.read_text())['request']
                self.cached.append((body,json.loads((prior/f'response_{suffix}.body').read_bytes())))
            save_new(self.out/'prior_evidence.json',dict(directory=str(prior.resolve()),manifest=manifest))

    def page(self,wallet,date,page):
        if not ADDRESS.fullmatch(wallet) or not 1<=page<=3:raise ValueError('probe_scope')
        body=dict(chain='arc',address=wallet,date=date,hide_spam_token=False,
            pagination=dict(page=page,per_page=100),order_by=[dict(field='block_timestamp',direction='ASC')])
        for request,data in self.cached:
            if request==body:
                return validate_page(data,page,timestamp(date['from']),timestamp(date['to']),True)
        if self.attempts>=10 or time.monotonic()>=self.deadline:raise ValueError('probe_limit')
        delay=max(0,.7-(time.monotonic()-self.last_request))
        time.sleep(delay);self.last_request=time.monotonic();self.attempts+=1
        save_new(self.out/f'attempt_{self.attempts:02d}.json',dict(request=body,at=time.time()))
        try:
            with self.session.post(URL,headers={'apikey':self.key,'Content-Type':'application/json','Accept':'application/json'},
                json=body,timeout=(5,20),allow_redirects=False,stream=True) as response:
                headers={k.lower():v for k,v in response.headers.items() if 'credits-' in k.lower()}
                raw=bytearray()
                for chunk in response.iter_content(16384):
                    raw.extend(chunk)
                    if len(raw)>4*1024*1024:
                        (self.out/f'response_{self.attempts:02d}.partial').write_bytes(raw)
                        self.billing_verified=False
                        raise ValueError('payload_size_limit')
                (self.out/f'response_{self.attempts:02d}.body').write_bytes(raw)
                save_new(self.out/f'response_{self.attempts:02d}.json',dict(status=response.status_code,billing=headers,at=time.time()))
                try:
                    cost=int(headers['x-nansen-credits-cost']);used=int(headers['x-nansen-credits-used'])
                    if cost<0 or used<0:raise ValueError()
                    self.credits+=used
                    if cost>1 or used>1:raise ValueError()
                except (KeyError,TypeError,ValueError):
                    self.billing_verified=False;raise ValueError('billing_unverified_or_changed') from None
                if response.status_code!=200:raise ValueError('nansen_http_'+str(response.status_code))
                data=json.loads(raw)
                return validate_page(data,page,timestamp(date['from']),timestamp(date['to']),True)
        except requests.RequestException:
            self.billing_verified=False
            raise ValueError('nansen_transport_uncertain') from None


def run(scope,source,out,key,prior=None):
    spec=json.loads(Path(scope).read_text(encoding='utf-8'))
    if not isinstance(spec,list) or not 1<=len(spec)<=3 or len({r['wallet'] for r in spec})!=len(spec):raise ValueError('probe_scope_maximum_three_wallets')
    for r in spec:
        if not ADDRESS.fullmatch(r['wallet']):raise ValueError('probe_wallet')
        if timestamp(r['date']['from'])>=timestamp(r['date']['to']):raise ValueError('probe_dates')
    if prior and json.loads((Path(prior)/'scope.json').read_text())!=spec:raise ValueError('prior_scope_changed')
    out=Path(out)
    if out.exists():raise FileExistsError(out)
    # Refuse a missing/unusable source before spending any provider credits.
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.execute('SELECT wallet,tx_hash,payload FROM historical_transactions LIMIT 0')
    out.mkdir(parents=True,exist_ok=False);save_new(out/'scope.json',spec)
    probe=Probe(key,out,prior=prior);records={r['wallet']:[] for r in spec};finished=set();error=None
    try:
        # Fairness: every wallet gets page one before spending on deeper pages.
        for page in range(1,4):
            for r in spec:
                w=r['wallet']
                if w in finished:continue
                rows,last=probe.page(w,r['date'],page)
                before={t['tx_hash'] for t in records[w]}
                if before&{t['tx_hash'] for t in rows}:raise ValueError('cross_page_duplicate')
                records[w].extend(rows)
                if last:finished.add(w)
    except (ValueError,TypeError,KeyError) as exc:
        # Fixed parser categories only; raw exceptions from JSON must stay local.
        error=str(exc) if type(exc) is ValueError and re.fullmatch('[a-z0-9_]+',str(exc)) else type(exc).__name__
    finally:probe.session.close()
    report=[]
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for r in spec:
            w=r['wallet'];rows=records[w]
            local=dict(db.execute('SELECT tx_hash,payload FROM historical_transactions WHERE wallet=?',(w,)))
            matches=conflicts=naive_matches=0
            for t in rows:
                existing=json.loads(local[t['tx_hash']]) if local.get(t['tx_hash']) else None
                if existing and existing.get('timestamp') is not None:
                    if t['timestamp'] is not None:
                        matches+=existing['timestamp']==t['timestamp'];conflicts+=existing['timestamp']!=t['timestamp']
                    else:
                        # Diagnostic only: does the naive clock coincide with known UTC?
                        assumed=datetime.fromisoformat(t['timestamp_raw']).replace(tzinfo=timezone.utc).timestamp()
                        naive_matches+=existing['timestamp']==assumed
            hashes={t['tx_hash'] for t in rows}
            report.append(dict(wallet=w,pages_complete=w in finished,records=len(rows),unique_transactions=len(hashes),
                matching_local_hashes=len(hashes&local.keys()),new_hashes=len(hashes-local.keys()),
                local_transaction_count=len(local),local_hashes_not_returned=len(local.keys()-hashes),
                timestamps_matching_decoded=matches,timestamps_conflicting_decoded=conflicts,
                timezone_unverified_records=sum(t['timestamp'] is None for t in rows),
                naive_dates_matching_local_utc=naive_matches,
                source_types=sorted({str(t['source_type']) for t in rows}),
                live_signal_authority=False,paper_selected=False,holding_duration_verified=False))
    summary=dict(at=time.time(),attempts=probe.attempts,credits_used_observed=probe.credits,
        billing_verified=probe.billing_verified,rows=report,error=error,
        note='Provider timestamps and transfers are research evidence; receipt/DEX confirmation still required.')
    save_new(out/'summary.json',summary)
    save_new(out/'manifest.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()})
    print(json.dumps(summary,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scope',required=True);p.add_argument('--source',required=True);p.add_argument('--output-dir',required=True)
    p.add_argument('--prior-evidence',help='Reuse archived pilot pages and carry their attempts/credits into this cap.')
    args=p.parse_args()
    from config import SETTINGS
    run(args.scope,args.source,args.output_dir,SETTINGS.nansen_api_key,args.prior_evidence)


if __name__=='__main__':main()
