import json
import threading
from collections import OrderedDict
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from decoder import classify_trade
from historical_collector import HistoricalCollector
from historical_efficiency import EfficientCollector, PersistentReads, ResearchRPC
from historical_metrics import analyze
from historical_snapshots import decoding_context
from rpc import ArcRPC, USDC_ERC20
from rpc_transport import RPCError
from test_historical_analysis import cfg, store, fake_collector, page, W, NOW
from test_historical_prefetch import factory, HASHES
from test_audit_regressions import transfer, receipt, T, POOL


def test_clients_survive_batches_and_close_exactly_once():
    clients=[]
    reader=PersistentReads(1,factory(clients=clients))
    try:
        for _ in range(3):
            assert len(list(reader.ordered(HASHES,100,lambda h,b,r:b)))==3
        assert len(clients)==1
        clients[0].w3.provider.session.close.assert_not_called()
    finally:reader.close()
    reader.close()
    clients[0].w3.provider.session.close.assert_called_once()
    with pytest.raises(RuntimeError):list(reader.ordered(HASHES,100,lambda *a:None))


def test_parallel_completion_still_yields_in_order():
    barrier=threading.Barrier(3);last=threading.Event();done=[]
    def hook(h):
        barrier.wait(5)
        if h!=HASHES[-1]:assert last.wait(5)
        done.append(h)
        if h==HASHES[-1]:last.set()
    with closing(PersistentReads(3,factory(hook))) as reader:
        assert [h for h,_ in reader.ordered(HASHES,100,lambda h,b,r:b)]==HASHES
    assert done[0]==HASHES[-1]


def test_abandoned_batch_drains_prefetch_without_closing_persistent_clients():
    calls=[];clients=[]
    with closing(PersistentReads(2,factory(calls.append,clients))) as reader:
        with closing(reader.ordered(HASHES*100,100,lambda h,b,r:b)) as rows:
            next(rows)
        assert len(calls)<=2
        assert all(c.w3.provider.session.close.call_count==0 for c in clients)
        assert len(list(reader.ordered(HASHES,100,lambda h,b,r:b)))==3
    assert all(c.w3.provider.session.close.call_count==1 for c in clients)


def test_failed_prefix_resume_keeps_payloads_metrics_and_fingerprint(store,cfg):
    base=fake_collector(store,cfg)
    base.arc.address_activity.return_value=page(HASHES)
    base.step(W,NOW)
    failure=[True];calls=[]
    def hook(h):
        calls.append(h)
        if h==HASHES[1] and failure[0]:raise RPCError('eth_getTransactionReceipt','429',3,'primary')
    settings=replace(cfg,historical_transactions_per_step=3,historical_rpc_workers=1)
    context=decoding_context(settings)
    with closing(EfficientCollector(store,base.rpc,base.arc,settings,rpc_factory=factory(hook))) as collector:
        collector.identity=Mock(return_value={})
        with pytest.raises(RPCError):collector.step(W,NOW+1)
        assert store.db.execute('SELECT COUNT(payload) FROM historical_transactions').fetchone()[0]==1
        failure[0]=False;calls.clear()
        collector.step(W,NOW+2)
        assert calls==HASHES[1:]
    rows=[json.loads(r[0]) for r in store.db.execute('SELECT payload FROM historical_transactions ORDER BY tx_hash')]
    rpc=factory()()
    expected=[base.transaction(W,h,{'cutoff_block':100},rpc=rpc) for h in HASHES]
    assert rows==expected and analyze(rows,cfg)==analyze(expected,cfg)
    assert decoding_context(settings)==context
    assert EfficientCollector.transaction is HistoricalCollector.transaction
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0]==0
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0]==0


def metadata_rpc(kind=ResearchRPC):
    rpc=kind.__new__(kind);rpc._metadata=OrderedDict()
    functions=SimpleNamespace(**{name:Mock(return_value=SimpleNamespace(call=Mock(return_value=value)))
                               for name,value in [('symbol','TEST'),('name','Test Token'),('decimals',18),('totalSupply',10**25)]})
    rpc.w3=SimpleNamespace(eth=SimpleNamespace(contract=Mock(return_value=SimpleNamespace(functions=functions))))
    rpc.swap_emitter_trusted=lambda *a:True
    return rpc,functions


def test_minimal_metadata_preserves_swap_result_and_halves_fields_requested():
    old,oldcalls=metadata_rpc(ArcRPC);new,newcalls=metadata_rpc()
    r=receipt([transfer(USDC_ERC20,W,POOL,100*10**6),transfer(T,POOL,W,5*10**18)])
    assert classify_trade(old,r,W,1e-18,strict=True)==classify_trade(new,r,W,1e-18,strict=True)
    assert all(getattr(oldcalls,k).call_count==1 for k in ('symbol','name','decimals','totalSupply'))
    assert newcalls.name.call_count==newcalls.totalSupply.call_count==0
    assert newcalls.symbol.call_count==newcalls.decimals.call_count==1
    classify_trade(new,r,W,1e-18,strict=True)
    assert newcalls.decimals.call_count==1


def test_metadata_cache_is_bounded_expires_and_returns_defensive_copy(monkeypatch):
    rpc,calls=metadata_rpc();rpc.metadata_limit=1
    monkeypatch.setattr('historical_efficiency.time.monotonic',lambda:100)
    result=rpc.token_meta(T);result['decimals']=0
    assert rpc.token_meta(T)['decimals']==18
    monkeypatch.setattr('historical_efficiency.time.monotonic',lambda:161)
    rpc.token_meta(T)
    assert calls.decimals.call_count==2
    rpc.token_meta(W);rpc.token_meta(T)
    assert len(rpc._metadata)==1 and calls.decimals.call_count==4


@pytest.mark.parametrize('field',['symbol','decimals'])
def test_failed_metadata_is_never_cached(field):
    rpc,calls=metadata_rpc()
    method=getattr(calls,field).return_value.call
    method.side_effect=RPCError('eth_call','502',3,'primary')
    with pytest.raises(RPCError):rpc.token_meta(T)
    assert not rpc._metadata
    method.side_effect=None
    assert rpc.token_meta(T)['decimals']==18


@pytest.mark.parametrize('value',[None,-1,256,'bad'])
def test_invalid_decimals_do_not_create_acceptable_metadata(value):
    rpc,calls=metadata_rpc();calls.decimals.return_value.call.return_value=value
    with pytest.raises(ValueError):rpc.token_meta(T)
    assert not rpc._metadata


@pytest.mark.parametrize('compatible',[True,False])
def test_existing_snapshots_keep_verified_reuse_and_unknown_provenance_stays_unknown(store,cfg,compatible):
    from test_historical_snapshots import completed, saved
    base,_=completed(store,cfg)
    previous=saved(store)
    if not compatible:
        store.db.execute('UPDATE historical_snapshots SET context=NULL');store.db.commit()
    calls=[]
    with closing(EfficientCollector(store,base.rpc,base.arc,base.cfg,rpc_factory=factory(calls.append))) as collector:
        collector.step(W,NOW+20)
        assert saved(store)==previous
        assert store.db.execute('SELECT reuse FROM historical_refresh_context').fetchone()[0]==int(compatible)
        assert store.db.execute('SELECT COUNT(*) FROM historical_transactions WHERE payload IS NULL').fetchone()[0]==(0 if compatible else 2)
        # A fresh traversal plus a canonical anchor are still required in both cases.
        assert base.arc.address_activity.call_count==2
        assert calls==[]


@pytest.mark.parametrize('result,expected',[
    ({'wallet_steps':10,'incomplete_steps':0},5),
    ({'wallet_steps':10,'incomplete_steps':1},30),
    ({'wallet_steps':0,'incomplete_steps':0},30),
])
def test_active_pause_never_accelerates_idle_or_failed_cycles(cfg,result,expected):
    from historical_analysis import cycle_pause
    settings=replace(cfg,historical_poll_seconds=30,historical_active_poll_seconds=5)
    assert cycle_pause(result,settings)==expected
    assert cycle_pause(result,replace(settings,historical_poll_seconds=2))==2


@pytest.mark.parametrize('value',[0,float('nan'),float('inf')])
def test_invalid_active_pause_rejected(cfg,value):
    with pytest.raises(ValueError):replace(cfg,historical_active_poll_seconds=value)
