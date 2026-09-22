from copy import deepcopy
from types import SimpleNamespace
import json
import hashlib
import sqlite3
import pytest
from web3 import Web3

import copy_research_replay as module
from copy_research_replay import replay_wallet,RawUnitsRPC,receipt_info,NONASSET
from dex_registry import TrustedDexRegistry
from test_copy_holding_duration import tx,W,T
from test_audit_regressions import transfer,POOL
from rpc import USDC_ERC20

G='0x'+'4'*40
B='0x'+'b'*64
SWAP=Web3.to_hex(Web3.keccak(text='Swap(address,uint256,uint256,uint256,uint256,address)'))


def raw(p,logs,sender=POOL):
    logs=[dict(l,transactionHash=p['tx_hash'],blockHash=B,blockNumber=hex(p['block']),
               transactionIndex='0x0',logIndex=hex(i),removed=False) for i,l in enumerate(logs)]
    t=dict(hash=p['tx_hash'],blockHash=B,blockNumber=hex(p['block']),value='0x0',**{'from':sender})
    r=dict(transactionHash=p['tx_hash'],blockHash=B,blockNumber=hex(p['block']),transactionIndex='0x0',status='0x1',logs=logs)
    return t,r


def setup(tmp_path):
    rows=[tx(1,100,quantity=10),tx(2,300,'SELL',10),dict(tx_hash='0x'+f'{3:064x}',block=3,index=0,timestamp=400,trades=[],inventory_gap=True)]
    raws={rows[0]['tx_hash']:raw(rows[0],[transfer(T,POOL,W,10,True)]),
          rows[1]['tx_hash']:raw(rows[1],[transfer(T,W,POOL,10,True)]),
          rows[2]['tx_hash']:raw(rows[2],[transfer(G,POOL,W,5,True)])}
    path=tmp_path/'registry.json';path.write_text(json.dumps(dict(chain_id=5042,emitters=[])))
    rpc=RawUnitsRPC(TrustedDexRegistry(path,5042),{}, {})
    run=dict(version=1,phase='COMPLETE',error=None,cursor=None,cutoff_block=3,cutoff_at=500)
    return rows,raws,rpc,run


def test_isolated_credit_preserved_with_full_explanation(tmp_path):
    rows,raws,rpc,run=setup(tmp_path);original=deepcopy(rows)
    r=replay_wallet(W,run,rows,raws,rpc,{})
    assert rows==original and r['holding']['holding_duration_eligible']
    assert r['holding']['median_first_sale_seconds']==200
    assert r['changes'][0]['reason']=='ISOLATED_NONTRADING_CREDIT'
    assert r['changes'][0]['before']['inventory_gap'] is True
    assert r['records'][2]['research_inventory_evidence']['raw_deltas']=={G:'5'}


@pytest.mark.parametrize('case',['self_sender','native_value','cash_transfer','swap_log','later_outflow','traded_token','missing_raw','incomplete','malformed'])
def test_credit_exception_is_narrow_and_fail_closed(tmp_path,case):
    rows,raws,rpc,run=setup(tmp_path);h=rows[2]['tx_hash'];t,r=raws[h]
    if case=='self_sender':t['from']=W
    elif case=='native_value':t['value']='0x1'
    elif case=='cash_transfer':r['logs'].append(transfer(USDC_ERC20,POOL,W,1,True))
    elif case=='swap_log':r['logs'].append(dict(address=POOL,topics=[SWAP],data='0x'))
    elif case=='later_outflow':
        p=dict(tx_hash='0x'+f'{4:064x}',block=4,index=0,timestamp=450,trades=[],inventory_gap=True)
        rows.append(p);raws[p['tx_hash']]=raw(p,[transfer(G,W,POOL,5,True)]);run['cutoff_block']=4
    elif case=='traded_token':r['logs']=[transfer(T,POOL,W,5,True)]
    elif case=='missing_raw':raws.pop(rows[0]['tx_hash'])
    elif case=='incomplete':run['phase']='DECODING'
    elif case=='malformed':r['logs'][0]['data']='0x01'
    result=replay_wallet(W,run,rows,raws,rpc,{})
    assert result['holding']['holding_duration_status']=='UNKNOWN_HOLDING_DURATION'
    assert result['holding']['median_first_sale_seconds'] is None


def test_raw_integer_quantity_never_rounded_into_false_inventory(tmp_path):
    rows,raws,rpc,run=setup(tmp_path);n=10**30+123
    raws[rows[0]['tx_hash']][1]['logs']=[transfer(T,POOL,W,n,True)]
    raws[rows[1]['tx_hash']][1]['logs']=[transfer(T,W,POOL,n,True)]
    r=replay_wallet(W,run,rows,raws,rpc,{})
    assert r['records'][0]['trades'][0]['token_amount']==str(n)
    assert r['holding']['holding_duration_eligible']


def test_large_raw_fifo_does_not_invent_a_dust_lot(tmp_path):
    _,_,rpc,run=setup(tmp_path);n=10**60+1
    rows=[tx(1,100,quantity=n),tx(2,300,'SELL',n),tx(3,400,quantity=1),tx(4,600,'SELL',1)]
    raws={p['tx_hash']:raw(p,[transfer(T,POOL if i%2==0 else W,W if i%2==0 else POOL,n if i<2 else 1,True)])
          for i,p in enumerate(rows)}
    run.update(cutoff_block=4,cutoff_at=700)
    result=replay_wallet(W,run,rows,raws,rpc,{})
    assert result['holding']['holding_duration_eligible']
    assert result['holding']['holding_sample_size']==2
    assert result['holding']['holding_unsold_buy_count']==0


def test_reviewed_nonasset_requires_exact_code_at_exact_block(tmp_path,monkeypatch):
    rows,raws,rpc,run=setup(tmp_path)
    raws[rows[2]['tx_hash']][1]['logs']=[transfer(NONASSET,W,POOL,5,True)]
    code=bytes.fromhex('600000');monkeypatch.setattr(module,'NONASSET_HASH',Web3.to_hex(Web3.keccak(code)))
    for codes in [{},{(NONASSET,2):{code}},{(NONASSET,3):{b'wrong'}}]:
        assert not replay_wallet(W,run,rows,raws,rpc,codes)['holding']['holding_duration_eligible']
    result=replay_wallet(W,run,rows,raws,rpc,{(NONASSET,3):{code}})
    assert result['holding']['holding_duration_eligible']
    assert result['changes'][0]['reason']=='REVIEWED_NON_STATE_CHANGING_LOG_SOURCE'


def test_unreviewed_swap_stays_unconfirmed(tmp_path):
    rows,raws,rpc,run=setup(tmp_path)
    rows[0].update(error='unverified',trades=[])
    raws[rows[0]['tx_hash']][1]['logs']=[transfer(USDC_ERC20,W,POOL,1000000,True),transfer(T,POOL,W,10,True),dict(address=POOL,topics=[SWAP],data='0x')]
    result=replay_wallet(W,run,rows,raws,rpc,{})
    assert result['records'][0]['error']=='unverified'
    assert result['holding']['holding_duration_status']=='UNKNOWN_HOLDING_DURATION'


def test_error_requires_reviewed_matching_exact_block_code(tmp_path):
    rows,raws,rpc,run=setup(tmp_path);rows[0].update(error='unverified',trades=[])
    raws[rows[0]['tx_hash']][1]['logs']=[transfer(USDC_ERC20,W,POOL,1000000,True),transfer(T,POOL,W,10,True),dict(address=POOL,topics=[SWAP],data='0x')]
    code=b'known code';rpc.registry.entries[POOL]=Web3.to_hex(Web3.keccak(code))
    for codes in [{(POOL,2):{code}},{(POOL,1):{b'wrong'}}]:
        rpc.codes=codes
        assert not replay_wallet(W,run,rows,raws,rpc,{})['holding']['holding_duration_eligible']
    rpc.codes={(POOL,1):{code}}
    r=replay_wallet(W,run,rows,raws,rpc,{})
    assert r['holding']['holding_duration_eligible']
    assert rows[0]['error']=='unverified' and r['records'][0].get('error') is None


def test_no_network_source_writes_or_authority_in_full_report(tmp_path,monkeypatch):
    import socket,requests
    from rpc_cache import RPCCache
    rows,raws,rpc,header=setup(tmp_path)
    def deny(*a,**kw):pytest.fail('Network forbidden')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(requests.Session,'request',deny)
    campaign=tmp_path/'campaign';campaign.mkdir();source=campaign/'campaign.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE historical_runs(wallet,version,phase,error,cursor,cutoff_block,cutoff_at)')
        db.execute('INSERT INTO historical_runs VALUES(?,?,?,?,?,?,?)',(W,1,'COMPLETE',None,None,3,500))
        db.execute('CREATE TABLE historical_transactions(wallet,tx_hash,payload)')
        db.executemany('INSERT INTO historical_transactions VALUES(?,?,?)',[(W,p['tx_hash'],json.dumps(p)) for p in rows])
    cache=tmp_path/'cache.sqlite3';RPCCache(cache)
    with sqlite3.connect(cache) as db:
        for h,(t,r) in raws.items():
            for method,value in [('eth_getTransactionByHash',t),('eth_getTransactionReceipt',r)]:
                text=json.dumps(value);key=json.dumps([h],separators=(',',':'))
                db.execute('INSERT INTO rpc_cache_entries VALUES(?,?,?,?,?)',(5042,method,key,text,hashlib.sha256(text.encode()).hexdigest()))
    pool=tmp_path/'pools';pool.mkdir();(pool/'manifest.json').write_text('{}')
    exact=tmp_path/'exact';(exact/'runs/1').mkdir(parents=True);(exact/'runs/1/results_manifest.json').write_text('{}')
    scope=tmp_path/'scope.json';scope.write_text(json.dumps(dict(wallets=[W])))
    before=source.read_bytes();before_cache=cache.read_bytes()
    result=module.run(campaign,scope,pool,exact,tmp_path/'out',[cache],[],tmp_path/'registry.json')
    assert result['network_calls']==0 and result['source_modified'] is False
    assert result['rows'][0]['paper_selected'] is False and result['rows'][0]['live_authority'] is False
    assert source.read_bytes()==before and cache.read_bytes()==before_cache
    assert result['passed']==1
