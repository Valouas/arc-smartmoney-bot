import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hexbytes import HexBytes

from alchemy_webhook import create_server
from config import Settings
from storage import Storage
from models import WalletInfo
from webhook_queue import accept, SCHEMA, MAX_BODY
from webhook_consumer import WebhookConsumer
from test_audit_regressions import W,T,POOL,ROUTER,H,event,receipt,transfer,rpc
from test_followup import make_bot
from rpc import USDC_ERC20

NOW=100000
KEY='unit-test-key-not-a-real-secret'


@pytest.fixture
def cfg(tmp_path):
    return replace(Settings(),alchemy_webhook_enabled=True,alchemy_webhook_signing_key=KEY,
                   alchemy_webhook_host='127.0.0.1',trusted_dex_registry=str(tmp_path/'registry.json'),
                   rpc_url='https://rpc.example.test',rpc_fallback_urls=(),telegram_bot_token='',telegram_chat_id='')


def payload(event_id='whevt_test',wallet=W):
    return {'id':event_id,'type':'ADDRESS_ACTIVITY','event':{'network':'ARC_MAINNET','activity':[
        {'hash':H,'fromAddress':wallet,'toAddress':POOL,
         'log':{'transactionHash':H,'logIndex':'0x3','removed':False}}]}}


@pytest.fixture
def endpoint(tmp_path,cfg):
    path=tmp_path/'webhook.sqlite3'
    factory=lambda:SimpleNamespace(reload=lambda:None,items={W:WalletInfo(W,'seed',4,'seed')})
    servers=[]
    def start(custom=None,paper_watch=None):
        server=create_server(custom or cfg,path,factory,port=0,paper_watch_factory=paper_watch or (lambda:set()))
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
        thread.start(); servers.append((server,thread))
        return server,path
    yield start
    for server,thread in servers:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def request(server,data=None,signature='auto',method='POST',path='/webhook/alchemy',extra=None):
    body=data if isinstance(data,bytes) else json.dumps(data or payload()).encode()
    headers={'Content-Type':'application/json'}
    if signature=='auto': signature=hmac.new(KEY.encode(),body,hashlib.sha256).hexdigest()
    if signature is not None: headers['X-Alchemy-Signature']=signature
    headers.update(extra or {})
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
    try:
        conn.request(method,path,body=body if method=='POST' else None,headers=headers)
        response=conn.getresponse()
        return response.status,json.loads(response.read())
    finally: conn.close()


def count(path,table):
    with sqlite3.connect(path) as db: return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]


def test_health_and_valid_signature_durable_acceptance(endpoint):
    server,path=endpoint()
    assert request(server,method='GET',path='/health')[0]==200
    status,result=request(server)
    assert status==200 and result['queued']==1
    assert count(path,'alchemy_deliveries')==count(path,'alchemy_items')==count(path,'alchemy_jobs')==1
    assert count(path,'events')==count(path,'scanner_state')==0


@pytest.mark.parametrize('signature',[None,'wrong','0'*64])
def test_missing_or_invalid_signature_refused(endpoint,signature):
    server,path=endpoint()
    assert request(server,signature=signature)[0]==401
    assert count(path,'alchemy_deliveries')==0


def test_signature_covers_exact_raw_body(endpoint):
    server,path=endpoint()
    body=json.dumps(payload(),indent=2).encode()
    signature=hmac.new(KEY.encode(),json.dumps(payload()).encode(),hashlib.sha256).hexdigest()
    assert request(server,body,signature)[0]==401
    assert request(server,body)[0]==200


def test_unconfigured_key_never_accepts_unsigned_delivery(endpoint,cfg):
    server,path=endpoint(replace(cfg,alchemy_webhook_signing_key=''))
    assert request(server,method='GET',path='/health')==(200,{'status':'ok','ingestion_ready':False})
    assert request(server,signature=None)[0]==503
    assert count(path,'alchemy_jobs')==0
    assert KEY not in repr(cfg)


def test_duplicate_event_and_duplicate_tx_log_are_persistent(endpoint):
    server,path=endpoint()
    assert request(server)[1]['queued']==1
    assert request(server)[1]['duplicate'] is True
    assert request(server,payload('whevt_retry_with_new_id'))[1]['queued']==0
    second,_=endpoint()
    assert request(second)[1]['duplicate'] is True
    assert count(path,'alchemy_deliveries')==2 and count(path,'alchemy_jobs')==1


@pytest.mark.parametrize('change',[lambda p:p['event'].update(network='ETH_MAINNET'),
    lambda p:p.update(type='CUSTOM'),lambda p:p['event']['activity'][0].update(hash='bad'),
    lambda p:p['event']['activity'][0]['log'].update(transactionHash='0x'+'b'*64)])
def test_bad_or_wrong_network_payload_refused(endpoint,change):
    server,path=endpoint(); p=payload(); change(p)
    assert request(server,p)[0]==400
    assert count(path,'alchemy_deliveries')==0


def test_payload_limit_and_database_failure_cannot_acknowledge(endpoint,monkeypatch):
    server,path=endpoint()
    assert request(server,extra={'Content-Length':str(MAX_BODY+1)})[0]==413
    monkeypatch.setattr('alchemy_webhook.accept',Mock(side_effect=sqlite3.OperationalError('busy')))
    assert request(server)[0]==503
    assert count(path,'alchemy_deliveries')==0


def test_unwatched_wallet_and_removed_log_are_not_queued(endpoint):
    server,path=endpoint()
    assert request(server,payload(wallet=ROUTER))[1]['queued']==0
    p=payload('removed'); p['event']['activity'][0]['log']['removed']=True
    assert request(server,p)[1]['queued']==0
    assert count(path,'alchemy_jobs')==0


def test_dex_infrastructure_not_queued_even_when_in_watchlist(endpoint,tmp_path):
    (tmp_path/'registry.json').write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    server,path=endpoint()
    assert request(server)[1]['queued']==0
    assert count(path,'alchemy_jobs')==0


@pytest.fixture
def engine(tmp_path,cfg,monkeypatch):
    monkeypatch.setattr('webhook_consumer.time.time',lambda:NOW)
    db=Storage(tmp_path/'engine.sqlite3')
    bot=make_bot(db,tmp_path); bot.cfg=cfg
    yield bot,WebhookConsumer(bot)
    db.db.close()


def rpc_for(bot,wallet=W,tx_hash=H,block_number=1,swap=True, index=0):
    r=receipt([transfer(USDC_ERC20,wallet,POOL,100*10**6),transfer(T,POOL,wallet,10**18)],swap)
    r.update(blockHash=HexBytes('0x'+'c'*64),blockNumber=block_number,transactionHash=HexBytes(tx_hash),transactionIndex=index)
    tx={'hash':HexBytes(tx_hash),'blockNumber':block_number,'blockHash':r['blockHash'],'from':wallet,'value':0}
    block={'number':block_number,'hash':r['blockHash'],'timestamp':NOW,'transactions':[tx]}
    bot.rpc=rpc()
    bot.rpc.get_receipt=Mock(return_value=r)
    bot.rpc.get_block=Mock(return_value=block)
    bot.rpc.block_number=Mock(return_value=block_number+2)
    bot.rpc.w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=Mock(return_value=tx)))
    bot.rpc.watched_participants=lambda *args:{tx_hash:{wallet}}
    return tx,r,block


def enqueue(bot,wallet=W,tx_hash=H,event_id='whevt_test'):
    p=payload(event_id,wallet); p['event']['activity'][0]['hash']=tx_hash
    p['event']['activity'][0]['log']['transactionHash']=tx_hash
    return accept(bot.db.db,json.dumps(p).encode(),bot.watch,NOW)


def test_simple_transfer_and_untrusted_swap_never_create_trading_event(engine):
    bot,consumer=engine
    _,r,_=rpc_for(bot,swap=False)
    r['logs']=r['logs'][1:]  # Pure incoming ERC20 transfer, no cash flow.
    enqueue(bot); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert bot.db.next_block() is None and not bot.db.tx_seen(H)
    rpc_for(bot,tx_hash='0x'+'b'*64,swap=False)
    enqueue(bot,tx_hash='0x'+'b'*64,event_id='second'); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0


def test_confirmed_swap_shared_with_backfill_without_double_trade(engine):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert bot.db.next_block() is None and not bot.db.tx_seen(H)
    assert bot.db.db.execute('SELECT COUNT(*) FROM wallet_coverage').fetchone()[0]==0
    bot.process_block(1)
    assert bot.db.next_block()==2 and bot.db.tx_seen(H)
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert bot.db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==1
    consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==1


def test_backfill_first_then_webhook_is_deduplicated(engine):
    bot,consumer=engine
    rpc_for(bot); bot.process_block(1)
    enqueue(bot); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert bot.db.next_block()==2


def test_two_confirmed_independent_swaps_use_existing_paper_rules(engine):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot); consumer.drain()
    rpc_for(bot,wallet=ROUTER,tx_hash='0x'+'b'*64,block_number=2)
    enqueue(bot,wallet=ROUTER,tx_hash='0x'+'b'*64,event_id='second'); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==1
    assert bot.db.db.execute("SELECT COUNT(*) FROM signals WHERE status='VALIDATED'").fetchone()[0]==1
    assert bot.db.next_block() is None


def test_live_future_event_cannot_confirm_old_backfill(engine):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot); consumer.drain()
    old=event(tx_hash='older',wallet=ROUTER,block_number=0,timestamp=NOW-10,
              wallet_weight=4,identity_id=ROUTER,reputation_score=90,transaction_index=0)
    bot.handle_event(old)
    assert bot.db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==0
    assert bot.db.db.execute('SELECT status FROM signals ORDER BY id DESC LIMIT 1').fetchone()[0]=='WATCH'


def test_same_block_future_transaction_not_used_as_evidence(engine):
    bot,consumer=engine
    rpc_for(bot,index=3); enqueue(bot); consumer.drain()
    old=event(tx_hash='earlier-index',wallet=ROUTER,block_number=1,timestamp=NOW,
              wallet_weight=4,identity_id=ROUTER,reputation_score=90,transaction_index=1)
    bot.handle_event(old)
    assert bot.db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==0


def test_rpc_failure_is_retryable_and_never_marks_completion(engine):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot)
    bot.rpc.get_receipt.side_effect=RuntimeError('endpoint credentials must never be logged')
    consumer.drain()
    job=bot.db.db.execute('SELECT * FROM alchemy_jobs').fetchone()
    assert job['status']=='PENDING' and job['attempts']==1 and job['error']=='RuntimeError'
    assert bot.db.next_block() is None and not bot.db.tx_seen(H)
    bot.rpc.get_receipt.side_effect=None
    with bot.db.db: bot.db.db.execute('UPDATE alchemy_jobs SET next_attempt=0')
    consumer.drain()
    assert bot.db.db.execute('SELECT status FROM alchemy_jobs').fetchone()[0]=='DONE'


def test_removed_wallet_is_rechecked_at_processing(engine):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot); bot.watch={}
    consumer.drain()
    bot.rpc.get_receipt.assert_not_called()
    assert bot.db.db.execute('SELECT status FROM alchemy_jobs').fetchone()[0]=='IGNORED'


def test_supervisor_adds_receiver_only_when_enabled(cfg):
    from run_v4 import commands_for
    assert any(c[-1]=='alchemy_webhook.py' for c in commands_for(cfg))
    assert all(c[-1]!='alchemy_webhook.py' for c in commands_for(replace(cfg,alchemy_webhook_enabled=False)))


def test_failed_receiver_does_not_stop_scanner(monkeypatch,capsys):
    import run_v4
    scanner=Mock(); scanner.poll.return_value=None
    receiver=Mock(); receiver.poll.return_value=1
    monkeypatch.setattr(run_v4,'commands_for',lambda _: [['python','bot.py'],['python','alchemy_webhook.py']])
    monkeypatch.setattr(run_v4.subprocess,'Popen',Mock(side_effect=[scanner,receiver]))
    monkeypatch.setattr(run_v4.time,'sleep',Mock(side_effect=[None,None,KeyboardInterrupt()]))
    run_v4.main()
    assert 'scanner/backfill continue' in capsys.readouterr().out
    scanner.terminate.assert_not_called()


def test_multiple_logs_and_concurrent_delivery_do_not_multiply_jobs(endpoint):
    from concurrent.futures import ThreadPoolExecutor
    server,path=endpoint()
    p=payload(); second=json.loads(json.dumps(p['event']['activity'][0]))
    second['log']['logIndex']='0x4'; p['event']['activity'].append(second)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:request(server,p),range(4)))
    assert all(status==200 for status,_ in results)
    assert count(path,'alchemy_jobs')==1 and count(path,'alchemy_items')==2
    assert count(path,'alchemy_deliveries')==1


def test_quarantine_is_effective_watchlist_but_has_no_new_authority(endpoint):
    server,path=endpoint()
    store=Storage(path); store.upsert_discovery_candidate(ROUTER,T,1); store.db.close()
    assert request(server,payload(wallet=ROUTER))[1]['queued']==1


@pytest.mark.parametrize('case',['old','confirmations','mismatch'])
def test_stale_unconfirmed_or_noncanonical_trigger_does_not_trade(engine,case):
    bot,consumer=engine
    _,r,block=rpc_for(bot); enqueue(bot)
    if case=='old': block['timestamp']=NOW-121
    elif case=='confirmations': bot.rpc.block_number.return_value=1
    else: r['blockHash']=b'x'*32
    consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert bot.db.next_block() is None
    status=bot.db.db.execute('SELECT status FROM alchemy_jobs').fetchone()[0]
    assert status==('IGNORED' if case=='old' else 'PENDING')


def test_late_retry_after_engine_commit_cannot_duplicate_trade(engine,monkeypatch):
    bot,consumer=engine
    rpc_for(bot); enqueue(bot)
    original=consumer.finish
    monkeypatch.setattr(consumer,'finish',Mock(side_effect=RuntimeError('crash after event commit')))
    consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    monkeypatch.setattr(consumer,'finish',original)
    with bot.db.db: bot.db.db.execute('UPDATE alchemy_jobs SET next_attempt=0')
    consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert bot.db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==1


def test_webhook_cannot_grant_candidate_authority(engine):
    bot,consumer=engine
    bot.db.upsert_discovery_candidate(W,T,1)
    rpc_for(bot); enqueue(bot); consumer.drain()
    rpc_for(bot,wallet=ROUTER,tx_hash='0x'+'b'*64,block_number=2)
    enqueue(bot,wallet=ROUTER,tx_hash='0x'+'b'*64,event_id='second'); consumer.drain()
    assert bot.db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==0
    assert bot.db.db.execute('SELECT status FROM discovery_candidates').fetchone()[0]=='QUARANTINE'
