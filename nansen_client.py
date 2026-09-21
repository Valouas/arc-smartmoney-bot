"""Arc-only research PnL. Read-only POST routes, bounded credits and cached pages."""
import json
import math
import re
import time
from decimal import Decimal

import requests

from external_http import ExternalError, digest, finite_tree, number, retry_seconds, wallet_address

SUMMARY = 'profiler/address/pnl-summary'
DETAIL = 'profiler/address/pnl'
NUMBERS = ('token_price', 'roi_percent_realised', 'pnl_usd_realised',
           'pnl_usd_unrealised', 'roi_percent_unrealised', 'bought_amount', 'bought_usd',
           'cost_basis_usd', 'sold_amount', 'sold_usd', 'avg_sold_price_usd',
           'holding_amount', 'holding_usd', 'max_balance_held', 'max_balance_held_usd')


def page_info(data, expected):
    if not isinstance(data, dict) or data.get('error') or data.get('errors'):
        raise ExternalError('invalid_nansen_response')
    p = data.get('pagination')
    if (not isinstance(p, dict) or type(p.get('page')) is not int or p['page'] != expected
            or type(p.get('is_last_page')) is not bool
            or type(p.get('per_page')) is not int or not 1 <= p['per_page'] <= 1000):
        raise ExternalError('invalid_nansen_pagination')
    return p


def count(value):
    if type(value) is int and value >= 0: return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,16}', value): return int(value)
    raise ExternalError('invalid_nansen_count')


def summary(data):
    p = page_info(data, 1)
    if not p['is_last_page']: raise ExternalError('partial_nansen_summary')
    result = {k:number(data.get(k)) for k in ('realized_pnl_usd', 'realized_pnl_percent', 'win_rate')}
    if not 0 <= result['win_rate'] <= 1: raise ExternalError('invalid_nansen_win_rate')
    result.update({k:count(data.get(k)) for k in ('traded_token_count', 'traded_times')})
    tokens = data.get('top5_tokens')
    if not isinstance(tokens, list) or len(tokens) > 5: raise ExternalError('invalid_nansen_summary')
    for row in tokens:
        if not isinstance(row, dict) or row.get('chain') != 'arc': raise ExternalError('wrong_nansen_chain')
        wallet_address(row.get('token_address'))
    # Percent/ROI fields are ratios, unlike display percentages. Preserve raw units.
    return result


def detail_page(data, page):
    p = page_info(data, page)
    rows = data.get('data')
    if not isinstance(rows, list) or len(rows) > p['per_page'] or (not rows and not p['is_last_page']):
        raise ExternalError('invalid_nansen_page')
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get('chain', 'arc') != 'arc': raise ExternalError('wrong_nansen_chain')
        result.append({'token_address':wallet_address(row.get('token_address')),
                       **{k:number(row.get(k), optional=True) for k in NUMBERS},
                       **{k:count(row.get(k)) for k in ('nof_buys', 'nof_sells')}})
    return result, p['is_last_page']


class NansenHTTP:
    """The caller holds one process lock and persists reservations before requests.

    There are no payment/signing routes or automatic credit purchases. Every retry
    consumes an attempt; missing billing headers or changed pricing stop the run.
    """
    def __init__(self, key, state, persist, session=None, sleep=time.sleep, clock=time.time):
        if not key: raise ExternalError('missing_nansen_key')
        self.key, self.state, self.persist = key, state, persist
        self.session, self.sleep, self.clock = session or requests.Session(), sleep, clock
        self.next_request = 0

    def post(self, route, body):
        if route not in (SUMMARY, DETAIL) or body.get('chain') != 'arc':
            raise ExternalError('invalid_nansen_route_or_chain')
        wallet_address(body.get('address'))
        key = digest([route, body])
        if key in self.state['cache']: return self.state['cache'][key]['response']
        for attempt in range(3):
            if self.state.get('halt'): raise ExternalError(self.state['halt'])
            if self.state.get('retry_at', 0) > self.clock():
                raise ExternalError('nansen_deferred', self.state['retry_at']-self.clock())
            if self.state['attempts'] >= self.state['max_requests']: raise ExternalError('nansen_request_budget')
            remaining = self.state.get('credits_remaining')
            if remaining is not None and remaining <= self.state.get('credit_reserve',5): raise ExternalError('nansen_credit_reserve')
            delay = max(0, self.next_request-self.clock())
            if delay: self.sleep(delay)
            self.next_request = self.clock()+.6  # <= 100/minute, including retries.
            self.state['attempts'] += 1
            self.persist()  # An uncertain/crashed attempt is never refunded.
            retry = 0
            try:
                with self.session.post('https://api.nansen.ai/api/v1/'+route,
                        headers={'apikey':self.key, 'Content-Type':'application/json', 'Accept':'application/json'},
                        json=body, timeout=(5,30), allow_redirects=False, stream=True) as r:
                    h = {k.lower():v for k,v in r.headers.items()}
                    status = r.status_code
                    if status == 200:
                        try:
                            cost, used, left = [int(h[k]) for k in ('x-nansen-credits-cost',
                                'x-nansen-credits-used','x-nansen-credits-remaining')]
                            if min(cost,used,left) < 0 or max(cost,used) > 1: raise ValueError()
                        except (ValueError,KeyError,TypeError):
                            self.state['halt'] = 'nansen_billing_unverified'; self.persist()
                            raise ExternalError('nansen_billing_unverified') from None
                        self.state['credits_remaining'] = left
                        self.state['credits_used_observed'] += used
                        self.persist()
                        if 'application/json' not in h.get('content-type','').lower():
                            raise ExternalError('nansen_content_type')
                        raw = bytearray()
                        for chunk in r.iter_content(16384):
                            raw.extend(chunk)
                            if len(raw) > 4*1024*1024: raise ExternalError('nansen_payload_limit')
                        try:
                            data = json.loads(raw.decode('utf-8')); finite_tree(data)
                        except (ValueError,UnicodeError,RecursionError):
                            raise ExternalError('invalid_nansen_json') from None
                        if route == SUMMARY: summary(data)
                        else: detail_page(data, body['pagination']['page'])
                        self.state['cache'][key] = {'route':route,'request':body,'response':data,
                                                   'received_at':int(self.clock())}
                        self.persist()
                        return data
                    category = 'nansen_http_'+str(status)
                    if status not in (429,500,502,503,504): raise ExternalError(category)
                    retry = retry_seconds(h.get('retry-after'), self.clock())
                    if status == 429:
                        for prefix in ('ratelimit','x-ratelimit'):
                            if h.get(prefix+'-remaining') == '0':
                                retry = max(retry,retry_seconds(h.get(prefix+'-reset'),self.clock()))
            except requests.Timeout: category = 'nansen_timeout'
            except requests.ConnectionError: category = 'nansen_connection'
            except requests.RequestException: raise ExternalError('nansen_transport') from None
            if attempt == 2 or retry > 30:
                self.state['retry_at'] = self.clock()+max(60,retry); self.persist()
                raise ExternalError(category, max(60,retry))
            self.sleep(max(retry, .5*2**attempt))
        raise AssertionError('unreachable')

    def close(self): self.session.close()


class NansenClient:
    def __init__(self, http): self.http = http

    def pnl(self, wallet, date):
        wallet = wallet_address(wallet)
        body = {'chain':'arc','address':wallet,'date':date}
        overview = summary(self.http.post(SUMMARY,body))
        views = {}
        for realized in (True,False):
            tokens = {}
            for page in range(1,11):
                response = self.http.post(DETAIL,{**body,'filters':{'show_realized':realized},
                                                  'pagination':{'page':page,'per_page':1000}})
                rows, last = detail_page(response,page)
                for row in rows:
                    if row['token_address'] in tokens: raise ExternalError('duplicate_nansen_token')
                    tokens[row['token_address']] = row
                if last: break
            else: raise ExternalError('nansen_page_cap')
            views['realized' if realized else 'open'] = list(tokens.values())
        def total(rows, key):
            if any(r[key] is None for r in rows): return None
            value = float(sum((Decimal(str(r[key])) for r in rows),Decimal(0)))
            if not math.isfinite(value): raise ExternalError('non_finite_nansen_total')
            return value
        realized = total(views['realized'],'pnl_usd_realised')
        unrealized = total(views['open'],'pnl_usd_unrealised')
        # A summary cache can lag detailed views. Record discrepancies, never hide them.
        flags = ['provider_pnl_not_local_audit', 'gas_fee_treatment_unverified', 'history_starts_2026_07_15']
        if realized is None or unrealized is None: flags.append('missing_pnl_fields')
        if realized is not None and abs(realized-overview['realized_pnl_usd']) > max(.01,abs(realized)*.001):
            flags.append('summary_detail_mismatch')
        union = {r['token_address'] for rows in views.values() for r in rows}
        if len(union) != overview['traded_token_count']: flags.append('summary_detail_token_count_mismatch')
        if any(r['holding_amount'] is None or r['holding_usd'] is None for r in views['open']):
            flags.append('missing_open_inventory_fields')
        if any((r['holding_amount'] or 0)>0 and not r['token_price'] for r in views['open']):
            flags.append('open_position_without_positive_price')
        return {'wallet':wallet,'source':'nansen','chain':'arc','chain_id':5042,'date':date,
                'summary':overview,'positions':views,'pages_complete':True,'data_complete':False,
                'live_signal_authority':False,'flags':flags,
                'metrics':{'realized_gain':realized,'unrealized_gain':unrealized,
                           'total_gain':None if realized is None or unrealized is None else realized+unrealized}}
