import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import Settings
from paper_copy.policy import Policy, size_buy
from paper_copy.store import Store
from paper_copy.selection import load_selection, SelectionError
from paper_copy.scanner import Scanner, ChainConsistencyError
from paper_copy.execution import Execution
from paper_copy.report import report
from rpc_transport import RPCError

W='0x'+'1'*40
W2='0x'+'2'*40
T='0x'+'3'*40
NOW=100000


@pytest.fixture
def cfg(tmp_path,monkeypatch):
    monkeypatch.setattr('paper_copy.scanner.time.time',lambda:NOW)
    return replace(Settings(),trusted_dex_registry=str(tmp_path/'registry.json'),
                   alchemy_webhook_enabled=False,
                   rpc_url='https://rpc.example.test',rpc_fallback_urls=(),
                   telegram_bot_token='',telegram_chat_id='')


@pytest.fixture
def ledger(tmp_path,cfg):
    policy=Policy()
    store=Store(tmp_path/'copy.sqlite3',policy)
    scanner=Scanner(store,None,policy,cfg)
    scanner.sync({w:entry() for w in (W,W2)},0)
    yield store
    store.close()


def entry():
    return {'baseline':['100']*10,'cluster':W,'snapshot_at':NOW-100,'expires_at':NOW+1000}


def order(store, identifier='buy',wallet=W,side='BUY',fraction=None,source_at=NOW,budget='50',block=1):
    with store.db:
        store.db.execute('''INSERT INTO observations
          (id,wallet,token,side,block,tx_index,source_at,detected_at,source_usdc,source_qty,sell_fraction,budget,sizing,status)
          VALUES(?,?,?,?,?,0,?,?, '100','10',?,?, '{}','PENDING')''',
          (identifier,wallet,T,side,block,source_at,NOW,fraction,budget))
    return dict(store.db.execute('SELECT * FROM observations WHERE id=?',(identifier,)).fetchone())


def market(price=1):
    return Mock(token_snapshot=Mock(return_value=SimpleNamespace(token=T,price_usd=price,
            liquidity_usd=1000000,volume_m5=100000,pair_address=W2)))


def cash(store,wallet=W):
    return Decimal(store.db.execute('SELECT cash FROM wallets WHERE wallet=?',(wallet,)).fetchone()[0])


@pytest.mark.parametrize('source,expected', [(1,'10.00'),(20,'10.00'),(50,'25.00'),(100,'50.00'),(200,'100.00'),(10000,'100.00')])
def test_causal_median_sizing_and_clamps(source,expected):
    result=size_buy(source,[100]*9+[100000],Policy())
    assert result['budget']==expected and result['baseline']=='100'
    assert result['confidence']=='HIGH'


def test_small_history_uses_minimum_and_cannot_claim_high_confidence():
    assert size_buy(1000,[1,100],Policy())=={'budget':'10.00','baseline':None,'samples':2,'confidence':'LOW'}


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),-1,True,0])
def test_invalid_trade_size_rejected(bad):
    with pytest.raises(ValueError): size_buy(bad,[100]*5,Policy())


def test_wallets_have_separate_cash_and_restart_does_not_reset(ledger,cfg):
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    ex.fill(order(ledger),NOW)
    assert cash(ledger)==950 and cash(ledger,W2)==1000
    Scanner(ledger,None,Policy(),cfg).sync({W:entry(),W2:entry()},200)
    assert cash(ledger)==950
    second=Store(ledger.path,Policy())
    assert cash(second)==950
    second.close()


def test_partial_sell_and_full_exit_preserve_accounting(ledger):
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    ex.fill(order(ledger),NOW)
    original=dict(ledger.db.execute('SELECT * FROM positions WHERE wallet=?',(W,)).fetchone())
    ex.market=market(2)
    ex.fill(order(ledger,'sell',side='SELL',fraction='.25'),NOW)
    pos=ledger.db.execute('SELECT * FROM positions WHERE wallet=?',(W,)).fetchone()
    assert Decimal(pos['quantity'])==Decimal(original['quantity'])*Decimal('.75')
    assert Decimal(pos['cost'])==Decimal('37.5')
    sell=dict(ledger.db.execute("SELECT * FROM fills WHERE observation_id='sell'").fetchone())
    assert Decimal(sell['realized_pnl'])==Decimal(sell['notional'])-Decimal(sell['fee'])-Decimal(sell['gas'])-Decimal('12.5')
    ex.fill(order(ledger,'sell-all',side='SELL',fraction='1'),NOW)
    assert ledger.db.execute('SELECT quantity,cost FROM positions').fetchone()[0]=='0'
    report_row=report(ledger.path)[0]
    assert abs(Decimal(report_row['realized_pnl'])-(cash(ledger)-1000)) < Decimal('1e-20')
    assert report_row['equity_market'] is None and not report_row['open_positions']


def test_same_order_never_filled_twice(ledger):
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    obs=order(ledger)
    ex.fill(obs,NOW); ex.fill(obs,NOW)
    assert cash(ledger)==950
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==1


def test_atomic_fill_rolls_back_cash_and_positions(ledger):
    obs=order(ledger)
    ledger.db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError): Execution(ledger,Policy(),market(),clock=lambda:NOW).fill(obs,NOW)
    assert cash(ledger)==1000
    assert ledger.db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]==0
    assert ledger.db.execute('SELECT status FROM observations').fetchone()[0]=='PENDING'


def test_cash_exhaustion_never_borrows_or_resets(ledger):
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    for n in range(11): ex.fill(order(ledger,str(n),budget='100'),NOW)
    assert cash(ledger)==0
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==10
    assert ledger.db.execute("SELECT reason FROM observations WHERE id='10'").fetchone()[0]=='INSUFFICIENT_CASH'


def test_missing_quote_blocks_later_orders_until_expiry(ledger):
    ex=Execution(ledger,Policy(),Mock(token_snapshot=Mock(return_value=None)),clock=lambda:NOW)
    order(ledger,'first',block=1); order(ledger,'second',block=2)
    ex.pending(NOW)
    assert cash(ledger)==1000
    assert ex.market.token_snapshot.call_count==1
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==0
    ex.pending(NOW+61)
    assert ledger.db.execute("SELECT COUNT(*) FROM observations WHERE status='SKIPPED'").fetchone()[0]==2


def test_slow_quote_cannot_fill_expired_signal(ledger):
    obs=order(ledger)
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW+61)
    ex.fill(obs,NOW)
    assert cash(ledger)==1000 and ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==0


def test_expired_selection_blocks_buys_but_still_copies_exits(ledger,cfg):
    ex=Execution(ledger,Policy(),market(),clock=lambda:NOW)
    ex.fill(order(ledger),NOW)
    Scanner(ledger,None,Policy(),cfg).sync({},2)
    ex.fill(order(ledger,'disabled-buy'),NOW)
    ex.fill(order(ledger,'exit',side='SELL',fraction='1'),NOW)
    assert ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==2
    assert ledger.db.execute("SELECT reason FROM observations WHERE id='disabled-buy'").fetchone()[0]=='SELECTION_NOT_AUTHORIZED'


def test_liquidity_and_wrong_token_quotes_do_not_invent_fills(ledger):
    m=market(); m.token_snapshot.return_value.liquidity_usd=1
    ex=Execution(ledger,Policy(),m,clock=lambda:NOW)
    ex.fill(order(ledger),NOW)
    assert ledger.db.execute('SELECT reason FROM observations').fetchone()[0]=='INSUFFICIENT_MARKET_CAPACITY'
    m.token_snapshot.return_value.token=W2
    ex.fill(order(ledger,'wrong-token'),NOW)
    assert cash(ledger)==1000 and ledger.db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]==0


def contract():
    return {'schema_version':1,'chain_id':5042,'generated_at':NOW,'purpose':'historical_selection_for_paper',
            'live_signal_authority':False,'requires_consumer_validation':True,'one_wallet_per_identity_cluster':True,
            'wallets':[{'wallet':W,'identity_cluster_id':W,'historical_class':'HISTORICALLY_QUALIFIED',
                       'paper_selection_eligible':True,'live_signal_authority':False,'infrastructure_excluded':False,
                       'identity_eligible':True,'data_complete':True,'snapshot_phase':'COMPLETE','blocking_reasons':[],
                       'snapshot_at':NOW-100,'expires_at':NOW+1000,
                       'buy_details':[{'timestamp':NOW-1000,'buy_usdc':100} for _ in range(10)]}]}


def test_selection_accepts_only_explicit_historical_paper_authority(tmp_path,cfg):
    path=tmp_path/'selection.json'; path.write_text(json.dumps(contract()))
    assert load_selection(path,cfg,NOW)[W]['baseline']==['100']*10


@pytest.mark.parametrize('field,value', [('historical_class','PROMISING'),('expires_at',NOW),
    ('data_complete',False),('identity_eligible',False),('snapshot_phase','FETCHING'),
    ('paper_selection_eligible',False),('live_signal_authority',True),('blocking_reasons',['incomplete'])])
def test_selection_fail_closed(tmp_path,cfg,field,value):
    data=contract(); data['wallets'][0][field]=value
    path=tmp_path/'selection.json'; path.write_text(json.dumps(data))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)


def test_selection_rejects_infrastructure_duplicates_and_future_baseline(tmp_path,cfg):
    path=tmp_path/'selection.json'
    data=contract(); data['wallets']*=2; path.write_text(json.dumps(data))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)
    data=contract(); data['wallets'][0]['buy_details'][0]['timestamp']=NOW+1; path.write_text(json.dumps(data))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)
    path.write_text(json.dumps(contract()))
    (tmp_path/'registry.json').write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)


def test_different_policy_or_analyzer_database_cannot_be_reused(ledger,tmp_path):
    with pytest.raises(ValueError): Store(ledger.path,replace(Policy(),normal_buy='40'))
    path=tmp_path/'analyzer.sqlite3'
    db=sqlite3.connect(path); db.execute('CREATE TABLE discovery_candidates(wallet TEXT)'); db.close()
    with pytest.raises(ValueError): Store(path,Policy())
    db=sqlite3.connect(path)
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}=={'discovery_candidates'}
    db.close()


def fake_rpc():
    def header(n,*args):
        return {'number':n,'hash':n.to_bytes(32,'big'),'parentHash':max(0,n-1).to_bytes(32,'big'),'timestamp':NOW}
    eth=SimpleNamespace(get_logs=Mock(return_value=[]),get_transaction=Mock(),contract=Mock())
    return SimpleNamespace(block_number=Mock(return_value=5),get_block=Mock(side_effect=header),
                           get_receipt=Mock(),w3=SimpleNamespace(eth=eth,provider=SimpleNamespace(active=0)))


def checkpoint(store,number=1):
    with store.db:
        store.set('next_block',number)
        store.set('last_hash',(number-1).to_bytes(32,'big').hex())


def test_scanner_starts_live_without_replaying_analyzer_history(ledger,cfg):
    rpc=fake_rpc(); scanner=Scanner(ledger,rpc,Policy(),cfg)
    scanner.cycle({W:entry()})
    assert ledger.get('next_block')=='4'
    rpc.w3.eth.get_logs.assert_not_called()


def test_range_queries_reduce_calls_and_checkpoint_every_block(ledger,cfg):
    rpc=fake_rpc(); checkpoint(ledger)
    scanner=Scanner(ledger,rpc,Policy(),cfg)
    callbacks=[]
    assert scanner.cycle({W:entry()},on_block=lambda:callbacks.append(ledger.get('next_block')))==3
    assert rpc.w3.eth.get_logs.call_count==2
    assert callbacks==['2','3','4']
    assert rpc.w3.eth.get_logs.call_args.args[0]['fromBlock']==1
    assert rpc.w3.eth.get_logs.call_args.args[0]['toBlock']==3


def test_rpc_failure_does_not_checkpoint_and_resume_skips_no_block(ledger,cfg):
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.w3.eth.get_logs.side_effect=[[],RPCError('eth_getLogs','429',3,'primary')]
    scanner=Scanner(ledger,rpc,Policy(),cfg)
    with pytest.raises(RPCError): scanner.cycle({W:entry()})
    assert ledger.get('next_block')=='1'
    rpc.w3.eth.get_logs.side_effect=None
    assert scanner.cycle({W:entry()})==3 and ledger.get('next_block')=='4'
    assert scanner.cycle({W:entry()})==0


@pytest.mark.parametrize('count',[1,10,11,40,43])
def test_alchemy_free_range_is_split_inclusive_without_gaps(ledger,cfg,count):
    from pathlib import Path
    limit=json.loads((Path(__file__).parent/'fixtures/alchemy_free_log_range.json').read_text())
    rpc=fake_rpc()
    def limited(query):
        if query['toBlock']-query['fromBlock']+1 > limit['accepted_blocks']:
            raise RPCError('eth_getLogs',str(limit['http_status']),1,'primary')
        return []
    rpc.w3.eth.get_logs.side_effect=limited
    assert Scanner(ledger,rpc,Policy(),cfg).participants(100,99+count,[W])=={}
    queries=[c.args[0] for c in rpc.w3.eth.get_logs.call_args_list]
    for position in (1,2):
        side=[q for q in queries if len(q['topics'])==position+1]
        covered=[n for q in side for n in range(q['fromBlock'],q['toBlock']+1)]
        assert covered==list(range(100,100+count))


@pytest.mark.parametrize('category',['400','502'])
def test_later_subrange_failure_preserves_checkpoint_and_order_on_resume(ledger,cfg,category):
    rpc=fake_rpc(); rpc.block_number.return_value=42; checkpoint(ledger)
    rpc.w3.eth.get_logs.side_effect=[[],[],[],RPCError('eth_getLogs',category,1,'primary')]
    scanner=Scanner(ledger,rpc,Policy(),cfg)
    callbacks=[]
    with pytest.raises(RPCError):
        scanner.cycle({W:entry()},on_block=lambda:callbacks.append(ledger.get('next_block')))
    assert ledger.get('next_block')=='1' and callbacks==[]
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    rpc.w3.eth.get_logs.side_effect=None
    assert scanner.cycle({W:entry()},on_block=lambda:callbacks.append(ledger.get('next_block')))==40
    assert callbacks==[str(n) for n in range(2,42)]
    assert ledger.get('next_block')=='41'
    assert scanner.cycle({W:entry()})==0
    assert cash(ledger)==1000  # Restart does not fund an existing account again.


def test_chunk_boundaries_keep_logs_and_deduplicate_both_transfer_sides(ledger,cfg):
    from rpc import TRANSFER_TOPIC
    rpc=fake_rpc()
    def logs(query):
        return [{'blockNumber':n,'blockHash':n.to_bytes(32,'big'),
                 'transactionHash':n.to_bytes(32,'big'),
                 'topics':[TRANSFER_TOPIC,'0x'+'0'*24+W[2:],'0x'+'0'*24+W2[2:]]}
                for n in (10,11) if query['fromBlock']<=n<=query['toBlock']]
    rpc.w3.eth.get_logs.side_effect=logs
    result=Scanner(ledger,rpc,Policy(),cfg).participants(1,20,[W,W2])
    assert len(result)==2
    for n in (10,11):
        assert result[(n,n.to_bytes(32,'big').hex())]['wallets']=={W,W2}


def test_log_outside_its_subrange_is_rejected_even_inside_full_window(ledger,cfg):
    rpc=fake_rpc()
    rpc.w3.eth.get_logs.return_value=[{'blockNumber':11,'topics':[]}]
    with pytest.raises(ChainConsistencyError,match='Invalid log range'):
        Scanner(ledger,rpc,Policy(),cfg).participants(1,40,[W])


def test_reorganization_halts_without_modifying_paper_accounts(ledger,cfg):
    rpc=fake_rpc(); checkpoint(ledger)
    with ledger.db: ledger.set('last_hash','bad')
    with pytest.raises(ChainConsistencyError): Scanner(ledger,rpc,Policy(),cfg).cycle({W:entry()})
    assert ledger.get('next_block')=='1' and cash(ledger)==1000


def test_block_stages_all_receipts_before_writing(ledger,cfg):
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.get_receipt.side_effect=RPCError('eth_getTransactionReceipt','timeout',3,'primary')
    entries=[('a'*64,{'wallets':{W},'hash':(1).to_bytes(32,'big').hex()})]
    with pytest.raises(RPCError): Scanner(ledger,rpc,Policy(),cfg).block(1,entries)
    assert ledger.get('next_block')=='1'
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0


def attach_transactions(rpc, receipts, from_wallet=W):
    txs={}
    for index,r in enumerate(receipts):
        h=(index+10).to_bytes(32,'big')
        r.update(transactionHash=h,blockHash=(1).to_bytes(32,'big'),blockNumber=1,transactionIndex=index,status=1)
        txs['0x'+h.hex()]={'hash':h,'blockHash':r['blockHash'],'blockNumber':1,'from':from_wallet,'value':0}
    by_hash={'0x'+r['transactionHash'].hex():r for r in receipts}
    rpc.get_receipt.side_effect=lambda h:by_hash[h]
    rpc.w3.eth.get_transaction.side_effect=lambda h:txs[h]
    return [(r['transactionHash'].hex(),{'wallets':{W},'hash':r['blockHash'].hex()}) for r in receipts]


def test_real_decoder_to_paper_fill_and_deduplicated_restart(ledger,cfg):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:True
    buy=receipt([transfer(USDC_ERC20,W,POOL,200*10**6),transfer(T,POOL,W,10*10**18)])
    entries=attach_transactions(rpc,[buy])
    Scanner(ledger,rpc,Policy(),cfg).block(1,entries)
    obs=dict(ledger.db.execute('SELECT * FROM observations').fetchone())
    assert obs['side']=='BUY' and Decimal(obs['budget'])==100
    assert json.loads(obs['sizing'])['baseline']=='100'
    baseline=json.loads(ledger.db.execute('SELECT baseline FROM wallets WHERE wallet=?',(W,)).fetchone()[0])
    assert len(baseline)==11 and Decimal(baseline[-1])==200
    ex=Execution(ledger,Policy(),market(3),clock=lambda:NOW)
    ex.pending(NOW)
    assert cash(ledger)==900
    fill=ledger.db.execute('SELECT * FROM fills').fetchone()
    assert Decimal(fill['price'])>3  # Not the source's 200/10 = $20 execution.
    assert Decimal(fill['price'])<4
    checkpoint(ledger)  # Explicit replay in test must still be idempotent.
    Scanner(ledger,rpc,Policy(),cfg).block(1,entries)
    ex.pending(NOW)
    assert cash(ledger)==900
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==1
    assert len(json.loads(ledger.db.execute('SELECT baseline FROM wallets WHERE wallet=?',(W,)).fetchone()[0]))==11


def test_sell_fraction_includes_same_block_incoming_transfers(ledger,cfg):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:True
    rpc.w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value=100*10**18
    incoming=receipt([transfer(T,W2,W,100*10**18)],False)
    sell=receipt([transfer(T,W,POOL,50*10**18),transfer(USDC_ERC20,POOL,W,100*10**6)])
    entries=attach_transactions(rpc,[incoming,sell])
    Scanner(ledger,rpc,Policy(),cfg).block(1,list(reversed(entries)))
    obs=ledger.db.execute('SELECT * FROM observations').fetchone()
    assert obs['side']=='SELL' and Decimal(obs['sell_fraction'])==Decimal('.25')
    rpc.w3.eth.contract.return_value.functions.balanceOf.return_value.call.assert_called_once_with(block_identifier=0)


def test_multiple_source_sales_same_block_use_remaining_source_balance(ledger,cfg):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:True
    rpc.w3.eth.contract.return_value.functions.balanceOf.return_value.call.return_value=100*10**18
    receipts=[receipt([transfer(T,W,POOL,q*10**18),transfer(USDC_ERC20,POOL,W,100*10**6)]) for q in (50,25)]
    entries=attach_transactions(rpc,receipts)
    Scanner(ledger,rpc,Policy(),cfg).block(1,entries)
    assert [Decimal(r[0]) for r in ledger.db.execute('SELECT sell_fraction FROM observations ORDER BY tx_index')]==[Decimal('.5')]*2


def test_unknown_emitter_never_generates_copy_order(ledger,cfg):
    from test_audit_regressions import transfer,receipt,POOL
    from rpc import USDC_ERC20
    rpc=fake_rpc(); checkpoint(ledger)
    rpc.token_meta=lambda _: {'decimals':18,'symbol':'TEST'}
    rpc.swap_emitter_trusted=lambda *a:False
    entries=attach_transactions(rpc,[receipt([transfer(T,POOL,W,10**18),transfer(USDC_ERC20,W,POOL,100*10**6)])])
    Scanner(ledger,rpc,Policy(),cfg).block(1,entries)
    assert ledger.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    assert ledger.db.execute('SELECT reason FROM gaps').fetchone()[0]=='UNTRUSTED_SWAP_NOT_COPIED'


def test_selection_reloads_without_resetting_baseline_or_initial_cash(ledger,cfg):
    with ledger.db:
        ledger.db.execute('UPDATE wallets SET baseline=?,cash=? WHERE wallet=?',(json.dumps(['50']*5),'123',W))
    scanner=Scanner(ledger,None,Policy(),cfg)
    scanner.sync({},100)
    scanner.sync({W:entry()},101)
    row=ledger.db.execute('SELECT * FROM wallets WHERE wallet=?',(W,)).fetchone()
    assert row['cash']=='123' and json.loads(row['baseline'])==['50']*5
    assert row['started_block']==1
    assert row['buy_after_block']==102


def test_reactivation_does_not_fill_pending_buys_from_before_renewal(ledger,cfg):
    obs=order(ledger)
    Scanner(ledger,None,Policy(),cfg).sync({},100)
    Scanner(ledger,None,Policy(),cfg).sync({W:entry()},101)
    Execution(ledger,Policy(),market(),clock=lambda:NOW).fill(obs,NOW)
    assert cash(ledger)==1000
    assert ledger.db.execute('SELECT reason FROM observations').fetchone()[0]=='BUY_BEFORE_AUTHORIZATION'


def test_expired_previous_authorization_moves_buy_start_to_live_head(ledger,cfg):
    expired=entry(); expired['expires_at']=NOW-1
    with ledger.db:
        ledger.db.execute('UPDATE wallets SET selection=? WHERE wallet=?',(json.dumps(expired),W))
    Scanner(ledger,None,Policy(),cfg).sync({W:entry()},200)
    row=ledger.db.execute('SELECT * FROM wallets WHERE wallet=?',(W,)).fetchone()
    assert row['buy_after_block']==201 and row['started_block']==1


def test_cli_empty_selection_uses_no_rpc_and_separate_database(tmp_path,monkeypatch,cfg):
    import paper_copy.__main__ as cli
    path=tmp_path/'selection.json'
    data=contract(); data['wallets']=[]; path.write_text(json.dumps(data))
    monkeypatch.setattr(cli,'SETTINGS',cfg)
    monkeypatch.setattr(cli.time,'time',lambda:NOW)
    rpc=Mock(side_effect=AssertionError('No RPC for empty selection'))
    monkeypatch.setattr(cli,'ArcRPC',rpc)
    db=tmp_path/'copy.sqlite3'
    assert cli.main(['--selection',str(path),'--db',str(db),'--once'])==0
    rpc.assert_not_called()
    assert cli.main(['--db',str(db),'--status'])==0


def test_persisted_chain_halt_cannot_be_cleared_by_restart(ledger,monkeypatch):
    import paper_copy.__main__ as cli
    with ledger.db: ledger.set('halt_reason','CHAIN_CONSISTENCY_REQUIRES_REVIEW')
    rpc=Mock(side_effect=AssertionError('No RPC after halt'))
    monkeypatch.setattr(cli,'ArcRPC',rpc)
    assert cli.main(['--db',str(ledger.path),'--once'])==2
    rpc.assert_not_called()
