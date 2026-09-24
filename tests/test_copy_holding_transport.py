import json
from unittest.mock import Mock

import pytest
import requests
from web3 import Web3

from copy_holding_transport import CampaignRPCProvider,AlchemyCampaignSession,require_alchemy_arc
from copy_holding_campaign import CampaignBudget,PaidBudgetLimit,enable_paid
from copy_holding_pilot import PilotLimit
from rpc_transport import RPCError
from test_copy_holding_campaign import prepared
from copy_holding_campaign import prepare,connect


class Clock:
    now=100
    def __call__(self):return self.now


class HTTP:
    def __init__(self):self.calls=[];self.chain='0x13b2';self.fail=False
    def post(self,url,**kw):
        request=json.loads(kw['data']);self.calls.append((url,request['method']))
        if self.fail and request['method']=='eth_chainId':raise requests.Timeout('secret_url_key')
        result=self.chain if request['method']=='eth_chainId' else '0x1'
        return Mock(status_code=200,headers={},json=lambda:dict(jsonrpc='2.0',id=request['id'],result=result),close=Mock())


def test_repeated_chain_ids_only_one_wire_check_per_minute():
    h=HTTP();clock=Clock();p=CampaignRPCProvider('https://primary',5042,Mock(),session=h,clock=clock,cache=False)
    w=Web3(p)
    for _ in range(40):assert w.eth.chain_id==5042
    assert len(h.calls)==1 and p.chain_cache_hits==39
    clock.now+=60
    assert w.eth.chain_id==5042 and len(h.calls)==2


def test_expired_failure_never_returns_stale_chain_identity():
    h=HTTP();clock=Clock();p=CampaignRPCProvider('https://primary',5042,Mock(),session=h,clock=clock,cache=False,sleep=lambda _:None)
    assert Web3(p).eth.chain_id==5042
    clock.now+=60;h.fail=True
    with pytest.raises(RPCError,match='timeout'):Web3(p).eth.chain_id
    assert 0 not in p.verified and not p.chain_checks
    assert len(h.calls)==4  # First success, then the bounded three attempts.


def test_changed_chain_and_second_endpoint_never_inherit_cache():
    h=HTTP();clock=Clock();p=CampaignRPCProvider('https://primary',5042,Mock(),fallback_urls=['https://secondary'],session=h,clock=clock,cache=False)
    assert Web3(p).eth.chain_id==5042
    h.chain='0x1'
    with pytest.raises(RPCError,match='wrong_chain'):p._request(1,'eth_chainId',[])
    clock.now+=60
    with pytest.raises(RPCError,match='wrong_chain'):Web3(p).eth.chain_id
    assert not p.chain_checks


def test_other_reads_including_anchors_still_go_to_wire():
    h=HTTP();p=CampaignRPCProvider('https://primary',5042,Mock(),session=h,cache=False)
    w=Web3(p)
    assert w.eth.block_number==1 and w.eth.block_number==1
    assert [m for _,m in h.calls]==['eth_chainId','eth_blockNumber','eth_blockNumber']


@pytest.mark.parametrize('url',[
 'http://arc-mainnet.g.alchemy.com/v2/secret',
 'https://arc-testnet.g.alchemy.com/v2/secret',
 'https://arc-mainnet.g.alchemy.com.evil.test/v2/secret',
 'https://arc-mainnet.g.alchemy.com/v2/secret?redirect=foo',
 'https://user:secret@arc-mainnet.g.alchemy.com/v2/key'])
def test_paid_session_forbids_wrong_host_network_or_redirect_parameters(url):
    with pytest.raises(ValueError,match='configured_alchemy'):require_alchemy_arc(url)


def test_paid_reservation_is_atomic_bounded_and_survives_restart(prepared):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg,rpc_cap=3)
    s=json.loads((out/'settings.json').read_text());s['paid_rpc']={'reserved_cu_cap':200}
    with connect(out/'campaign.sqlite3') as db:
        first=CampaignBudget(out,db,s);first.take_paid('eth_call')
        again=CampaignBudget(out,db,s);again.take_paid('eth_call')
        with pytest.raises(PaidBudgetLimit):again.take_paid('eth_call')
        assert db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget').fetchone()[:]==(2,200)
        assert again.counts['rpc']==2
        again.take('rpc','eth_getBlockByNumber')
        with pytest.raises(PilotLimit):again.take('rpc','eth_getBlockByNumber')
        assert not again.paid_available()


def test_failed_paid_http_attempt_reserved_without_logging_url(prepared,monkeypatch):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    s=json.loads((out/'settings.json').read_text());s['paid_rpc']={'reserved_cu_cap':100}
    def failure(*a,**k):raise requests.Timeout('secret_url_key')
    monkeypatch.setattr(requests.Session,'request',failure)
    with connect(out/'campaign.sqlite3') as db:
        b=CampaignBudget(out,db,s);session=AlchemyCampaignSession(b,'https://arc-mainnet.g.alchemy.com/v2/secret_url_key')
        with pytest.raises(ValueError):session.post(session.endpoint,data=json.dumps(dict(method='eth_sendRawTransaction')))
        assert b.counts['rpc']==0
        with pytest.raises(requests.Timeout):session.post(session.endpoint,data=json.dumps(dict(method='eth_call',params=[])))
        assert not b.paid_available()
        ledger=(out/'requests.jsonl').read_text()
        assert 'secret_url_key' not in ledger and 'https:' not in ledger
        assert 'Timeout' in ledger


def test_enable_paid_archives_settings_and_never_resets_usage(prepared):
    src,scope,out,cfg=prepared;prepare(src,scope,out,cfg)
    cfg.rpc_url='https://arc-mainnet.g.alchemy.com/v2/secret_url_key'
    original=(out/'settings.json').read_bytes()
    enable_paid(out,cfg,5)
    with connect(out/'campaign.sqlite3') as db:
        budget=CampaignBudget(out,db,json.loads((out/'settings.json').read_text()));budget.take_paid('eth_call')
    enable_paid(out,cfg,5)
    with connect(out/'campaign.sqlite3') as db:
        assert db.execute('SELECT attempts FROM campaign_paid_budget').fetchone()[0]==1
    assert original in [p.read_bytes() for p in out.glob('settings_before_paid_*.json')]
    assert all('secret_url_key' not in p.read_text() for p in out.glob('*paid*.json'))
    settings=json.loads((out/'settings.json').read_text())
    assert settings['paid_rpc']['reserved_cu_cap']==5_000_000
    assert settings['created_at']==json.loads(original)['created_at']
