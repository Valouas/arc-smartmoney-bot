import json
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import pytest
from web3 import Web3
from eth_abi import encode

from copy_route_research import prove_route,recover_key,KEY_TYPES,MANAGER,V3,V4,CASH
from copy_exact_code_research import FACTORY,INIT_HASH
from copy_ambiguity_audit import OfflineRPC
from dex_registry import TrustedDexRegistry
from test_audit_regressions import transfer

W='0x'+'7'*40
I='0x'+'1'*40
T='0x'+'b'*40
OTHER='0x'+'9'*40
HOOK='0x'+'0'*40


def fixture(tmp_path,sell=False):
    salt=Web3.keccak(encode(['address','address','uint24'],[I,CASH,3000]))
    pool=Web3.to_hex(Web3.keccak(b'\xff'+bytes.fromhex(FACTORY[2:])+salt+INIT_HASH)[12:])
    code=b'canonical fixture';hash_=Web3.to_hex(Web3.keccak(code))
    p=dict(pool=pool,factory=FACTORY,token0=I,token1=CASH,fee=3000,code='0x'+code.hex(),
           runtime_code_hash=hash_,status='FACTORY_MEMBERSHIP_OBSERVED')
    key=(I,T,10000,200,HOOK);encoded=encode(KEY_TYPES,key);pid=Web3.to_hex(Web3.keccak(encoded))
    addr_topic='0x'+W[2:].zfill(64);sign=-1 if sell else 1
    logs=[transfer(CASH,pool if sell else W,W if sell else pool,10_000_000,True),
          transfer(I,MANAGER if sell else pool,pool if sell else MANAGER,10,True),
          transfer(T,W if sell else MANAGER,MANAGER if sell else W,100,True),
          dict(address=pool,topics=['0x'+V3,addr_topic,addr_topic],data='0x'+encode(['int256','int256','uint160','uint128','int24'],[-10*sign,10_000_000*sign,1,1,0]).hex()),
          dict(address=MANAGER,topics=['0x'+V4,pid,addr_topic],data='0x'+encode(['int128','int128','uint160','uint128','int24','uint24'],[-10*sign,100*sign,1,1,0,10000]).hex())]
    receipt=dict(status=1,blockNumber=42,logs=logs)
    tx=dict(value='0x0',input='0x12345678'+encoded.hex(),**{'from':W})
    path=tmp_path/'registry.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    registry=TrustedDexRegistry(path,5042);registry.entries={pool:hash_,MANAGER:hash_}
    rpc=OfflineRPC(registry,{}, {(pool,42):{code},(MANAGER,42):{code}})
    trade=dict(token=T,side='SELL' if sell else 'BUY')
    return tx,receipt,trade,rpc,{pool:p}


@pytest.mark.parametrize('sell',[False,True])
def test_exact_route_in_both_directions(tmp_path,sell):
    tx,r,t,rpc,p=fixture(tmp_path,sell)
    before=deepcopy(r)
    result=prove_route(tx,r,W,t,rpc,p)
    assert result and result['engine_authority'] is False and len(result['pools'])==2
    assert r==before


@pytest.mark.parametrize('case',['missing_code','wrong_code','wrong_block','unknown_factory','wrong_pool_id',
    'wrong_calldata_key','wrong_sign','disconnected','partial_amount','extra_swap','native_value',
    'wrong_sender','failed_receipt','malformed_swap','malformed_integer','conflicting_cash','extra_pool_currency'])
def test_incomplete_or_unreconciled_route_stays_closed(tmp_path,case):
    tx,r,t,rpc,p=fixture(tmp_path);pool=next(iter(p))
    if case=='missing_code':rpc.codes.pop((pool,42))
    elif case=='wrong_code':rpc.codes[(MANAGER,42)]={b'changed'}
    elif case=='wrong_block':r['blockNumber']=43
    elif case=='unknown_factory':p[pool]['factory']=OTHER
    elif case=='wrong_pool_id':r['logs'][-1]['topics'][1]='0x'+'f'*64
    elif case=='wrong_calldata_key':tx['input']='0x12345678'+encode(KEY_TYPES,[I,T,10001,200,HOOK]).hex()
    elif case=='wrong_sign':r['logs'][-1]['data']='0x'+encode(['int128','int128','uint160','uint128','int24','uint24'],[10,-100,1,1,0,10000]).hex()
    elif case=='disconnected':r['logs'][2]=transfer(T,MANAGER,OTHER,100,True)
    elif case=='partial_amount':r['logs'][1]=transfer(I,pool,MANAGER,9,True)
    elif case=='extra_swap':r['logs'].append(deepcopy(r['logs'][-1]))
    elif case=='native_value':tx['value']='0x1'
    elif case=='wrong_sender':tx['from']=OTHER
    elif case=='failed_receipt':r['status']=0
    elif case=='malformed_swap':r['logs'][-1]['data']='0x01'
    elif case=='malformed_integer':r['logs'][-1]['data']='0x'+('01'*16)+r['logs'][-1]['data'][34:]
    elif case=='conflicting_cash':
        from copy_route_research import MIRROR
        r['logs'].append(transfer(MIRROR,W,pool,123,True))
    elif case=='extra_pool_currency':r['logs'].append(transfer(OTHER,W,MANAGER,1,True))
    assert prove_route(tx,r,W,t,rpc,p) is None


def test_matching_arc_cash_mirror_not_double_counted(tmp_path):
    from copy_route_research import MIRROR
    tx,r,t,rpc,p=fixture(tmp_path);pool=next(iter(p))
    r['logs'].append(transfer(MIRROR,W,pool,10_000_000*10**12,True))
    assert prove_route(tx,r,W,t,rpc,p)


def test_pool_key_recovery_is_hash_matched_and_bounded():
    key=(I,T,10000,200,HOOK);encoded=encode(KEY_TYPES,key);pid=Web3.to_hex(Web3.keccak(encoded))
    assert recover_key('0x12345678'+encoded.hex(),{I,T},pid)==key
    assert recover_key('0x12345678'+encoded.hex(),{I,T},'0x'+'f'*64) is None
    assert recover_key('0x'+'00'*65537,{I,T},pid) is None


def test_route_replay_can_resolve_holding_without_network_or_source_mutation(tmp_path,monkeypatch):
    import socket,requests
    from copy_research_replay import replay_wallet
    def denied(*a,**kw):pytest.fail('offline replay attempted network')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(requests.Session,'request',denied)
    records=[];raw={};rpc=None;pools=None
    for i,sell in enumerate((False,True)):
        t,r,_,current,poolmap=fixture(tmp_path,sell)
        h='0x'+f'{i+1:064x}';bh='0x'+'a'*64;block=42+i
        t.update(hash=h,blockHash=bh,blockNumber=hex(block))
        r.update(transactionHash=h,blockHash=bh,blockNumber=hex(block),transactionIndex='0x0',status='0x1')
        records.append(dict(tx_hash=h,block=block,index=0,timestamp=100+300*i,error='unverified',trades=[]))
        raw[h]=(t,r)
        if rpc is None:rpc=current;pools=poolmap
        if i:
            for (address,_),code in current.codes.items():rpc.codes[(address,block)]=code
    rpc.pool_proofs=pools;rpc.token_meta=lambda a:dict(decimals=0,symbol='RAW')
    before=deepcopy(records)
    result=replay_wallet(W,dict(version=1,phase='COMPLETE',cutoff_block=43,cutoff_at=500),records,raw,rpc,{})
    assert result['holding']['holding_duration_eligible']
    assert result['holding']['median_first_sale_seconds']==300
    assert [c['reason'] for c in result['changes']]==['RECONCILED_V3_V4_ROUTE']*2
    assert records==before


def test_preserved_arc_buy_and_sell_fixture():
    f=json.loads((Path(__file__).parent/'fixtures/copy_route_v3_v4_arc.json').read_text())
    p=f['pool_proof'];pool=p['pool']
    # Exact-code failure modes are covered above; this fixture freezes the real
    # observed transfer/event quantities and calldata, with a canonical V3 proof.
    blocks={x['receipt']['blockNumber'] for x in f['cases']}
    rpc=SimpleNamespace(registry=SimpleNamespace(entries={pool:p['runtime_code_hash']}),
                        swap_emitter_trusted=lambda a,b:a in {pool,MANAGER} and b in blocks)
    for case in f['cases']:
        result=prove_route(case['tx'],case['receipt'],f['wallet'],dict(token=f['token'],side=case['side']),rpc,{pool:p})
        assert result and result['method']=='EXACT_RECONCILED_V3_V4_ROUTE'
