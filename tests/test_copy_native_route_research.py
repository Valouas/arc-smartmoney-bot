import json
from copy import deepcopy
from pathlib import Path
import pytest
from copy_native_route_research import reviewed_native_payment,TOKEN,WALLET,BLOCK
from copy_route_research import prove_route,MANAGER
from copy_research_replay import RawUnitsRPC
from dex_registry import TrustedDexRegistry

def fixture():
    return json.loads((Path(__file__).parent/'fixtures/copy_native_route_arc.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('bad',[None,'value','from','to','hash','block','input','logs','missing_log','extra_log','fee','removed','receipt_hash','block_hash','token','side'])
def test_native_input_is_exact_receipt_only(bad):
    f=fixture();tx=f['tx'];r=dict(f['receipt'],status=1,blockNumber=BLOCK)
    t=dict(side='BUY',token=TOKEN)
    if bad=='value':tx['value']=hex(89*10**18)
    elif bad in ('from','to','hash'):tx[bad]='0x'+'1'*40
    elif bad=='block':r['blockNumber']+=1
    elif bad=='input':tx['input']+='00'
    elif bad=='logs':r['logs'].reverse()
    elif bad=='missing_log':r['logs'].pop()
    elif bad=='extra_log':r['logs'].append(deepcopy(r['logs'][0]))
    elif bad=='fee':r['logs'][1]['data']='0x'+'0'*64
    elif bad=='removed':r['logs'][0]['removed']=True
    elif bad=='receipt_hash':r['transactionHash']='0x'+'2'*64
    elif bad=='block_hash':r['blockHash']='0x'+'3'*64
    elif bad=='token':t['token']='0x'+'4'*40
    elif bad=='side':t['side']='SELL'
    assert (reviewed_native_payment(tx,r,WALLET,t) is not None) is (bad is None)

@pytest.mark.parametrize('bad',[None,'code','factory','block','no_proof'])
def test_native_payment_still_requires_full_two_pool_proof(tmp_path,monkeypatch,bad):
    import requests,socket
    def denied(*a,**kw):pytest.fail('network during offline research')
    monkeypatch.setattr(requests.Session,'request',denied);monkeypatch.setattr(socket.socket,'connect',denied)
    f=fixture();original=deepcopy(f);p=f['pool_proof'];pool=p['pool']
    path=tmp_path/'trusted.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    before=path.read_bytes();registry=TrustedDexRegistry(path,5042)
    registry.entries[pool]=p['pool_code_hash']
    # Existing reviewed V4 emitter already validated separately in the research
    # replay. This test isolates native+Slipstream proof; V4 failure is covered.
    from web3 import Web3
    manager_code=b'v4 fixture';registry.entries[MANAGER]=Web3.to_hex(Web3.keccak(manager_code))
    codes={(pool,BLOCK):{bytes.fromhex(p['pool_code'][2:])},(MANAGER,BLOCK):{manager_code}}
    rpc=RawUnitsRPC(registry,{},codes);rpc.pool_proofs={pool:p}
    if bad=='code':codes[(pool,BLOCK)]={b'wrong'}
    elif bad=='factory':p['factory']='0x'+'1'*40
    elif bad=='block':p['block']+=1
    elif bad=='no_proof':rpc.pool_proofs={}
    receipt=dict(f['receipt'],status=1,blockNumber=BLOCK)
    result=prove_route(f['tx'],receipt,WALLET,dict(side='BUY',token=TOKEN),rpc,rpc.pool_proofs)
    assert bool(result) is (bad is None)
    if result:
        assert result['native_payment']['gross_usdc_raw']==90_000_000
        assert result['native_payment']['router_fee_usdc_raw']==900_000
        assert len(result['pools'])==2 and result['engine_authority'] is False
        from decoder import classify_trade
        trades=classify_trade(rpc,receipt,WALLET,1e-18,native_value_wei=int(f['tx']['value'],16),strict=True)
        assert len(trades)==1 and trades[0]['side']=='BUY'
        assert trades[0]['usdc_amount']==90  # Gross wallet spend, fee included.
        assert not trades[0]['confirmation_eligible']  # Needs isolated route proof.
        assert f==original
    assert path.read_bytes()==before
