"""Resumable, credit-capped Nansen research. Never changes engine authority."""
import argparse
import json
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from atomic_json import write_json
from config import SETTINGS
from external_http import ExternalError, digest
from external_wallet_report import combined_report
from external_wallet_store import ExternalStore
from nansen_audit import START, import_result
from nansen_client import SUMMARY, NansenHTTP, NansenClient, summary
from nansen_queue import Campaign
from process_lock import ProcessLock
from wallet_policy import infrastructure_addresses

STATUS = Path('data/nansen_bulk_status.json')


def import_summary(store, payload):
    with store.db:
        store.db.execute('''INSERT OR IGNORE INTO external_wallet_metrics
            VALUES(?,'nansen_summary',?,?,?,5042,?,0,NULL)''',
            (payload['wallet'],digest(payload),payload['retrieved_at'],payload['retrieved_at'],
             json.dumps(payload,allow_nan=False)))


def replay_imports(queue, store):
    # Idempotent recovery of the small gap between the two independent databases.
    for job in queue.db.execute('SELECT summary,result FROM jobs WHERE excluded=0'):
        if job['summary']: import_summary(store,json.loads(job['summary']))
        if job['result']:
            p=json.loads(job['result']); import_result(store,p,p['retrieved_at'])


def step(queue, http, store):
    state = queue.state
    if state.get('halt'):
        state['status']='REVIEW_REQUIRED'; queue.persist(); return False
    if state.get('retry_at',0)>time.time():
        state['status']='RETRY_WAIT'; queue.persist(); return False
    job = queue.next_job('SUMMARIES'); phase='SUMMARIES'
    if job is None: job=queue.next_job('DETAILS'); phase='DETAILS'
    if job is None:
        pending=queue.db.execute('SELECT COUNT(*) FROM jobs WHERE excluded=0 AND result IS NULL').fetchone()[0]
        state['status']='COMPLETE' if pending==0 else 'PARTIAL_WAIT'
        queue.persist(); return False
    wallet=job['wallet']; state['status']=phase
    try:
        if phase=='SUMMARIES':
            body={'chain':'arc','address':wallet,'date':state['date']}
            parsed=summary(http.post(SUMMARY,body))
            cached=state['cache'][digest([SUMMARY,body])]
            payload={'wallet':wallet,'source':'nansen_summary','chain_id':5042,
                     'date':state['date'],'summary':parsed,'retrieved_at':int(cached['received_at']),
                     'data_complete':False,'live_signal_authority':False,
                     'flags':['realized_only_open_positions_not_checked','history_starts_2026_07_15',
                              'provider_pnl_not_local_audit','gas_fee_treatment_unverified']}
            queue.save_summary(wallet,payload)
            import_summary(store,payload)
        else:
            result=NansenClient(http).pnl(wallet,state['date'])
            result['retrieved_at']=int(time.time())
            queue.save_result(wallet,result)
            import_result(store,result,result['retrieved_at'])
        state['consecutive_errors']=0; state.pop('last_error',None); queue.persist()
        return True
    except ExternalError as error:
        category=error.category; state['last_error']=category
        if category in ('nansen_request_budget','nansen_credit_reserve'):
            state['status']='BUDGET_STOP'; queue.persist(); return False
        if category in ('nansen_http_401','nansen_http_403','nansen_http_402','missing_nansen_key',
                        'nansen_billing_unverified'):
            state['halt']=category; state['status']='REVIEW_REQUIRED'; queue.persist(); return False
        if error.retry_after:
            state['retry_at']=max(state.get('retry_at',0),time.time()+error.retry_after)
            state['status']='RETRY_WAIT'; queue.persist(); return False
        # A malformed wallet stays visible, with its cached pages preserved.
        queue.defer(wallet,category,365*86400)
        state['consecutive_errors']=state.get('consecutive_errors',0)+1
        if state['consecutive_errors']>=3:
            state['halt']='repeated_nansen_schema_errors'; state['status']='REVIEW_REQUIRED'
        queue.persist(); return not state.get('halt')


def run(cfg=SETTINGS, once=False, db_path='data/arc_bot.sqlite3', campaign_path='data/nansen_bulk.sqlite3',
        trial_path='data/nansen_audit.json', status_path=STATUS, http_factory=NansenHTTP):
    if not cfg.nansen_bulk_enabled:
        print('Nansen bulk disabled; no requests.'); return 0
    if cfg.chain_id!=5042: raise ExternalError('wrong_chain')
    # Shared with the trial CLI: two Nansen request loops never run together.
    with ProcessLock('data/nansen_audit.lock'), closing(Campaign(campaign_path)) as queue:
        if queue.state is None:
            trial=json.loads(Path(trial_path).read_text()) if Path(trial_path).exists() else {
                'date':{'from':START,'to':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')},'cache':{}}
            queue.initialize(combined_report(db_path,cfg),trial,cfg.nansen_bulk_max_requests)
        elif queue.state['max_requests']!=cfg.nansen_bulk_max_requests:
            raise ExternalError('nansen_campaign_budget_changed')
        queue.exclude(infrastructure_addresses(cfg))
        with closing(ExternalStore(db_path)) as store, closing(http_factory(cfg.nansen_api_key,queue.state,queue.persist)) as http:
            replay_imports(queue,store)
            previous=None
            while True:
                queue.exclude(infrastructure_addresses(cfg))
                moved=step(queue,http,store)
                progress=queue.progress(); write_json(status_path,progress)
                counts=(progress['status'],progress['summaries']//25,progress['details']//25)
                if counts!=previous:
                    print(json.dumps({k:progress[k] for k in ('status','total','summaries','details',
                        'attempts','max_requests','credits_remaining','errors')}),flush=True)
                    previous=counts
                if once: return 0
                if not moved: time.sleep(30)  # No requests after a budget stop or completion.


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.status:
            print(STATUS.read_text(encoding='utf-8') if STATUS.exists() else 'No Nansen campaign started.'); return 0
        return run(once=args.once)
    except KeyboardInterrupt: return 0
    except Exception as error:
        print('Nansen bulk stopped | category='+(error.category if isinstance(error,ExternalError) else type(error).__name__),flush=True)
        return 1


if __name__=='__main__': raise SystemExit(main())
