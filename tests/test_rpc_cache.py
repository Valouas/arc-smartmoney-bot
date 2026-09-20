import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from rpc_cache import RPCCache
from rpc_transport import ResilientRPCProvider,RPCError
from test_rpc_transport import FakeHTTP

H='0x'+'11'*32
B='0x'+'22'*32
F='0x'+'33'*32


def header(number=100,h=F):
    return {'number':hex(number),'hash':h,'timestamp':'0x1','transactions':[]}


def tx(number=1):
    return {'hash':H,'blockNumber':hex(number),'blockHash':B}


def provider(tmp_path,results,chain=5042):
    http=FakeHTTP(results)
    cache=RPCCache(tmp_path/'cache.sqlite3')
    p=ResilientRPCProvider('https://secret@primary.test/key',chain,Mock(),session=http,sleep=lambda _:None,cache=cache)
    return p,http


def test_finalized_transaction_reused_after_restart_without_paid_tx_request(tmp_path):
    p,http=provider(tmp_path,[header(),tx()])
    assert p.make_request('eth_getTransactionByHash',[H])['result']==tx()
    new,network=provider(tmp_path,[])
    assert new.make_request('eth_getTransactionByHash',[H])['result']==tx()
    assert [m for _,m,_ in network.calls]==['eth_chainId']
    with sqlite3.connect(p.cache.path) as db:
        assert db.execute('SELECT hits,misses,writes FROM rpc_cache_stats').fetchone()==(1,1,1)
    assert b'secret' not in p.cache.path.read_bytes()


def test_receipt_reused_and_by_number_chain_checks_always_fresh(tmp_path):
    receipt={'transactionHash':H,'blockNumber':'0x1','blockHash':B,'status':'0x1','logs':[]}
    p,http=provider(tmp_path,[header(),receipt,header(1,B),header(1,F)])
    assert p.make_request('eth_getTransactionReceipt',[H])['result']==receipt
    assert p.make_request('eth_getTransactionReceipt',[H])['result']==receipt
    assert p.make_request('eth_getBlockByNumber',['0x1',False])['result']['hash']==B
    assert p.make_request('eth_getBlockByNumber',['0x1',False])['result']['hash']==F


def test_unfinalized_transaction_is_never_reused(tmp_path):
    p,http=provider(tmp_path,[header(),tx(101),tx(101)])
    for _ in range(2):p.make_request('eth_getTransactionByHash',[H])
    assert sum(m=='eth_getTransactionByHash' for _,m,_ in http.calls)==2
    with sqlite3.connect(p.cache.path) as db:assert db.execute('SELECT COUNT(*) FROM rpc_cache_entries').fetchone()[0]==0


@pytest.mark.parametrize('answer',[None,{},429])
def test_errors_and_invalid_results_never_cached(tmp_path,answer):
    p,http=provider(tmp_path,[header(),answer,answer,answer])
    with pytest.raises(RPCError):p.make_request('eth_getTransactionByHash',[H])
    with sqlite3.connect(p.cache.path) as db:assert db.execute('SELECT COUNT(*) FROM rpc_cache_entries').fetchone()[0]==0


def test_unsupported_finalized_tag_falls_back_to_real_uncached_reads(tmp_path):
    p,http=provider(tmp_path,[400,tx(),tx()])
    for _ in range(2):assert p.make_request('eth_getTransactionByHash',[H])['result']==tx()
    assert sum(m=='eth_getTransactionByHash' for _,m,_ in http.calls)==2


def test_finality_change_rejected_before_cached_response(tmp_path,monkeypatch):
    monkeypatch.setattr('rpc_transport.time.time',lambda:100)
    p,http=provider(tmp_path,[header(),tx(),header(110),header(100,B)])
    p.make_request('eth_getTransactionByHash',[H])
    monkeypatch.setattr('rpc_transport.time.time',lambda:161)
    with pytest.raises(RPCError,match='finality_changed'):p.make_request('eth_getTransactionByHash',[H])


def test_finality_probe_failure_keeps_anchor_for_next_check(tmp_path,monkeypatch):
    monkeypatch.setattr('rpc_transport.time.time',lambda:100)
    p,http=provider(tmp_path,[header(),tx(),400,tx(),header(110),header(100,B)])
    p.make_request('eth_getTransactionByHash',[H])
    monkeypatch.setattr('rpc_transport.time.time',lambda:161)
    p.make_request('eth_getTransactionByHash',[H])
    monkeypatch.setattr('rpc_transport.time.time',lambda:222)
    with pytest.raises(RPCError,match='finality_changed'):p.make_request('eth_getTransactionByHash',[H])


def test_corrupt_cache_is_miss_and_refetched(tmp_path):
    p,http=provider(tmp_path,[header(),tx(),tx()])
    p.make_request('eth_getTransactionByHash',[H])
    with sqlite3.connect(p.cache.path) as db:db.execute("UPDATE rpc_cache_entries SET payload='{}'")
    assert p.make_request('eth_getTransactionByHash',[H])['result']==tx()
    assert sum(m=='eth_getTransactionByHash' for _,m,_ in http.calls)==2


def test_chain_isolation_and_wrong_provider_rejected(tmp_path):
    p,http=provider(tmp_path,[header(),tx()])
    p.make_request('eth_getTransactionByHash',[H])
    assert p.cache.get(1,'eth_getTransactionByHash',[H],100) is None
    wrong,_=provider(tmp_path,[],chain=1)
    with pytest.raises(RPCError,match='wrong_chain'):wrong.make_request('eth_getTransactionByHash',[H])


def test_live_logs_calls_and_read_only_guard_unchanged(tmp_path):
    p,http=provider(tmp_path,[[],[],'0x01','0x02'])
    for _ in range(2):assert p.make_request('eth_getLogs',[{}])['result']==[]
    assert p.make_request('eth_call',[{},'latest'])['result']=='0x01'
    assert p.make_request('eth_call',[{},'latest'])['result']=='0x02'
    with pytest.raises(RPCError,match='read_only'):p.make_request('eth_sendRawTransaction',['0x'])
    with sqlite3.connect(p.cache.path) as db:assert db.execute('SELECT COUNT(*) FROM rpc_cache_entries').fetchone()[0]==0


def test_independent_connections_share_cache_and_atomic_stats(tmp_path):
    p,_=provider(tmp_path,[header(),tx()])
    p.make_request('eth_getTransactionByHash',[H])
    def read(_):return RPCCache(p.cache.path).get(5042,'eth_getTransactionByHash',[H],100)
    with ThreadPoolExecutor(max_workers=4) as pool:assert list(pool.map(read,range(20)))==[tx()]*20
    with sqlite3.connect(p.cache.path) as db:assert db.execute('SELECT hits FROM rpc_cache_stats').fetchone()[0]==20


def test_block_by_hash_cached_but_full_and_header_requests_separate(tmp_path):
    p,http=provider(tmp_path,[header(),header(1,B),header(1,B)])
    for _ in range(2):p.make_request('eth_getBlockByHash',[B,False])
    p.make_request('eth_getBlockByHash',[B,True])
    assert sum(m=='eth_getBlockByHash' for _,m,_ in http.calls)==2


def test_explicit_historical_header_cache_survives_restart_but_anchors_stay_fresh(tmp_path):
    p,http=provider(tmp_path,[header(),header(1,B)])
    with p.historical_headers():
        assert p.make_request('eth_getBlockByNumber',['0x1',False])['result']==header(1,B)
    other,network=provider(tmp_path,[header(1,F)])
    with other.historical_headers():
        assert other.make_request('eth_getBlockByNumber',['0x1',False])['result']==header(1,B)
    # Outside the evidence context, even the same number must be read fresh.
    assert other.make_request('eth_getBlockByNumber',['0x1',False])['result']==header(1,F)
    assert [m for _,m,_ in network.calls]==['eth_chainId','eth_getBlockByNumber']


def test_historical_header_above_finality_is_never_reused(tmp_path):
    p,http=provider(tmp_path,[header(),header(101,B),header(101,B)])
    with p.historical_headers():
        for _ in range(2):p.make_request('eth_getBlockByNumber',['0x65',False])
    assert sum(params==['0x65',False] for _,_,params in http.calls)==2


def test_header_context_restored_on_error_and_does_not_cache_tags_or_full_blocks(tmp_path):
    p,http=provider(tmp_path,[header(),header(),header(1,B),header(1,B)])
    with pytest.raises(ValueError):
        with p.historical_headers():
            p.make_request('eth_getBlockByNumber',['latest',False])
            p.make_request('eth_getBlockByNumber',['latest',False])
            p.make_request('eth_getBlockByNumber',['0x1',True])
            raise ValueError('test')
    p.make_request('eth_getBlockByNumber',['0x1',False])
    with sqlite3.connect(p.cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM rpc_cache_entries').fetchone()[0]==0


def test_cached_number_header_checks_finality_and_wrong_number_response(tmp_path,monkeypatch):
    monkeypatch.setattr('rpc_transport.time.time',lambda:100)
    p,http=provider(tmp_path,[header(),header(2,B),header(1,B),header(110),header(100,B)])
    with p.historical_headers():
        with pytest.raises(RPCError):p.make_request('eth_getBlockByNumber',['0x1',False])
        p.make_request('eth_getBlockByNumber',['0x1',False])
        monkeypatch.setattr('rpc_transport.time.time',lambda:161)
        with pytest.raises(RPCError,match='finality_changed'):
            p.make_request('eth_getBlockByNumber',['0x1',False])
