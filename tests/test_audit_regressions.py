import json
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hexbytes import HexBytes
from web3 import Web3

from arcscan import ArcscanClient, ArcscanError, require_complete_history
from auto_discovery import AutoDiscovery
from bot import ArcSmartMoneyBot
from cluster import ClusterBook
from config import Settings
from decoder import classify_trade, wallet_token_deltas
from discovery_parser import extract_transfer_rows, extract_tx_hashes
from identity_graph import IdentityGraph
from launch_parser import extract_contract_candidates, extract_tokens_from_transfers
from market_data import DexScreenerClient
from models import TradeEvent, MarketSnapshot, WalletInfo
from paper_trader import PaperTrader
from risk_engine import RiskEngine
from rpc import TRANSFER_TOPIC, USDC_ERC20, NATIVE_MIRROR, ArcRPC
from storage import Storage, SCHEMA, V4_SCHEMA
from wallet_profiler import WalletProfiler
from atomic_json import write_json
from watchlist_manager import WatchlistManager

W = '0x'+'1'*40
T = '0x'+'2'*40
POOL = '0x'+'3'*40
ROUTER = '0x'+'4'*40
H = '0x'+'a'*64


def fixture(name):
    return json.loads((Path(__file__).parent/'fixtures'/name).read_text(encoding='utf-8-sig'))


def transfer(token, sender, recipient, amount, wire=False):
    topics = [HexBytes(TRANSFER_TOPIC), HexBytes('0x'+'0'*24+sender[2:]), HexBytes('0x'+'0'*24+recipient[2:])]
    return {'address': token, 'topics': [Web3.to_hex(t) for t in topics] if wire else topics,
            'data': Web3.to_hex(amount.to_bytes(32,'big')) if wire else amount.to_bytes(32,'big')}


def receipt(logs, swap=True):
    if swap:
        logs = logs + [{'address':POOL,'topics':[Web3.keccak(text='Swap(address,uint256,uint256,uint256,uint256,address)')], 'data':b''}]
    return {'status':1,'logs':logs}


def rpc():
    return SimpleNamespace(token_meta=lambda _: {'decimals':18, 'symbol':'TEST'})


@pytest.mark.parametrize('wire',[False,True])
def test_erc20_swap_router_multihop_and_topic_formats(wire):
    r = receipt([transfer(USDC_ERC20,W,ROUTER,100*10**6,wire),
                 transfer(T,POOL,ROUTER,5*10**18,wire), transfer(T,ROUTER,W,5*10**18,wire)])
    result = classify_trade(rpc(),r,W,25)
    assert len(result)==1 and result[0]['confirmation_eligible']
    assert result[0]['usdc_amount']==100 and result[0]['token_amount']==5


def test_failed_receipt_airdrop_and_nft():
    r=receipt([transfer(T,POOL,W,10**18)],False)
    assert classify_trade(rpc(),r,W,25)==[]
    r['logs'].append(transfer(USDC_ERC20,W,ROUTER,100*10**6))
    assert not classify_trade(rpc(),r,W,25)[0]['confirmation_eligible']
    r['status']=0
    assert classify_trade(rpc(),r,W,25)==[]
    nft=transfer(T,POOL,W,1); nft['topics'].append(HexBytes('0x'+'0'*64))
    assert wallet_token_deltas(receipt([nft]), W)=={}


def test_basket_and_unknown_decimals():
    r=receipt([transfer(USDC_ERC20,W,ROUTER,100*10**6),transfer(T,POOL,W,10**18),transfer(ROUTER,POOL,W,10**18)])
    assert classify_trade(rpc(),r,W,25)==[]
    with pytest.raises(ValueError): classify_trade(rpc(),r,W,25,strict=True)
    r['logs'].pop(2)
    with pytest.raises(ValueError): classify_trade(SimpleNamespace(token_meta=lambda _: {}),r,W,25)


@pytest.mark.parametrize('sell',[False,True])
def test_native_mirror_refunds_and_sell(sell):
    logs=[transfer(NATIVE_MIRROR,W,ROUTER,100*10**18),transfer(NATIVE_MIRROR,ROUTER,W,20*10**18),transfer(T,POOL,W,10**18)]
    if sell: logs=[transfer(NATIVE_MIRROR,ROUTER,W,80*10**18),transfer(T,W,POOL,10**18)]
    r=receipt(logs)
    out=classify_trade(rpc(),r,W,25,native_value_wei=100*10**18 if not sell else 0)
    assert out[0]['usdc_amount']==80 and out[0]['confirmation_eligible']
    assert out[0]['side']==('SELL' if sell else 'BUY')


def test_native_and_erc20_mirrors_never_double_count():
    logs=[transfer(NATIVE_MIRROR,W,POOL,100*10**18),transfer(USDC_ERC20,W,POOL,100*10**6),transfer(T,POOL,W,10**18)]
    out=classify_trade(rpc(),receipt(logs),W,25,native_value_wei=100*10**18)
    assert out[0]['usdc_amount']==100
    logs[1]=transfer(USDC_ERC20,W,POOL,90*10**6)
    assert classify_trade(rpc(),receipt(logs),W,25)==[]


def test_live_native_v4_swap_fixture():
    rows={r['id']:r['result'] for r in fixture('arc_rpc_swap.json')}
    r=rows[1]; r['status']=int(r['status'],16)
    tx=rows[2]
    out=classify_trade(rpc(),r,tx['from'],25,native_value_wei=int(tx['value'],16))
    assert len(out)==1 and out[0]['confirmation_eligible']
    assert out[0]['token'].lower()=='0xa528ba8f99cfdb75731fa96d71712fbca6ab56d4'
    assert out[0]['usdc_amount']==pytest.approx(int(tx['value'],16)/1e18)


def test_live_arcscan_schemas():
    rows=extract_contract_candidates(fixture('arcscan_live_3.json'))
    assert rows[0]['deployment_block']==21175171 and rows[0]['is_erc20']
    transfers=fixture('arcscan_extra_1.json')
    assert extract_tokens_from_transfers(transfers)[0]['symbol']=='USDCAR'
    assert extract_transfer_rows(transfers)[0]['log_index']==687
    assert extract_tx_hashes(fixture('arcscan_live_1.json'))
    require_complete_history(fixture('arcscan_live_1.json'))
    with pytest.raises(ArcscanError): require_complete_history(fixture('arcscan_live_2.json'))
    g=IdentityGraph(Mock())
    f=g._parse_facts(W, fixture('arcscan_extra_2.json'))
    assert f.funded_by=='0xc715abec49a7f9a2f11f6d017a02d408cf66a4a8'
    assert g._parse_facts(W,fixture('arcscan_live_0.json')).funded_by is None


def test_concentration_explicit_units_and_live_census():
    c=ArcscanClient('https://unused')
    c._json=Mock(return_value=fixture('arcscan_extra_0.json'))
    assert c.concentration(T)==(24.5811,11667)
    for payload, expected in [({'top10_pct':0.5},0.5),({'top10_ratio':0.5},50),({'top10':0.5},None),({'top100_pct':12},None),({'top10_pct':'nan'},None)]:
        c._json=Mock(return_value=payload)
        assert c.concentration(T)[0]==expected
    truncated=fixture('arcscan_extra_0.json'); truncated['census']['truncated']=True
    c._json=Mock(return_value=truncated)
    assert c.concentration(T)==(None,None)


def test_arcscan_errors_and_parameter():
    c=ArcscanClient('https://unused')
    c._json=Mock(return_value={'items':[]})
    c.explore_token_transfers(standard='ERC-20')
    assert c._json.call_args.kwargs['params']['standard']=='erc20'
    c=ArcscanClient('https://unused')
    response=Mock(); response.json.return_value={'error':{'code':'BUSY'}}; c.s.get=Mock(return_value=response)
    with pytest.raises(ArcscanError): c.address_activity(W)
    response.json.return_value={'truncated':True,'items':[]}
    with pytest.raises(ArcscanError): c.token_transfers(T)


def test_coverage_unknown_gaps_caps():
    for data in [{}, {'items':[]},fixture('arcscan_live_2.json')]:
        with pytest.raises(ArcscanError): require_complete_history(data)
    data=fixture('arcscan_live_1.json'); data['page']['coverage']['gap_blocks']=1
    with pytest.raises(ArcscanError): require_complete_history(data)


def pair(chain='arc', base=T, quote=W, price='1', liq=50000):
    return {'chainId':chain,'baseToken':{'address':base,'symbol':'T'},'quoteToken':{'address':quote},'priceUsd':price,'liquidity':{'usd':liq}}


@pytest.mark.parametrize('bad',[pair(chain='ethereum'),pair(base=W,quote=T)])
def test_market_rejects_wrong_chain_and_quote_token(bad):
    m=DexScreenerClient(); m._get=Mock(return_value=[bad])
    assert m.token_snapshot(T) is None
    m._get=Mock(return_value=[])
    response=Mock(); response.json.return_value={'pairs':[bad,pair(liq=1)]}; m.s.get=Mock(return_value=response)
    assert m.token_snapshot(T).liquidity_usd==1


def test_market_nonfinite_price():
    m=DexScreenerClient(); m._get=Mock(return_value=[pair(price='nan')])
    assert m.token_snapshot(T).price_usd is None


@pytest.mark.parametrize('missing',['market','liquidity','market_cap','age','concentration','price'])
def test_missing_risk_never_validates(missing):
    m=MarketSnapshot(T,price_usd=1,liquidity_usd=50000,market_cap_usd=100000,pair_created_at_ms=9999999999999)
    attr={'liquidity':'liquidity_usd','market_cap':'market_cap_usd','age':'pair_created_at_ms','price':'price_usd'}
    if missing in attr: setattr(m,attr[missing],None)
    report=RiskEngine(SimpleNamespace(get_code=lambda _:b'code'),SimpleNamespace(token_snapshot=lambda _: None if missing=='market' else m),SimpleNamespace(concentration=lambda _: (None,None) if missing=='concentration' else (10,100)),Settings()).inspect(T)
    assert report.hard_fail_reasons and not report.passed


def event(**kwargs):
    args=dict(tx_hash=H,block_number=1,timestamp=1000,wallet=W,wallet_label='w',wallet_weight=3,side='BUY',token=T,token_symbol='T',token_amount=10,usdc_amount=100)
    args.update(kwargs); return TradeEvent(**args)


@pytest.fixture
def db(tmp_path):
    db=Storage(tmp_path/'test.sqlite3')
    yield db
    db.db.close()


def launch(db, token=T):
    db.db.execute('INSERT INTO launches(token,discovered_at,pair_created_at,first_price_usd) VALUES(?,100,100,1)',(token,)); db.db.commit()


def discovery(db):
    d=AutoDiscovery.__new__(AutoDiscovery); d.cfg=Settings(); d.db=db
    return d


def test_history_failure_never_completes_candidate_or_winner(db):
    d=discovery(db); db.upsert_discovery_candidate(W,T,1)
    d.activity_hashes=Mock(side_effect=ArcscanError('failed'))
    with pytest.raises(ArcscanError): d.hydrate_wallet(W)
    row=db.db.execute('SELECT * FROM discovery_candidates').fetchone()
    assert not row['history_complete'] and row['hydrated_at'] is None
    db.db.execute('INSERT INTO discovery_winners VALUES(?,1,200,NULL)',(T,)); db.db.commit()
    d.early_buyers=Mock(side_effect=ArcscanError('failed'))
    assert d.discover_from_winners()==0
    assert db.db.execute('SELECT processed_at FROM discovery_winners').fetchone()[0] is None


def test_pagination_limit_and_cycle(db):
    d=discovery(db); d.cfg=replace(Settings(),discovery_activity_pages=2)
    page=fixture('arcscan_live_1.json'); page['page'].update(next='repeat',has_more=True)
    d.arc=SimpleNamespace(address_activity=lambda *a,**k:page,next_cursor=ArcscanClient.next_cursor)
    with pytest.raises(ArcscanError): d.activity_hashes(W)
    d.cfg=replace(d.cfg,discovery_activity_pages=1)
    with pytest.raises(ArcscanError): d.activity_hashes(W)


def test_candidate_cannot_confirm_until_complete_promotion(db):
    assert db.wallet_eligible(W)
    db.upsert_discovery_candidate(W,T,1)
    assert not db.wallet_eligible(W)
    db.save_promoted_wallet(W,80,'SMART',3,'w')
    assert not db.wallet_eligible(W)
    db.db.execute('UPDATE discovery_candidates SET history_complete=1'); db.db.commit()
    assert db.wallet_eligible(W)
    db.db.execute('UPDATE promoted_wallets SET enabled=0'); db.db.commit()
    assert not db.wallet_eligible(W)


def test_candidate_overlap_is_not_double_counted_and_execution_price(db):
    launch(db); e=event(); db.add_event(e); db.add_research_trade(e)
    d=discovery(db)
    assert d._candidate_entries(W)[0]['usdc']==100
    profiler=WalletProfiler.__new__(WalletProfiler); profiler.db=db
    db.add_trade_context(e,MarketSnapshot(T,price_usd=999))
    assert profiler._launch_entries(W)[0]['entry_price']==10
    db.add_event(event(tx_hash='other',token_amount=1,usdc_amount=1000,trade_verified=False))
    assert profiler._launch_entries(W)[0]['usdc']==100


def worker(path):
    db=Storage(path)
    for _ in range(8): db.upsert_discovery_candidate(W,T,1)
    db.db.close()


def test_four_process_migration_and_source_race(tmp_path):
    path=str(tmp_path/'legacy.sqlite3')
    conn=sqlite3.connect(path); conn.executescript(SCHEMA+V4_SCHEMA); conn.close()
    with ProcessPoolExecutor(max_workers=4) as ex: list(ex.map(worker,[path]*4))
    db=Storage(path)
    assert db.db.execute('SELECT source_winners FROM discovery_candidates').fetchone()[0]==1
    assert db.db.execute('PRAGMA journal_mode').fetchone()[0]=='wal'
    assert db.db.execute('PRAGMA user_version').fetchone()[0]==1
    assert not db.wallet_eligible(W)
    db.db.close()


def test_snapshot_delay_and_candidate_fairness(db,monkeypatch):
    monkeypatch.setattr('storage.time.time',lambda:10000)
    launch(db)
    db.save_launch_snapshot(T,5,400,MarketSnapshot(T,price_usd=1))
    row=db.db.execute('SELECT * FROM launch_snapshots').fetchone()
    assert row['delay_seconds']==9600 and row['observed_at']==10000
    db.upsert_candidate(W,'test'); db.upsert_candidate(T,'test')
    db.mark_candidate_checked(W)
    assert db.due_candidates(0,168,1)[0]['token']==T


def test_identity_direct_funding_unknown_exchange_and_refresh():
    data={W:{'funded_by':POOL},T:{'funded_by':W}}
    g=IdentityGraph(SimpleNamespace(address_facts=lambda a:data.get(a,{})))
    g.refresh([W,T,ROUTER]); assert g.related(W,T) and not g.eligible(ROUTER)
    data[T]={'funded_by':ROUTER}; g.refresh([W,T]); assert not g.related(W,T)
    shared={'funded_by':{'address':POOL,'category':'exchange'}}
    data.update({W:shared,T:shared}); g.refresh([W,T])
    assert not g.related(W,T) and not g.eligible(W)


def test_watchlist_weight_reload_and_atomic_publication(tmp_path):
    base=tmp_path/'watch.json'; write_json(base,{'wallets':[{'address':W,'weight':1}]})
    m=WatchlistManager(base,tmp_path/'none',0); assert m.reload(True)
    write_json(base,{'wallets':[{'address':W,'weight':4}]})
    assert m.reload(True) and m.items[W].weight==4
    assert not list(tmp_path.glob('*.tmp'))


def test_paper_exits_are_once_and_bad_prices_ignored(db):
    db.open_position(T,'T',1,1,100,100)
    p=PaperTrader(db,Settings())
    market=SimpleNamespace(token_snapshot=lambda _:MarketSnapshot(T,price_usd=float('nan')))
    assert not p.evaluate_stops(market)
    market.token_snapshot=lambda _:MarketSnapshot(T,price_usd=0.5)
    assert len(p.evaluate_stops(market))==1
    assert not p.evaluate_stops(market)
    assert db.get_position(T)['pnl_usd']==-50
    assert not db.close_position(T,1,0.5,-50,'duplicate')


def test_scanner_receipt_retry_and_event_retry(db,monkeypatch):
    b=ArcSmartMoneyBot.__new__(ArcSmartMoneyBot); b.cfg=Settings(); b.db=db
    b.watch={W:WalletInfo(W,'w',3,'seed')}
    tx={'from':W,'hash':HexBytes(H),'value':0}
    b.rpc=SimpleNamespace(get_block=lambda *a,**k:{'timestamp':1000,'transactions':[tx]},get_receipt=Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(RuntimeError): b.process_block(1)
    assert not db.tx_seen(H)
    b.rpc.get_receipt=Mock(return_value={'status':1})
    monkeypatch.setattr('bot.classify_trade',lambda *a,**k:[{'side':'BUY','token':T,'symbol':'T','token_amount':1,'usdc_amount':100,'confirmation_eligible':True}])
    b.reputation=Mock(); b.reputation.get.return_value=SimpleNamespace(score=80)
    b.intelligence=SimpleNamespace(effective=lambda *a:{'weight':3,'score':80,'class':'SMART','eligible':True})
    b.identities=SimpleNamespace(identity_id=lambda _:W,eligible=lambda _:True)
    b.handle_event=Mock(side_effect=RuntimeError('market down'))
    with pytest.raises(RuntimeError): b.process_block(1)
    assert not db.tx_seen(H)
    b.handle_event=Mock(); b.process_block(1); b.process_block(1)
    assert b.handle_event.call_count==1 and db.tx_seen(H)
    assert db.db.execute('SELECT count(*) FROM events').fetchone()[0]==1
    db.checkpoint(2); assert db.next_block()==2


def test_rpc_rejects_wrong_chain():
    r=ArcRPC.__new__(ArcRPC); r.chain_id=5042; r.w3=SimpleNamespace(eth=SimpleNamespace(chain_id=5042002))
    with pytest.raises(RuntimeError): r.sanity_check()

def test_transfer_token_filter_nft_and_order(db):
    row={'tx_hash':H,'from':POOL,'to':W,'token':{'address':T,'standard':'erc20'},'block':10,'log_index':2}
    assert not extract_transfer_rows({'items':[row]},expected_token=ROUTER)
    nft=dict(row,token={'address':T,'standard':'erc721'})
    assert not extract_transfer_rows({'items':[nft]})
    assert not extract_tokens_from_transfers({'items':[nft]})
    d=discovery(db)
    page=fixture('arcscan_live_1.json'); page['items']=[row,dict(row,log_index=1,tx_hash='0x'+'b'*64)]
    d.arc=SimpleNamespace(token_transfers=lambda *a,**k:page,next_cursor=ArcscanClient.next_cursor)
    assert [r['log_index'] for r in d._all_transfer_rows(T)]==[1,2]
    del row['log_index']
    with pytest.raises(ArcscanError): d._all_transfer_rows(T)


def test_failed_hydration_receipt_keeps_quarantine(db):
    d=discovery(db); db.upsert_discovery_candidate(W,T,1)
    d.activity_hashes=lambda _:[H]
    d.rpc=SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=Mock(side_effect=RuntimeError('offline')))))
    with pytest.raises(ArcscanError): d.hydrate_wallet(W)
    row=db.db.execute('SELECT hydrated_at,history_complete FROM discovery_candidates').fetchone()
    assert tuple(row)==(None,0)


def test_insufficient_evaluable_profile_cannot_promote(db):
    d=discovery(db); db.upsert_discovery_candidate(W,T,1)
    db.db.execute('UPDATE discovery_candidates SET hydrated_at=1,history_complete=1,first_discovered_at=1,source_winners=3'); db.db.commit()
    p={'wallet':W,'generated_at':1,'smart_score':90,'profile_class':'WATCH','recommended_weight':4,'confirmation_eligible':True,'launches_bought':10,'opportunity_capture_rate':0.1,'evaluable_trades':1,'winners':1}
    d.profile_candidate=lambda _:p; d._funding_collision=lambda _:False; d.write_generated_watchlist=Mock()
    assert d.promote_candidates()==0
    assert 'insufficient_evaluable_history' in db.db.execute('SELECT promotion_reason FROM discovery_candidates').fetchone()[0]


def test_good_profile_promotes_only_after_gates(db):
    d=discovery(db); db.upsert_discovery_candidate(W,T,1)
    db.db.execute('UPDATE discovery_candidates SET hydrated_at=1,history_complete=1,first_discovered_at=1,source_winners=3'); db.db.commit()
    p={'wallet':W,'generated_at':1,'smart_score':80,'profile_class':'SMART','recommended_weight':3,'confirmation_eligible':True,'launches_bought':10,'opportunity_capture_rate':0.1,'evaluable_trades':8,'winners':3}
    d.profile_candidate=lambda _:p; d._funding_collision=lambda _:False; d.write_generated_watchlist=Mock()
    assert d.promote_candidates()==1 and db.wallet_eligible(W)


def test_launch_outage_does_not_stop_snapshots():
    from launch_indexer import LaunchIndexer
    x=LaunchIndexer.__new__(LaunchIndexer)
    x.discover=Mock(side_effect=ArcscanError('offline')); x.promote_candidates=Mock(return_value=0); x.snapshots=Mock(return_value=1)
    x.cycle(); x.snapshots.assert_called_once()


def test_snapshots_reject_missing_price_or_changed_pair(db):
    from launch_indexer import LaunchIndexer
    launch(db); db.db.execute('UPDATE launches SET pair_address=?',(POOL,)); db.db.commit()
    x=LaunchIndexer.__new__(LaunchIndexer); x.db=db
    x.market=SimpleNamespace(token_snapshot=lambda _:MarketSnapshot(T,pair_address=POOL))
    assert x.snapshots()==0
    x.market.token_snapshot=lambda _:MarketSnapshot(T,price_usd=1,pair_address=ROUTER)
    assert x.snapshots()==0
    x.market.token_snapshot=lambda _:MarketSnapshot(T,price_usd=1,pair_address=POOL)
    assert x.snapshots()==5


def test_buffered_wallet_removal_and_quarantine_cannot_trigger_validated(db):
    from models import RiskReport
    b=ArcSmartMoneyBot.__new__(ArcSmartMoneyBot); b.cfg=Settings(); b.db=db
    b.clusters=ClusterBook(1200,600); prior=event(); b.clusters.add(prior)
    b.watch={T:WalletInfo(T,'new',4,'seed')}
    b.identities=SimpleNamespace(identity_id=lambda a:a,eligible=lambda _:True)
    b.intelligence=SimpleNamespace(effective=lambda *a:{'weight':4,'eligible':True})
    b.risk=SimpleNamespace(inspect=lambda _:RiskReport(T,True,top10_concentration_pct=10,market=MarketSnapshot(T,price_usd=1,liquidity_usd=50000,market_cap_usd=100000,pair_created_at_ms=9999999999999,volume_m5=10000,volume_h1=12000)))
    b.tg=Mock(); b.paper=Mock(); b.paper.maybe_open.return_value=None; b.proposals=Mock()
    new=event(tx_hash='new',wallet=T,timestamp=1001,confirmation_eligible=False,trade_verified=False)
    b.handle_event(new)
    assert not prior.confirmation_eligible
    b.proposals.add.assert_not_called()


def test_history_lab_does_not_hide_log_holes():
    from history_lab import HistoryLab
    h=HistoryLab.__new__(HistoryLab); h._creation_block=lambda _:1
    h.rpc=SimpleNamespace(block_number=lambda:2,w3=SimpleNamespace(eth=SimpleNamespace(get_logs=Mock(side_effect=RuntimeError('pruned')))))
    assert h.earliest_transfer_logs(T,block_chunk=1000)==([], 'log_range_unavailable')

def test_legacy_migration_requires_revalidation_and_preserves_data(tmp_path):
    path=tmp_path/'legacy.sqlite3'; c=sqlite3.connect(path); c.executescript(SCHEMA+V4_SCHEMA)
    c.execute("INSERT INTO discovery_candidates(wallet,first_discovered_at,last_discovered_at,status,hydrated_at) VALUES(?,1,1,'PROMOTED',1)",(W,))
    c.execute("INSERT INTO promoted_wallets(wallet,promoted_at,smart_score,profile_class,weight,enabled) VALUES(?,1,90,'ELITE',4,1)",(W,))
    c.execute("INSERT INTO research_trades(tx_hash,wallet,side,token,token_amount,usdc_amount) VALUES(?,?,'BUY',?,1,999)",(H,W,T))
    c.commit(); c.close()
    db=Storage(path)
    row=db.db.execute('SELECT status,hydrated_at,history_complete FROM discovery_candidates').fetchone()
    assert tuple(row)==('QUARANTINE',None,0)
    assert db.db.execute('SELECT enabled FROM promoted_wallets').fetchone()[0]==0
    assert db.db.execute('SELECT trade_verified FROM research_trades').fetchone()[0]==0
    db.add_research_trade(event())
    row=db.db.execute('SELECT usdc_amount,trade_verified FROM research_trades').fetchone()
    assert tuple(row)==(100,1)
    db.db.close()


def test_unpriced_swap_cannot_be_complete_history():
    r=receipt([transfer(T,W,POOL,10**18),transfer(ROUTER,POOL,W,10**18)])
    with pytest.raises(ValueError): classify_trade(rpc(),r,W,25,strict=True)

def test_unavailable_snapshots_do_not_starve_other_tokens(db,monkeypatch):
    monkeypatch.setattr('storage.time.time',lambda:10000)
    launch(db,T); launch(db,W)
    first=db.launches_due_snapshot(horizons=(5,),limit=1)[0][0]['token']
    second=db.launches_due_snapshot(horizons=(5,),limit=1)[0][0]['token']
    assert first != second
