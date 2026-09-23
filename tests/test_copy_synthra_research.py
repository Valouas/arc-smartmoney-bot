import json
from copy import deepcopy
from pathlib import Path
import pytest
from copy_synthra_research import reviewed_pool
from copy_research_replay import RawUnitsRPC
from decoder import classify_trade
from dex_registry import TrustedDexRegistry


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/copy_synthra_arc.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('case',[None,'factory','reported_factory','registered_pool','token0','fee',
    'init_code_hash','predicted_pool','pool_code','pool_code_hash','block','status','missing','short_address'])
def test_exact_block_membership_and_official_create2_required(case):
    p=fixture()['rows'][0]['proof'];block=p['block']
    if case=='missing':p.pop('registered_pool')
    elif case=='block':block+=1
    elif case=='fee':p['fee']=3000
    elif case=='status':p['status']='UNREVIEWED'
    elif case=='short_address':p['token0']='0x1'
    elif case:p[case]='0x'+'1'*40
    assert reviewed_pool(p,block) is (case is None)


@pytest.mark.parametrize('index,side',[(0,'BUY'),(1,'SELL'),(2,'SELL')])
def test_stored_swaps_confirm_offline_without_changing_production(tmp_path,monkeypatch,index,side):
    import socket,requests
    def deny(*a,**kw):pytest.fail('network during local replay')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(requests.Session,'request',deny)
    f=fixture()['rows'][index];p=f['proof'];original=deepcopy(f)
    path=tmp_path/'trusted.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    before=path.read_bytes();registry=TrustedDexRegistry(path,5042)
    registry.entries[p['pool']]=p['pool_code_hash']
    code=bytes.fromhex(p['pool_code'][2:]);block=p['block']
    rpc=RawUnitsRPC(registry,{}, {(p['pool'],block):{code},(p['pool'],block+1):{code}})
    rpc.synthra_proofs={(p['pool'],block):p}
    receipt=dict(f['receipt'],status=1,blockNumber=block)
    trades=classify_trade(rpc,receipt,p['wallet'],1e-18,native_value_wei=0,strict=True)
    assert len(trades)==1 and trades[0]['side']==side and trades[0]['confirmation_eligible']
    assert not rpc.swap_emitter_trusted(p['pool'],block+1)
    assert path.read_bytes()==before and f==original
    p['reported_factory']='0x'+'1'*40
    assert not rpc.swap_emitter_trusted(p['pool'],block)


def test_no_proof_cannot_self_approve_a_pool(tmp_path):
    path=tmp_path/'trusted.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    rpc=RawUnitsRPC(TrustedDexRegistry(path,5042),{}, {})
    p=fixture()['rows'][0]['proof']
    assert not rpc.swap_emitter_trusted(p['pool'],p['block'])
