from copy_exact_code_research import canonical_pool,code_diagnostic,ExactBudget,FACTORY,INIT_HASH
from copy_holding_pilot import PilotLimit
from web3 import Web3
from eth_abi import encode
import pytest
import json
import hashlib
import sqlite3
import time
from types import SimpleNamespace
import copy_exact_code_research as module
from rpc_transport import RPCError


def proof():
    a,b='0x'+'1'*40,'0x'+'2'*40
    salt=Web3.keccak(encode(['address','address','uint24'],[a,b,3000]))
    pool=Web3.to_hex(Web3.keccak(b'\xff'+bytes.fromhex(FACTORY[2:])+salt+INIT_HASH)[12:])
    return dict(status='FACTORY_MEMBERSHIP_OBSERVED',factory=FACTORY,token0=a,token1=b,fee=3000,pool=pool,
                code='0x6001',runtime_code_hash=Web3.to_hex(Web3.keccak(bytes.fromhex('6001'))))


def test_create2_and_runtime_hash_required():
    p=proof();assert canonical_pool(p)
    p['pool']='0x'+'3'*40;assert not canonical_pool(p)


@pytest.mark.parametrize('key,value',[
    ('factory','0x'+'3'*40),('code','0x'),('code','0x6002'),('status','DIFFERENT_FACTORY'),('fee',True)])
def test_canonical_failure_is_closed(key,value):
    p=proof();p[key]=value;assert not canonical_pool(p)


def test_opcodes_in_push_data_do_not_mean_instructions():
    r=code_diagnostic('0x605560f1605460f400')
    assert r['status']=='NO_STORAGE_OR_CALL_OPCODES' and r['automatic_exclusion'] is False


@pytest.mark.parametrize('op',['54','55','5c','5d','f0','f1','f2','f4','f5','fa','ff'])
def test_no_state_or_call_instruction_can_be_ignored(op):
    assert code_diagnostic('0x'+op)['status']=='REQUIRES_SEMANTIC_REVIEW'


def test_truncated_push_and_empty_code_are_not_proofs():
    assert code_diagnostic('0x61ff')['status']=='TRUNCATED_PUSH'
    assert code_diagnostic('0x')['status']=='REQUIRES_SEMANTIC_REVIEW'


def test_budget_ceiling_precedes_reservation():
    b=object.__new__(ExactBudget);b.exact_attempts=2500
    with pytest.raises(PilotLimit):b.take_paid('eth_getCode')
    b.exact_attempts=0
    with pytest.raises(PilotLimit):b.take_paid('eth_call')


@pytest.mark.parametrize('failure',[False,True])
def test_resume_preserves_history_and_does_not_repeat_observations(tmp_path,monkeypatch,failure):
    campaign=tmp_path/'campaign';campaign.mkdir()
    wallet='0x'+'1'*40
    with sqlite3.connect(campaign/'campaign.sqlite3') as db:
        db.executescript("CREATE TABLE campaign_budget(service PRIMARY KEY,attempts); INSERT INTO campaign_budget VALUES('rpc',0),('arcscan',0); CREATE TABLE historical_transactions(payload);")
        db.execute('INSERT INTO historical_transactions VALUES(?)',('original evidence',))
    settings=dict(rpc_cap=10,arcscan_cap=0,max_seconds=600,created_at=time.time(),
                  paid_rpc=dict(enabled=True,reserved_cu_cap=1000,wallets=[wallet]))
    (campaign/'settings.json').write_text(json.dumps(settings))
    output=tmp_path/'proofs';output.mkdir();(output/'results').mkdir();(output/'runs').mkdir()
    entry=dict(wallet=wallet,address='0x'+'2'*40,block=123,kind='token',expected_code_hash=None)
    (output/'plan.json').write_text(json.dumps([entry]))
    (output/'policy.json').write_text(json.dumps(dict(campaign=str(campaign),
        plan_sha256=hashlib.sha256((output/'plan.json').read_bytes()).hexdigest())))
    calls=[]
    class Session:
        def __init__(self,budget,url):self.budget=budget
        def close(self):pass
    class Provider:
        def __init__(self,*args,session,**kwargs):self.budget=session.budget
        def make_request(self,method,params):
            calls.append((method,params));self.budget.take_paid(method)
            if failure:raise RPCError(method,'timeout',1,'primary')
            return dict(result='0x600000')
    monkeypatch.setattr(module,'AlchemyCampaignSession',Session)
    monkeypatch.setattr(module,'CampaignRPCProvider',Provider)
    cfg=SimpleNamespace(rpc_url='https://arc-mainnet.g.alchemy.com/v2/test',rpc_rate_limit_file=str(tmp_path/'rate.sqlite3'))
    module.run(output,cfg)
    saved=(output/'results/0000.json').read_bytes()
    module.run(output,cfg)
    assert len(calls)==1 and (output/'results/0000.json').read_bytes()==saved
    with sqlite3.connect(campaign/'campaign.sqlite3') as db:
        assert db.execute('SELECT payload FROM historical_transactions').fetchone()[0]=='original evidence'
        assert db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget').fetchone()==(1,100)
    result=json.loads(saved)
    assert result['engine_authority'] is False and result['paper_selected'] is False
    assert bool(result.get('error')) is failure
