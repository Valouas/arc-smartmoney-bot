"""Campaign-only RPC acceleration; bounded credentials stay in memory."""
import json
import re
import time
from functools import lru_cache
from urllib.parse import urlsplit

import requests

from rpc_transport import ResilientRPCProvider, RPCError, READ_METHODS
from rpc import ArcRPC, ERC20_ABI
from web3 import Web3


class CampaignArcRPC(ArcRPC):
    """Only metadata actually consumed by historical classify_trade.

    Keep symbol/decimals behavior, failure propagation and per-client caching.
    The general token_meta implementation used by the other bots is untouched.
    """
    @lru_cache(maxsize=4096)
    def token_meta(self, address):
        addr=Web3.to_checksum_address(address)
        c=self.w3.eth.contract(address=addr,abi=ERC20_ABI)
        out=dict(address=addr,symbol='?',decimals=None)
        try:out['symbol']=c.functions.symbol().call()
        except RPCError:raise
        except Exception:pass
        try:out['decimals']=int(c.functions.decimals().call())
        except RPCError:raise
        except Exception as exc:raise ValueError('token decimals unavailable') from exc
        return out

# Conservative reservation, higher than all allowed standard RPC method costs.
# https://www.alchemy.com/docs/reference/compute-unit-costs (checked 2026-09-22)
RESERVED_CU_PER_ATTEMPT = 100
RESERVED_USD_PER_MILLION_CU = 1


class CampaignRPCProvider(ResilientRPCProvider):
    """Remember a successful chain check for <=60s, separately per endpoint.

    Block anchors and RPC payload validation are never cached by this layer.
    An expired/failed/wrong chain check cannot fall back to stale identity.
    """
    def __init__(self,*args,chain_ttl=60,clock=time.monotonic,**kwargs):
        super().__init__(*args,**kwargs)
        if not 0<chain_ttl<=60:raise ValueError('invalid_chain_check_ttl')
        self.chain_ttl,self.chain_clock=chain_ttl,clock
        self.chain_checks={};self.chain_cache_hits=0

    def _request(self,index,method,params):
        if method!='eth_chainId' or params:return super()._request(index,method,params)
        checked=self.chain_checks.get(index)
        if checked is not None and 0<=self.chain_clock()-checked<self.chain_ttl:
            self.chain_cache_hits+=1
            request_id=json.loads(self.encode_rpc_request(method,params))['id']
            return dict(jsonrpc='2.0',id=request_id,result=hex(self.chain_id))
        self.chain_checks.pop(index,None)
        try:
            result=super()._request(index,method,params)
            if int(result['result'],16)!=self.chain_id:
                raise self._error(method,'wrong_chain',1,index)
        except RPCError:
            self.verified.discard(index)
            raise
        self.chain_checks[index]=self.chain_clock()
        return result


def require_alchemy_arc(url):
    p=urlsplit(url)
    if (p.scheme!='https' or p.hostname!='arc-mainnet.g.alchemy.com'
            or p.username or p.password or p.port not in (None,443)
            or p.query or p.fragment or not re.fullmatch(r'/v2/[A-Za-z0-9_-]+',p.path)):
        raise ValueError('configured_alchemy_arc_endpoint_required')


class AlchemyCampaignSession(requests.Session):
    """No redirects, batches, arbitrary endpoints or writes; every attempt reserved."""
    def __init__(self,budget,url):
        super().__init__();require_alchemy_arc(url)
        self.budget,self.endpoint=budget,url

    def request(self,method,url,**kwargs):
        if method.upper()!='POST' or url!=self.endpoint:raise ValueError('campaign_endpoint_forbidden')
        payload=json.loads(kwargs['data'])
        if not isinstance(payload,dict) or payload.get('method') not in READ_METHODS:
            raise ValueError('campaign_method_forbidden')
        name=payload['method'];sequence=self.budget.take_paid(name)
        event=dict(service='alchemy',wallet=self.budget.wallet,
            sequence=sequence,state='attempt',
            scope=dict(method=name,params=payload.get('params')))
        self.budget.record(event)
        kwargs['allow_redirects']=False
        kwargs['timeout']=min(10,max(.1,self.budget.deadline-time.monotonic()))
        try:
            response=super().request(method,url,**kwargs)
            event.update(state='response',status=response.status_code)
            if response.status_code==200:
                try:event['response']=response.json()
                except ValueError:event['category']='invalid_json'
            self.budget.record(event)
            return response
        except requests.RequestException as error:
            event.update(state='failure',category=type(error).__name__)
            self.budget.record(event)
            # Provider catches request failures and only exposes safe categories.
            raise
