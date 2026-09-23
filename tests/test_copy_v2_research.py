import json
from pathlib import Path
from copy import deepcopy
import pytest
from copy_v2_research import reviewed_pool
from copy_research_replay import RawUnitsRPC
from dex_registry import TrustedDexRegistry
from decoder import classify_trade


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/copy_v2_arc.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('field',[None,'factory','token0','token1','pool','registered_pool','predicted_pool','init_code_hash','code','runtime_code_hash','factory_code','factory_code_hash','status','block','missing'])
def test_v2_membership_create2_and_both_codes_required(field):
    p=fixture()['rows'][0]['proof'];block=p['block']
    if field=='missing':p.pop('registered_pool')
    elif field=='block':p['block']+=1
    elif field:p[field]='0x'+'1'*40
    assert reviewed_pool(p,block) is (field is None)


@pytest.mark.parametrize('index,side',[(0,'BUY'),(1,'SELL')])
def test_offline_swap_confirmed_only_at_reviewed_blocks(tmp_path,monkeypatch,index,side):
    import requests,socket
    def deny(*a,**k):pytest.fail('network during offline V2 replay')
    monkeypatch.setattr(requests.Session,'request',deny);monkeypatch.setattr(socket.socket,'connect',deny)
    f=fixture()['rows'][index];p=f['proof'];before=deepcopy(f)
    path=tmp_path/'trusted.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    registry=TrustedDexRegistry(path,5042);original=path.read_bytes()
    registry.entries[p['pool']]=p['runtime_code_hash'];block=p['block'];code=bytes.fromhex(p['code'][2:])
    rpc=RawUnitsRPC(registry,{}, {(p['pool'],block):{code},(p['pool'],block+1):{code}})
    rpc.v2_proofs={(p['pool'],block):p}
    receipt=dict(f['receipt'],status=1,blockNumber=block)
    trades=classify_trade(rpc,receipt,f['wallet'],1e-18,native_value_wei=0,strict=True)
    assert len(trades)==1 and trades[0]['side']==side and trades[0]['confirmation_eligible']
    assert not rpc.swap_emitter_trusted(p['pool'],block+1)
    p['factory']='0x'+'1'*40
    assert not rpc.swap_emitter_trusted(p['pool'],block)
    assert path.read_bytes()==original
    p['factory']=before['proof']['factory'];assert f==before


def test_no_registry_grant_without_evidence(tmp_path):
    p=fixture()['rows'][0]['proof'];path=tmp_path/'trusted.json'
    path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    rpc=RawUnitsRPC(TrustedDexRegistry(path,5042),{}, {})
    assert not rpc.swap_emitter_trusted(p['pool'],p['block'])
