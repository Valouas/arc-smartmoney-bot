"""Explicit, resumable Dune x10 research collection. Never a trading authority."""
import argparse
import csv
import json
import re
import time
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from atomic_json import write_json
from config import SETTINGS
from dune_discovery import DuneDiscovery
from external_http import ExternalError, ExternalHTTP, digest, number, wallet_address
from external_wallet_store import ExternalStore
from process_lock import ProcessLock

SQL = Path(__file__).parent/'queries/dune_arc_x10_history.sql'
COVERAGE = 'reviewed_v4_direct_usdc_single_swap_hourly_median'


def windows(start, end):
    start,end=date.fromisoformat(start),date.fromisoformat(end)
    if start>=end: raise ValueError('Invalid date interval')
    result=[]
    while start<end:
        following=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
        last=min(end,following)
        result.append({'start':start.isoformat(),'end':last.isoformat(),'status':'NEW'})
        start=last
    return result


def utc_stamp(value):
    if not re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d',value):
        raise ValueError('Expected UTC timestamp YYYY-MM-DD HH:MM:SS')
    return int(datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())


class QueryAPI:
    def __init__(self,cfg):
        self.http=ExternalHTTP('dune',cfg.dune_api_key)

    def post(self,path,payload):
        # Execution POSTs must never be automatically retried: an uncertain
        # response may already have started a billable execution.
        if not re.fullmatch(r'query/[1-9][0-9]*/execute|usage',path):
            raise ExternalError('invalid_route')
        try:
            with self.http.session.post(self.http.base+path,json=payload,
                    headers=self.http.headers,timeout=(5,45),allow_redirects=False) as r:
                if r.status_code!=200: raise ExternalError('http_'+str(r.status_code))
                if len(r.content)>self.http.max_bytes: raise ExternalError('payload_limit')
                data=r.json()
                if not isinstance(data,dict): raise ExternalError('invalid_response')
                return data
        except requests.RequestException: raise ExternalError('submission_uncertain') from None
        except ValueError: raise ExternalError('invalid_response') from None

    def usage(self):
        data=self.post('usage',{})
        today=datetime.now(timezone.utc).date().isoformat()
        periods=[p for p in data.get('billing_periods',[]) if p['start_date']<=today<p['end_date']]
        if len(periods)!=1: raise ExternalError('billing_period_unknown')
        p=periods[0]
        return {'period':p['start_date'],'used':number(p['credits_used']),
                'included':number(p['credits_included'])}

    def close(self): self.http.close()


class WindowHTTP:
    """Validate query scope/evidence before the standard row/pagination parser."""
    def __init__(self,http,window,cutoff):
        self.http,self.window,self.cutoff=http,window,cutoff
        self.raw={}

    def get(self,path,params):
        data=self.http.get(path,params)
        for row in data.get('result',{}).get('rows',[]):
            if not isinstance(row,dict): raise ExternalError('invalid_row')
            if (row.get('window_start')!=self.window['start'] or row.get('window_end')!=self.window['end']
                    or row.get('observation_cutoff')!=self.cutoff or row.get('coverage')!=COVERAGE):
                raise ExternalError('wrong_history_scope')
            if number(row.get('best_observed_multiple'))<10 or type(row.get('winner_tokens')) is not int or row['winner_tokens']<1:
                raise ExternalError('missing_x10_evidence')
            self.raw[wallet_address(row.get('wallet'))]=row
        return data


def fetch_window(api,cfg,window,cutoff):
    wrapped=WindowHTTP(api.http,window,cutoff)
    result=DuneDiscovery(cfg,wrapped).fetch(execution_id=window['execution_id'],exhaustive=True)
    if any(k!='infrastructure_excluded' and n for k,n in result['rejected'].items()):
        raise ExternalError('history_rows_rejected')
    if result['selection_limited']: raise ExternalError('partial_history_window')
    earliest=utc_stamp(window['start']+' 00:00:00')
    latest=min(utc_stamp(window['end']+' 00:00:00'),utc_stamp(cutoff))
    for row in result['rows']:
        if not earliest<=row['first_seen_at']<=row['last_seen_at']<latest:
            raise ExternalError('history_timestamp_outside_window')
        raw=wrapped.raw.get(row['wallet'])
        if raw is None: raise ExternalError('missing_history_evidence')
        row.update({key:raw[key] for key in ('best_observed_multiple','winner_tokens','coverage',
                    'window_start','window_end','observation_cutoff')})
    result.update(source='dune_x10',window_start=window['start'],window_end=window['end'],
                  observation_cutoff=cutoff,coverage=COVERAGE,live_signal_authority=False)
    return result


def import_window(store,result,cfg):
    now=result['retrieved_at']
    provenance={k:v for k,v in result.items() if k!='rows'}
    with store.db:
        for row in result['rows']:
            payload=json.dumps({**row,'provenance':provenance},allow_nan=False)
            store.db.execute('''INSERT INTO external_wallet_candidates
                (wallet,provider,observed_at,retrieved_at,chain_id,payload,data_complete)
                VALUES(?,'dune_x10',?,?,5042,?,1) ON CONFLICT(wallet,provider) DO UPDATE SET
                retrieved_at=excluded.retrieved_at,payload=excluded.payload,data_complete=1,error_category=NULL''',
                (row['wallet'],now,now,payload))
            store.db.execute('''INSERT OR IGNORE INTO external_wallet_metrics
                VALUES(?,'dune_x10',?,?,?,5042,?,1,NULL)''',
                (row['wallet'],result['execution_id']+':'+row['raw_hash'],now,now,payload))
    from wallet_policy import infrastructure_addresses
    store.bridge(cfg,now,infrastructure_addresses(cfg))


def report(job,path):
    wallets={}
    for window in job['windows']:
        if window['status']!='COMPLETE': continue
        result=json.loads(Path(window['result_path']).read_text(encoding='utf-8'))
        for row in result['rows']:
            current=wallets.setdefault(row['wallet'],{'wallet':row['wallet'],'first_seen_at':row['first_seen_at'],
                'last_seen_at':row['last_seen_at'],'buy_count':0,'sell_count':0,'periods':0,
                'best_observed_multiple':0,'live_signal_authority':False})
            current['first_seen_at']=min(current['first_seen_at'],row['first_seen_at'])
            current['last_seen_at']=max(current['last_seen_at'],row['last_seen_at'])
            current['best_observed_multiple']=max(current['best_observed_multiple'],row['best_observed_multiple'])
            for key in ('buy_count','sell_count'): current[key]+=row[key]
            current['periods']+=1
    rows=sorted(wallets.values(),key=lambda r:r['wallet'])
    output=path.parent/(path.stem+'.wallets.json')
    write_json(output,{'coverage':COVERAGE,'cutoff':job['cutoff'],'complete':job.get('status')=='COMPLETE',
                      'distinct_wallets':len(rows),'wallets':rows})
    with output.with_suffix('.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=('wallet','first_seen_at','last_seen_at','buy_count','sell_count','periods','best_observed_multiple','live_signal_authority')
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
    return len(rows)


def collect(job,path,api,cfg,store,*,sleep=time.sleep):
    if not cfg.external_discovery_enabled: raise ExternalError('external_discovery_disabled')
    # Do not silently execute a query edited in the dashboard since preparation.
    remote=api.http.get('query/'+cfg.dune_query_id)
    if digest(remote.get('query_sql'))!=job['sql_hash']: raise ExternalError('query_changed')
    for index,window in enumerate(job['windows']):
        if window['status']=='COMPLETE': continue
        if window['status'] in ('SUBMITTING','FAILED'):
            raise ExternalError('execution_requires_review')
        params={'start_date':window['start'],'end_date':window['end'],'cutoff_at':job['cutoff']}
        if window['status']=='NEW':
            usage=api.usage()
            if usage['period']!=job['usage_start']['period']: raise ExternalError('billing_period_changed')
            if usage['used']-job['usage_start']['used']>=job['max_credits'] or usage['included']-usage['used']<100:
                raise ExternalError('research_credit_budget')
            window['status']='SUBMITTING';write_json(path,job)
            response=api.post('query/'+cfg.dune_query_id+'/execute',{'query_parameters':params,'performance':'medium'})
            execution=response.get('execution_id')
            if not isinstance(execution,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',execution):
                raise ExternalError('invalid_execution')
            window.update(status='SUBMITTED',execution_id=execution);write_json(path,job)
        if window['status']=='SUBMITTED':
            deadline=time.monotonic()+900
            while True:
                status=api.http.get('execution/'+window['execution_id']+'/status')
                if status.get('execution_id')!=window['execution_id'] or status.get('query_id')!=int(cfg.dune_query_id):
                    raise ExternalError('wrong_execution_status')
                if status.get('state')=='QUERY_STATE_COMPLETED' and status.get('is_execution_finished') is True:
                    window['execution_cost_credits']=status.get('execution_cost_credits');break
                if status.get('state') in ('QUERY_STATE_FAILED','QUERY_STATE_CANCELLED','QUERY_STATE_EXPIRED'):
                    window.update(status='FAILED',failure=status['state']);write_json(path,job)
                    raise ExternalError('history_execution_failed')
                if time.monotonic()>=deadline: raise ExternalError('execution_still_pending')
                sleep(10)
            result=fetch_window(api,cfg,window,job['cutoff'])
            result_path=path.parent/(path.stem+'.'+window['start']+'.json')
            write_json(result_path,result)
            window.update(status='FETCHED',result_path=str(result_path));write_json(path,job)
        if window['status']=='FETCHED':
            result=json.loads(Path(window['result_path']).read_text(encoding='utf-8'))
            import_window(store,result,cfg)
            window.update(status='COMPLETE',wallets=len(result['rows']),rejected=result['rejected'])
            write_json(path,job)
            print(json.dumps({'period':index+1,'total_periods':len(job['windows']),**window}),flush=True)
    job['status']='COMPLETE';job['finished_at']=int(time.time());job['usage_end']=api.usage()
    job['distinct_wallets']=report(job,path)
    write_json(path,job)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true',help='Explicitly authorize Dune SQL executions using existing credits')
    p.add_argument('--query-id',required=True)
    p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--cutoff',required=True,help='Fixed UTC timestamp, YYYY-MM-DD HH:MM:SS')
    p.add_argument('--max-credits',type=float,default=500,help='Stop before further executions after this observed spend; in-flight cost may exceed it')
    p.add_argument('--state',default='data/dune_x10_history.json')
    p.add_argument('--db',default='data/arc_bot.sqlite3')
    args=p.parse_args(argv)
    if not 0<args.max_credits<=2000 or not 0<utc_stamp(args.cutoff)<=time.time(): p.error('Invalid budget or future cutoff')
    periods=windows(args.start,args.end)
    if not utc_stamp(args.start+' 00:00:00')<utc_stamp(args.cutoff)<=utc_stamp(args.end+' 00:00:00'):
        p.error('Cutoff must lie in the collection interval')
    cfg=replace(SETTINGS,dune_query_id=args.query_id)
    path=Path(args.state)
    signature={'query_id':args.query_id,'start':args.start,'end':args.end,'cutoff':args.cutoff,'sql_hash':digest(SQL.read_text(encoding='utf-8'))}
    if not args.run:
        print(json.dumps({**signature,'windows':periods,'executes_sql':False}));return 0
    try:
        with ProcessLock(str(path)+'.lock'),closing(QueryAPI(cfg)) as api,closing(ExternalStore(args.db)) as store:
            if path.exists():
                job=json.loads(path.read_text(encoding='utf-8'))
                if any(job.get(k)!=v for k,v in signature.items()): raise ExternalError('history_job_changed')
            else:
                job={**signature,'windows':periods,'usage_start':api.usage(),'max_credits':args.max_credits,'status':'RUNNING'}
                write_json(path,job)
            job['status']='RUNNING';job.pop('last_error',None);write_json(path,job)
            collect(job,path,api,cfg,store)
            print('History collection COMPLETE within declared source coverage.',flush=True)
        return 0
    except Exception as error:
        category=error.category if isinstance(error,ExternalError) else type(error).__name__
        # Keep the execution ID and period state for an explicit resume. No
        # uncertain submission is automatically rerun or labelled complete.
        if 'job' in locals() and all(job.get(k)==v for k,v in signature.items()):
            job.update(status='DEFERRED',last_error=category);write_json(path,job)
        print('History collection deferred | category='+category,flush=True)
        return 1


if __name__=='__main__': raise SystemExit(main())
