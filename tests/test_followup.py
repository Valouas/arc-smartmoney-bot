"""Regressions for the follow-up: persisted state, forward cohorts and trust."""
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from web3 import Web3

from test_audit_regressions import W,T,POOL,ROUTER,H,event,receipt,transfer,rpc,db,fixture
from config import Settings
from models import MarketSnapshot, RiskReport, WalletInfo
from storage import Storage
from bot import ArcSmartMoneyBot
from cluster import ClusterBook
from alert_outbox import AlertOutbox
from telegram_alerts import TelegramAlerts
from proposal_queue import ProposalQueue
from process_lock import ProcessLock
from launch_indexer import LaunchIndexer
from arcscan import ArcscanClient, ArcscanError
from dex_registry import TrustedDexRegistry
from decoder import classify_trade
from rpc import USDC_ERC20,ArcRPC
from paper_trader import PaperTrader
from forward_metrics import build_forward_profile,collect_outcomes
from identity_graph import IdentityGraph,IdentityRefresher
from intelligence_book import IntelligenceBook


def test_event_transaction_rolls_back_all_state(db):
    e=event()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.add_event(e); db.save_runtime_event(e); db.enqueue('alert','text')
            db.mark_event_processed(e)
            raise RuntimeError('crash before commit')
    for table in ('events','runtime_events','alert_outbox','processed_events'):
        assert db.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    with db.transaction():
        db.add_event(e); db.save_runtime_event(e); db.enqueue('alert','text'); db.mark_event_processed(e)
    assert db.event_processed(e) and db.recent_runtime_events(0)[0]==e


def test_nested_transaction_never_commits_early(db):
    with pytest.raises(RuntimeError):
        with db.transaction():
            with db.transaction(): db.enqueue('a','b')
            raise RuntimeError()
    assert db.db.execute('SELECT COUNT(*) FROM alert_outbox').fetchone()[0]==0


def test_outbox_success_dedup_and_recovery(db):
    db.enqueue('same','text',kind='BUY_OPPORTUNITY'); db.enqueue('same','text',kind='BUY_OPPORTUNITY')
    transport=SimpleNamespace(enabled=True,send=Mock(return_value={'state':'SENT','message_id':'1'}))
    out=AlertOutbox(db,transport)
    assert out.flush()==1 and out.flush()==0
    assert transport.send.call_count==1
    db.enqueue('crashed','text',kind='BUY_OPPORTUNITY'); db.db.execute("UPDATE alert_outbox SET state='SENDING' WHERE id='crashed'"); db.commit()
    out.recover(); out.flush()
    assert transport.send.call_count==1
    assert db.db.execute("SELECT state FROM alert_outbox WHERE id='crashed'").fetchone()[0]=='UNKNOWN'


def test_outbox_retries_only_known_rejection(db,monkeypatch):
    monkeypatch.setattr('alert_outbox.time.time',lambda:100)
    db.enqueue('a','text',kind='WALLET_PROMOTED')
    send=Mock(side_effect=[{'state':'PENDING','retry_after':60,'error':'rate_limited'},{'state':'SENT'}])
    out=AlertOutbox(db,SimpleNamespace(enabled=True,send=send))
    out.flush(); out.flush(); assert send.call_count==1
    monkeypatch.setattr('alert_outbox.time.time',lambda:160)
    out.flush(); assert send.call_count==2
    db.enqueue('b','text',kind='WALLET_PROMOTED'); send.side_effect=RuntimeError('sensitive_url')
    out.flush()
    row=db.db.execute("SELECT state,last_error FROM alert_outbox WHERE id='b'").fetchone()
    assert tuple(row)==('UNKNOWN','transport_unknown')


@pytest.mark.parametrize('status,payload,expected',[(200,{'ok':True,'result':{'message_id':3}},'SENT'),(429,{'ok':False,'parameters':{'retry_after':5}},'PENDING'),(403,{'ok':False},'FAILED'),(503,{'ok':False},'UNKNOWN'),(200,[],'UNKNOWN')])
def test_telegram_delivery_states(monkeypatch,status,payload,expected):
    response=Mock(status_code=status); response.json.return_value=payload
    monkeypatch.setattr('telegram_alerts.requests.post',lambda *a,**k:response)
    assert TelegramAlerts('test-secret','test-chat').send('text')['state']==expected


def test_telegram_transport_never_exposes_url(monkeypatch):
    monkeypatch.setattr('telegram_alerts.requests.post',Mock(side_effect=requests.Timeout('test-secret')))
    result=TelegramAlerts('test-secret','test-chat').send('text')
    assert result['state']=='UNKNOWN' and 'test-secret' not in json.dumps(result)


def signal(price=1,liquidity=100000,volume=10000,ts=100):
    book=ClusterBook(1200,600)
    s=book.add(event(timestamp=ts))
    s.status='VALIDATED'; s.risk=RiskReport(T,True,market=MarketSnapshot(T,symbol='T',price_usd=price,liquidity_usd=liquidity,volume_m5=volume))
    return s


def test_proposals_are_durable_one_pending_per_token(db,tmp_path):
    q=ProposalQueue(db,tmp_path/'p.jsonl'); s=signal()
    first,created=q.add(s); assert created
    s.first_seen+=10
    duplicate,created=q.add(s); assert not created and duplicate['id']==first['id']
    q.export(); q.export()
    assert len((tmp_path/'p.jsonl').read_text().splitlines())==1
    assert json.loads((tmp_path/'p.jsonl').read_text())['id']==first['id']


def test_scanner_lock_excludes_second_owner_and_releases(tmp_path):
    path=tmp_path/'owner.lock'
    with ProcessLock(path):
        with pytest.raises(RuntimeError):
            with ProcessLock(path): pass
    with ProcessLock(path): pass


def contract_page(address,nxt=None):
    return {'items':[{'address':address,'standard':'erc20','symbol':'X','creation_block':1,'timestamp':1}],
            'page':{'has_more':bool(nxt),'next':nxt}, 'ranking':{'complete':True}}


def indexer(db,callback):
    obj=LaunchIndexer.__new__(LaunchIndexer); obj.db=db
    obj.cfg=replace(Settings(),launch_discovery_pages=1)
    obj.arcscan=SimpleNamespace(explore_contracts=callback,next_cursor=ArcscanClient.next_cursor)
    return obj


def test_discovery_persists_cursor_across_restart_and_outage(db):
    calls=[]
    def fetch(**kwargs):
        calls.append(kwargs['cursor'])
        return contract_page(T,'page2') if not kwargs['cursor'] else contract_page(W)
    x=indexer(db,fetch); assert x._discover_stream('contracts')==1
    assert db.db.execute("SELECT cursor FROM discovery_cursors").fetchone()[0]=='page2'
    x=indexer(db,Mock(side_effect=ArcscanError('outage')))
    with pytest.raises(ArcscanError): x._discover_stream('contracts')
    assert db.db.execute('SELECT cursor FROM discovery_cursors').fetchone()[0]=='page2'
    x=indexer(db,fetch); x._discover_stream('contracts')
    assert calls==[None,'page2']
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0]==2
    state=db.db.execute('SELECT * FROM discovery_cursors').fetchone()
    assert state['cursor'] is None and state['stop_key']==T
    x._discover_stream('contracts')
    assert db.db.execute('SELECT cursor FROM discovery_cursors').fetchone()[0] is None


def test_discovery_detects_cycle_after_restart(db):
    x=indexer(db,lambda **k:contract_page(T,'cycle')); x._discover_stream('contracts')
    with pytest.raises(ArcscanError): indexer(db,lambda **k:contract_page(W,'cycle'))._discover_stream('contracts')


@pytest.mark.parametrize('mutation',[
    lambda p:p.pop('page'),
    lambda p:p['page'].update(has_more=True,next=None),
    lambda p:p.update(capped=True),
    lambda p:p['items'][0].pop('creation_block'),
])
def test_incomplete_launch_pages_never_advance_coverage(db,mutation):
    page=contract_page(T); mutation(page)
    with pytest.raises(ArcscanError): indexer(db,lambda **k:page)._discover_stream('contracts')
    assert db.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0]==0
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0]==0


def test_nft_only_page_retains_contract_cursor(db):
    page=contract_page(T,'next'); page['items'][0]['standard']='erc721'
    assert indexer(db,lambda **k:page)._discover_stream('contracts')==0
    assert db.db.execute('SELECT cursor FROM discovery_cursors').fetchone()[0]=='next'


@pytest.mark.parametrize('values',[{'outcome_horizon_seconds':0},{'outcome_grace_seconds':-1},{'launch_discovery_pages':0},{'identity_workers':9}])
def test_invalid_observation_settings_fail_early(values):
    with pytest.raises(ValueError): replace(Settings(),**values)


def test_dex_registry_pins_chain_address_and_runtime_hash(tmp_path):
    path=tmp_path/'dex.json'; code=b'example-bytecode'
    path.write_text(json.dumps({'chain_id':5042,'emitters':[{'address':POOL,'runtime_code_hash':Web3.to_hex(Web3.keccak(code))}]}))
    reg=TrustedDexRegistry(path,5042)
    assert reg.trusts(POOL,code)
    assert not reg.trusts(T,code) and not reg.trusts(POOL,b'changed')
    assert not TrustedDexRegistry(path,1).trusts(POOL,code)
    assert not TrustedDexRegistry(tmp_path/'missing',5042).trusts(POOL,code)


@pytest.mark.parametrize('payload',[[],{'chain_id':5042,'emitters':[None]},{'chain_id':5042,'emitters':[{'address':POOL,'runtime_code_hash':123}]}])
def test_malformed_registry_is_untrusted(tmp_path,payload):
    path=tmp_path/'dex.json'; path.write_text(json.dumps(payload))
    assert not TrustedDexRegistry(path,5042).entries


def test_spoofed_swap_or_unconnected_cash_has_no_authority():
    r=receipt([transfer(USDC_ERC20,W,POOL,100*10**6),transfer(T,POOL,W,10**18)])
    fake=rpc(); fake.swap_emitter_trusted=lambda *a:False
    assert not classify_trade(fake,r,W,25)[0]['confirmation_eligible']
    r=receipt([transfer(USDC_ERC20,W,ROUTER,100*10**6),transfer(T,POOL,W,10**18)])
    assert not classify_trade(rpc(),r,W,25)[0]['confirmation_eligible']


def test_transfer_logs_find_relayed_wallets():
    api=ArcRPC.__new__(ArcRPC)
    log=transfer(T,POOL,W,1); log['transactionHash']=bytes.fromhex(H[2:])
    api.w3=SimpleNamespace(eth=SimpleNamespace(get_logs=Mock(return_value=[log])))
    result=api.watched_participants(1,[W])
    assert result[H]=={W}


def setup_forward(db):
    db.db.execute("INSERT INTO observation_epochs VALUES('launch_universe',10)")
    db.db.execute("INSERT INTO discovery_cursors(stream,completed_at) VALUES('contracts',130)")
    for token,block,ts in ((T,2,20),(POOL,3,30)):
        db.upsert_candidate(token,'contracts',deployment_block=block,deployed_at=ts,deployer=ROUTER)
    for block in range(1,6): db.mark_wallet_coverage(W,block,block*10)
    db.observe_wallet(W,H,3,25)
    db.add_event(event(timestamp=25,block_number=3))
    return replace(Settings(),outcome_horizon_seconds=100,outcome_grace_seconds=10,profile_min_evaluable_trades=1)


def test_forward_denominator_and_fixed_outcome(db):
    cfg=setup_forward(db)
    p=build_forward_profile(db,W,cfg,now=50)
    assert p['opportunity_count']==2 and p['opportunity_capture_rate']==0.5
    assert p['pending_outcomes']==1 and p['winners']==0 and not p['confirmation_eligible']
    market=SimpleNamespace(token_snapshot=lambda _:MarketSnapshot(T,price_usd=30))
    assert collect_outcomes(db,market,now=126)==1
    p=build_forward_profile(db,W,cfg,now=130)
    assert p['winners']==1 and p['data_quality_ok'] and p['confirmation_eligible']
    assert collect_outcomes(db,SimpleNamespace(token_snapshot=lambda _:MarketSnapshot(T,price_usd=1000)),now=130)==0
    assert db.db.execute('SELECT price FROM trade_outcomes').fetchone()[0]==30


def test_missing_loser_is_not_removed_from_trials(db):
    cfg=setup_forward(db); build_forward_profile(db,W,cfg,now=50)
    collect_outcomes(db,SimpleNamespace(token_snapshot=lambda _:None),now=140)
    p=build_forward_profile(db,W,cfg,now=140)
    assert p['missing_outcomes']==1 and p['outcome_trials']==1
    assert not p['confirmation_eligible'] and not p['data_quality_ok']
    assert db.db.execute('SELECT status FROM trade_outcomes').fetchone()[0]=='MISSING'


def test_historical_winner_and_future_stakes_do_not_train_forward_cohort(db):
    cfg=setup_forward(db)
    db.add_research_trade(event(tx_hash='historical',timestamp=1,usdc_amount=100000))
    db.add_event(event(tx_hash='later',timestamp=40,block_number=4,usdc_amount=100000))
    p=build_forward_profile(db,W,cfg,now=50)
    assert p['launches_bought']==1 and p['median_launch_buy_usdc']==100


def test_gap_in_observed_blocks_excludes_opportunities(db):
    cfg=setup_forward(db)
    db.db.execute('DELETE FROM wallet_coverage WHERE block=2');db.commit()
    p=build_forward_profile(db,W,cfg,now=50)
    assert p['opportunity_count']==1 and p['unmatched_buys']==1 and not p['data_quality_ok']


def test_known_coverage_without_economic_evidence_cannot_confirm(db):
    cfg=setup_forward(db)
    db.db.execute('INSERT INTO reconstruction_gaps VALUES(?,?,?,?)',(W,H,'untrusted_swap',25));db.commit()
    assert not build_forward_profile(db,W,cfg,now=130)['data_quality_ok']


def test_stale_legacy_or_missing_profiles_have_no_authority(tmp_path):
    path=tmp_path/'intel.json'
    path.write_text(json.dumps({'profiles':{W:{'smart_score':99,'confirmation_eligible':True}}}))
    book=IntelligenceBook(path,0)
    assert not book.effective(W,99,4)['eligible']
    assert not book.effective(T,99,4)['eligible']
    path.write_text(json.dumps({'profiles':{W:{'profile_version':2,'generated_at':1,'smart_score':99,'confirmation_eligible':True,'data_quality_ok':True}}}))
    assert not book.effective(W,99,4)['eligible']


def test_paper_costs_partial_fills_reentry_and_idempotence(db):
    trader=PaperTrader(db,Settings())
    s=signal(liquidity=10000,volume=10000)
    buy=trader.maybe_open(s)
    assert buy['partial'] and buy['notional']==10 and buy['fee']>0 and buy['price']>1
    assert trader.maybe_open(s) is None
    # Only $1 can be sold in this liquidity scenario.
    m=MarketSnapshot(T,price_usd=1,liquidity_usd=1000,volume_m5=10000)
    first=trader._sell(T,m,200,'test','exit-1')
    assert first['partial'] and first['pnl_usd']<0
    assert trader._sell(T,m,200,'test','exit-1') is None
    full=trader._sell(T,MarketSnapshot(T,price_usd=1,liquidity_usd=100000,volume_m5=100000),201,'test','exit-2')
    assert not full['partial'] and db.get_position(T)['status']=='CLOSED'
    s.first_seen=300;s.last_seen=300
    assert trader.maybe_open(s) is not None
    assert db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==4


def test_paper_missing_depth_is_no_fill(db):
    trader=PaperTrader(db,Settings())
    assert trader.maybe_open(signal(volume=None)) is None


def test_identity_cache_backoff_and_ancestry(monkeypatch):
    now=[100]; monkeypatch.setattr('identity_graph.time.time',lambda:now[0])
    data={W:{'funded_by':POOL},T:{'funded_by':ROUTER},POOL:{'funded_by':'0x'+'5'*40},ROUTER:{'funded_by':'0x'+'5'*40}}
    fetch=Mock(side_effect=lambda a:data.get(a,{}))
    graph=IdentityGraph(SimpleNamespace(address_facts=fetch),cache_seconds=10)
    graph.refresh([W,T]); assert graph.related(W,T)
    count=fetch.call_count; graph.refresh([W,T]); assert fetch.call_count==count
    now[0]=120; graph.refresh([W,T]); assert fetch.call_count>count


def test_clusters_restore_and_old_tokens_are_evicted(db):
    with db.transaction(): db.save_runtime_event(event(timestamp=100))
    book=ClusterBook(1200,600)
    for e in db.recent_runtime_events(0): book.add(e)
    s=book.add(event(wallet=POOL,tx_hash='new',timestamp=101))
    assert s.distinct_wallets==2
    book.add(event(token=POOL,timestamp=10000))
    assert T not in book.events

def make_bot(db,tmp_path):
    b=ArcSmartMoneyBot.__new__(ArcSmartMoneyBot); b.cfg=Settings(); b.db=db
    b.watch={W:WalletInfo(W,'one',4,'seed'),ROUTER:WalletInfo(ROUTER,'two',4,'seed')}
    b.clusters=ClusterBook(1200,600)
    b.identities=SimpleNamespace(eligible=lambda _:True,identity_id=lambda a:a)
    b.intelligence=SimpleNamespace(effective=lambda *a:{'score':90,'class':'SMART','eligible':True,'weight':4})
    b.risk=SimpleNamespace(inspect=lambda _:RiskReport(T,True,top10_concentration_pct=10,
        market=MarketSnapshot(T,price_usd=1,liquidity_usd=50000,market_cap_usd=100000,
            pair_created_at_ms=int(time.time()*1000),volume_m5=10000,volume_h1=12000)))
    b.paper=PaperTrader(db,b.cfg); b.proposals=ProposalQueue(db,tmp_path/'proposals.jsonl')
    return b


def test_handler_crash_rolls_back_paper_proposals_signals_and_replay(db,tmp_path):
    b=make_bot(db,tmp_path)
    first=event(timestamp=100,wallet_weight=4,reputation_score=90,identity_id=W)
    second=event(timestamp=101,tx_hash='second',wallet=ROUTER,wallet_weight=4,reputation_score=90,identity_id=ROUTER)
    b.handle_event(first)
    queue=b.proposals
    b.proposals=SimpleNamespace(add=Mock(side_effect=RuntimeError('crash')))
    with pytest.raises(RuntimeError): b.handle_event(second)
    assert db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==1
    assert db.get_position(T) is None and not db.event_processed(second)
    b.proposals=queue; b.handle_event(second); b.handle_event(second)
    assert db.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]==2
    assert db.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0]==1
    assert db.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0]==1
    assert db.event_processed(second)
    assert db.db.execute("SELECT COUNT(*) FROM trade_outcomes WHERE wallet='signal'").fetchone()[0]==1
    restarted=make_bot(db,tmp_path)
    restarted.rpc=SimpleNamespace(get_block=lambda *a,**k:{'timestamp':102,'transactions':[]},watched_participants=lambda *a:{})
    restarted.process_block(2)
    restored=list(restarted.clusters.events[T]['BUY'])
    assert len(restored)==2 and db.next_block()==3


def test_nested_failure_caught_by_outer_uses_savepoint(db):
    with db.transaction():
        db.enqueue('keep','one')
        try:
            with db.transaction():
                db.enqueue('rollback','two')
                raise RuntimeError()
        except RuntimeError: pass
    assert [r[0] for r in db.db.execute('SELECT id FROM alert_outbox')]==['keep']


def test_untrusted_sell_cannot_retrigger_old_eligible_exit(db,tmp_path):
    b=make_bot(db,tmp_path)
    b.clusters.add(event(side='SELL',timestamp=99))
    b.paper=SimpleNamespace(maybe_exit_on_cluster=Mock())
    b.handle_event(event(side='SELL',timestamp=100,tx_hash='untrusted',wallet=ROUTER,
        trade_verified=False,confirmation_eligible=False))
    b.paper.maybe_exit_on_cluster.assert_not_called()


def test_chain_code_lookup_is_at_receipt_block():
    r=ArcRPC.__new__(ArcRPC); r.dex_registry=SimpleNamespace(entries={POOL:True},trusts=lambda a,c:True)
    get_code=Mock(return_value=b'code'); r.w3=SimpleNamespace(eth=SimpleNamespace(get_code=get_code))
    assert r.swap_emitter_trusted(POOL,123)
    assert get_code.call_args.kwargs['block_identifier']==123


def test_related_winner_deployers_count_once(db):
    from auto_discovery import AutoDiscovery
    d=AutoDiscovery.__new__(AutoDiscovery); d.db=db
    d.arc=SimpleNamespace(address_facts=lambda a:{'funded_by':'0x'+'9'*40})
    db.upsert_candidate(T,'contracts',deployer=W)
    db.upsert_candidate(POOL,'contracts',deployer=ROUTER)
    assert d._independent_winners([T,POOL])==1
    assert d._independent_winners(['0x'+'8'*40])==0


def test_async_identity_refresh_does_not_block_scanner():
    import threading
    blocker=threading.Event(); entered=threading.Event()
    def fetch(_):
        entered.set(); blocker.wait(2)
        return {'funded_by':POOL}
    refresh=IdentityRefresher(SimpleNamespace(address_facts=fetch))
    try:
        initial=refresh.poll([W]); assert entered.wait(1)
        assert not initial.eligible(W)
        # Poll is nonblocking while the HTTP worker is held.
        assert refresh.poll([W]) is initial
        blocker.set(); refresh.future.result(timeout=3)
        assert refresh.poll([W]).eligible(W)
    finally:
        blocker.set(); refresh.close()


def test_late_outcome_request_does_not_masquerade_as_target_quote(db,monkeypatch):
    cfg=setup_forward(db); build_forward_profile(db,W,cfg,now=50)
    clock=iter([126,140]); monkeypatch.setattr('forward_metrics.time.time',lambda:next(clock))
    collect_outcomes(db,SimpleNamespace(token_snapshot=lambda _:MarketSnapshot(T,price_usd=30)))
    row=db.db.execute('SELECT status,price FROM trade_outcomes').fetchone()
    assert row['status']=='PENDING' and row['price'] is None


def test_backtest_reports_missing_bounds_and_no_repeated_token_trials(db,tmp_path,monkeypatch):
    from backtest_v3 import BacktestV3
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('storage.time.time',lambda:100)
    db.db.execute("INSERT INTO observation_epochs VALUES('launch_universe',1)");db.commit()
    s=signal(); first=db.add_signal(s); db.add_signal(s)
    cfg=replace(Settings(),outcome_horizon_seconds=10,outcome_grace_seconds=5)
    db.register_signal_outcome(first,T,cfg)
    monkeypatch.setattr('backtest_v3.time.time',lambda:200)
    b=BacktestV3.__new__(BacktestV3); b.storage=db;b.db=db.db
    out=b.run()['summary']
    assert out['signals']==1 and out['missing']==1
    assert out['win_rate_lower_bound']==0 and out['win_rate_upper_bound']==1
    assert out['insufficient_data']
