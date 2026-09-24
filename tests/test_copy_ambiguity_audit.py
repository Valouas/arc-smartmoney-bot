import json
import sqlite3
from types import SimpleNamespace

import pytest
from web3 import Web3

from copy_ambiguity_audit import OfflineRPC,diagnose,audit,cached,load_ledger,readonly,write_report
from dex_registry import TrustedDexRegistry
from rpc_cache import RPCCache
from test_audit_regressions import W,T,POOL,H,transfer
from rpc import USDC_ERC20

B='0x'+'b'*64


def raw(logs):
    tx=dict(hash=H,blockHash=B,blockNumber='0x1',value='0x0',**{'from':W})
    receipt=dict(transactionHash=H,blockHash=B,blockNumber='0x1',transactionIndex='0x0',status='0x1',logs=logs)
    return tx,receipt


def rpc(tmp_path,reviewed=False,codes=None,decimals=None):
    path=tmp_path/'dex.json'
    entries=[dict(address=POOL,runtime_code_hash=Web3.to_hex(Web3.keccak(b'code')))] if reviewed else []
    path.write_text(json.dumps(dict(chain_id=5042,emitters=entries)))
    return OfflineRPC(TrustedDexRegistry(path,5042),{T:{18}} if decimals is None else decimals,{} if codes is None else codes)


def swap():
    return [transfer(USDC_ERC20,W,POOL,10**6,True),transfer(T,POOL,W,10**18,True),
        dict(address=POOL,topics=[Web3.to_hex(Web3.keccak(text='Swap(address,uint256,uint256,uint256,uint256,address)'))],data='0x')]


@pytest.mark.parametrize('disposed,covered,cause',[
    (set(),True,'INCOMING_ONLY_NO_OBSERVED_DISPOSAL'),
    ({T},True,'INCOMING_TOKEN_WITHOUT_CONFIRMED_BUY'),
    (set(),False,'INCOMING_TOKEN_WITHOUT_CONFIRMED_BUY')])
def test_incoming_credit_never_invented_as_buy(tmp_path,disposed,covered,cause):
    tx,r=raw([transfer(T,POOL,W,10**18,True)])
    d=diagnose(W,dict(block=1,index=0,inventory_gap=True),tx,r,rpc(tmp_path),disposed,covered)
    assert d['cause']==cause and 'diagnostic_trades' not in d


def test_unreviewed_swap_emitter_never_trusted(tmp_path):
    tx,r=raw(swap())
    d=diagnose(W,dict(block=1,index=0,error='unsupported'),tx,r,rpc(tmp_path),set(),True)
    assert d['cause']=='NO_REVIEWED_SWAP_EMITTER'
    assert d['diagnostic_trades'][0]['confirmation_eligible'] is False


@pytest.mark.parametrize('codes,cause',[
    ({},'MISSING_STORED_HISTORICAL_CODE'),
    ({(POOL,2):{b'code'}},'MISSING_STORED_HISTORICAL_CODE'),
    ({(POOL,1):{b'wrong'}},'HISTORICAL_EMITTER_CODE_MISMATCH'),
    ({(POOL,1):{b'code'}},'LOCAL_REPLAY_DIFFERS_REQUIRES_REVIEW')])
def test_exact_historical_code_required_and_replay_only_diagnostic(tmp_path,codes,cause):
    tx,r=raw(swap())
    assert diagnose(W,dict(block=1,index=0,error='unsupported'),tx,r,rpc(tmp_path,True,codes),set(),True)['cause']==cause


@pytest.mark.parametrize('decimals,cause',[({},'MISSING_STORED_DECIMALS'),({T:{6,18}},'CONFLICTING_STORED_DECIMALS')])
def test_missing_or_conflicting_metadata_not_invented(tmp_path,decimals,cause):
    tx,r=raw(swap())
    assert diagnose(W,dict(block=1,index=0,error='unsupported'),tx,r,rpc(tmp_path,decimals=decimals),set(),True)['cause']==cause


def test_corrupt_cached_payload_not_used(tmp_path):
    p=tmp_path/'cache.sqlite3';RPCCache(p)
    with sqlite3.connect(p) as db:
        db.execute('INSERT INTO rpc_cache_entries VALUES(?,?,?,?,?)',(5042,'eth_getTransactionByHash',json.dumps([H]),json.dumps(raw([])[0]),'wrong'))
    with readonly(p) as db:assert cached([db],'eth_getTransactionByHash',[H]) is None


def test_full_report_no_network_no_source_mutation_no_authority(tmp_path,monkeypatch):
    import socket,requests
    def forbidden(*a,**kw):pytest.fail('Network forbidden in offline audit')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(requests.Session,'request',forbidden)
    p=tmp_path/'campaign';p.mkdir();dbpath=p/'campaign.sqlite3';cachepath=p/'cache.sqlite3'
    payload=dict(tx_hash=H,block=1,index=0,inventory_gap=True,trades=[])
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE historical_transactions(wallet,tx_hash,payload)')
        db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(W,H,json.dumps(payload)))
    c=RPCCache(cachepath);tx,r=raw([transfer(T,POOL,W,100,True)])
    c.put(5042,'eth_getTransactionByHash',[H],tx,100)
    c.put(5042,'eth_getTransactionReceipt',[H],r,100)
    registry=tmp_path/'empty.json';registry.write_text('{"chain_id":5042,"emitters":[]}')
    before={f:f.read_bytes() for f in (dbpath,cachepath,registry)}
    result=audit(p,[W],[cachepath],[],registry)
    assert result['network_calls']==0 and result['issues']==1
    assert not result['rows'][0]['holding_eligible'] and not result['rows'][0]['paper_selected']
    assert all(f.read_bytes()==data for f,data in before.items())
    write_report(result,tmp_path/'output')
    with pytest.raises(FileExistsError):write_report(result,tmp_path/'output')


def test_latest_code_and_failed_jsonrpc_do_not_supply_proof(tmp_path):
    log=tmp_path/'requests.jsonl'
    rows=[dict(state='response',status=200,scope=dict(method='eth_getCode',params=[POOL,'latest']),response=dict(result='0x636f6465')),
          dict(state='response',status=200,scope=dict(method='eth_call',params=[dict(to=T,data='0x313ce567')]),response=dict(error={'message':'SECRET'},result='0x'+f'{18:064x}'))]
    log.write_text('\n'.join(json.dumps(r) for r in rows))
    decimals,codes,proof=load_ledger([log],{T,POOL})
    assert not decimals and not codes and not proof
