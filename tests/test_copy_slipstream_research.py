import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from copy_slipstream_research import reviewed_pool
from copy_route_research import prove_route, MANAGER
from copy_research_replay import RawUnitsRPC


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/copy_slipstream_arc.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('invalid',[
    None,'factory','implementation','implementation_code','factory_code',
    'pool_code','registered_pool','token0','tick_spacing','block','status','short_address',
])
def test_pinned_factory_clone_and_exact_implementation_required(invalid):
    f=fixture();p=f['proof'];block=p['block']
    if invalid=='block':block+=1
    elif invalid=='tick_spacing':p[invalid]=50
    elif invalid=='status':p[invalid]='UNREVIEWED'
    elif invalid=='short_address':p['token0']='0x1'
    elif invalid and invalid.endswith('_code'):p[invalid]='0x00'
    elif invalid:p[invalid]='0x'+'1'*40
    assert reviewed_pool(p,block) is (invalid is None)


def test_real_route_reconciles_without_network(tmp_path,monkeypatch):
    import socket,requests
    def deny(*a,**kw):pytest.fail('network during local research')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(requests.Session,'request',deny)
    f=fixture();p=f['proof'];r=f['receipt'];before=deepcopy(r)
    rpc=SimpleNamespace(registry=SimpleNamespace(entries={p['pool']:p['pool_code_hash']}),
        swap_emitter_trusted=lambda a,b:a in {p['pool'],MANAGER} and b==p['block'])
    result=prove_route(f['tx'],r,f['wallet'],dict(token=f['token'],side='BUY'),rpc,{p['pool']:p})
    assert result and result['method']=='EXACT_RECONCILED_SLIPSTREAM_ROUTE'
    assert result['engine_authority'] is False and r==before
    # Altered amounts and wrong historical implementation remain fail-closed.
    p['implementation_code']='0x00'
    assert prove_route(f['tx'],r,f['wallet'],dict(token=f['token'],side='BUY'),rpc,{p['pool']:p}) is None


def test_clone_alone_cannot_authorize_a_different_block():
    p=fixture()['proof'];address=p['pool'];block=p['block']
    registry=SimpleNamespace(entries={address:p['pool_code_hash']},trusts=lambda a,c:True)
    rpc=RawUnitsRPC(registry,{}, {(address,block):{bytes.fromhex(p['pool_code'][2:])},
                                 (address,block+1):{bytes.fromhex(p['pool_code'][2:])}})
    rpc.pool_proofs={address:p}
    assert rpc.swap_emitter_trusted(address,block)
    assert not rpc.swap_emitter_trusted(address,block+1)
    p['implementation_code']='0x00'
    assert not rpc.swap_emitter_trusted(address,block)
