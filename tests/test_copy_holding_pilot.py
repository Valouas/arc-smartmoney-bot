from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from hexbytes import HexBytes
import pytest
import requests

from copy_holding_pilot import Budget, BoundedSession, PilotLimit, prepare, process_wallet, RPC_URL, save_new
from historical_collector import HistoricalCollector
from historical_store import SCHEMA
from copy_holding_duration import stored_holding
from test_copy_holding_duration import tx,W


def test_attempt_cap_includes_errors_and_forbids_other_endpoints(tmp_path,monkeypatch):
    budget=Budget(tmp_path,rpc=1);s=BoundedSession(budget,'rpc')
    monkeypatch.setattr('copy_holding_pilot.time.sleep',lambda _:None)
    def timeout(*args,**kwargs):raise requests.Timeout()
    monkeypatch.setattr(requests.Session,'request',timeout)
    payload=json.dumps(dict(method='eth_chainId',params=[]))
    with pytest.raises(ValueError):s.post('https://api.nansen.ai',data=payload)
    assert not budget.counts
    with pytest.raises(requests.Timeout):s.post(RPC_URL,data=payload)
    with pytest.raises(PilotLimit):s.post(RPC_URL,data=payload)
    assert budget.counts['rpc']==1
    entries=[json.loads(l) for l in (tmp_path/'requests.jsonl').read_text().splitlines()]
    assert entries[-1]['category']=='Timeout'


def test_no_signing_or_broadcast_or_expired_budget(tmp_path):
    b=Budget(tmp_path);s=BoundedSession(b,'rpc')
    with pytest.raises(ValueError):BoundedSession(b,'rpc','https://arc-mainnet.g.alchemy.com/v2/secret')
    with pytest.raises(ValueError):s.post(RPC_URL,data=json.dumps({'method':'eth_sendRawTransaction'}))
    b.deadline=0
    with pytest.raises(PilotLimit):b.take('rpc','eth_chainId')


def test_arcscan_scope_strict_and_response_preserved(tmp_path,monkeypatch):
    b=Budget(tmp_path);b.wallet=W;s=BoundedSession(b,'arcscan')
    response=requests.Response();response.status_code=200;response._content=b'{"items":[]}'
    monkeypatch.setattr(requests.Session,'request',lambda *a,**k:response)
    monkeypatch.setattr('copy_holding_pilot.time.sleep',lambda _:None)
    with pytest.raises(ValueError):s.get('https://api.arc-scan.org/v1/address/other/activity')
    s.get('https://api.arc-scan.org/v1/address/'+W+'/activity')
    assert json.loads((tmp_path/'requests.jsonl').read_text().splitlines()[-1])['response']=={'items':[]}


@pytest.fixture
def isolated(tmp_path):
    source=tmp_path/'source.sqlite3';out=tmp_path/'out';out.mkdir()
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase) VALUES(?,1,1,99,10000,?,?)',
                   (W,'0x'+'a'*64,'DECODING'))
        db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,tx(1,100)['tx_hash'],json.dumps(tx(1,100))))
        db.execute('INSERT INTO historical_transactions VALUES(?,?,NULL)',(W,tx(2,220)['tx_hash']))
    before=source.read_bytes()
    db=prepare(source,out,[W])
    yield source,before,db,out
    db.close()


def rpc():
    return SimpleNamespace(get_block=lambda *a:{'hash':HexBytes('0x'+'a'*64)},
        w3=SimpleNamespace(provider=SimpleNamespace(historical_headers=nullcontext)))


def test_existing_good_payload_reused_and_only_missing_decoded(isolated,monkeypatch):
    source,before,db,out=isolated;calls=[]
    def decode(self,wallet,h,run,rpc=None):calls.append(h);return tx(2,220,'SELL')
    monkeypatch.setattr(HistoricalCollector,'transaction',decode)
    result=process_wallet(db,W,rpc(),None)
    assert result=={'decoded_this_pilot':1,'reused_decoded':1}
    assert calls==[tx(2,220)['tx_hash']]
    assert stored_holding(db,W)['holding_duration_eligible']
    assert source.read_bytes()==before
    assert (out/'initial_evidence.json').exists()


def test_limit_during_decode_never_completes_history(isolated,monkeypatch):
    source,before,db,_=isolated
    def stop(*a,**k):raise PilotLimit()
    monkeypatch.setattr(HistoricalCollector,'transaction',stop)
    with pytest.raises(PilotLimit):process_wallet(db,W,rpc(),None)
    assert not stored_holding(db,W)['holding_duration_eligible']
    assert source.read_bytes()==before


def test_anchor_mismatch_refuses_cached_proof(isolated):
    _,_,db,_=isolated
    r=rpc();r.get_block=lambda *a:{'hash':HexBytes('0x'+'b'*64)}
    with pytest.raises(ValueError,match='anchor_mismatch'):process_wallet(db,W,r,None)


def test_outputs_never_overwrite_previous_evidence(tmp_path):
    path=tmp_path/'data.json';save_new(path,{'saved':True})
    with pytest.raises(FileExistsError):save_new(path,{})
    assert json.loads(path.read_text())=={'saved':True}


def test_source_cache_is_readonly_and_bad_digest_not_reused(tmp_path):
    from copy_holding_pilot import PilotCache
    from rpc_cache import RPCCache
    source=tmp_path/'source.sqlite3';cache=RPCCache(source)
    value=dict(number='0x1',timestamp='0x64',hash='0x'+'a'*64,transactions=[])
    cache.put(5042,'eth_getBlockByNumber',['0x1',False],value,99)
    original=source.read_bytes()
    overlay=PilotCache(tmp_path/'overlay.sqlite3',[source])
    assert overlay.get(5042,'eth_getBlockByNumber',['0x1',False],99)==value
    assert overlay.reused==1 and source.read_bytes()==original
    with sqlite3.connect(source) as db:db.execute("UPDATE rpc_cache_entries SET digest='invalid'")
    other=PilotCache(tmp_path/'other.sqlite3',[source])
    assert other.get(5042,'eth_getBlockByNumber',['0x1',False],99) is None
