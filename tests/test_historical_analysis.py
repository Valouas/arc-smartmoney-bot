import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arcscan import ArcscanError
from config import Settings
from historical_metrics import analyze, qualify, performance_stage
from historical_store import initialize
from historical_collector import HistoricalCollector, parse_page
from historical_report import build_report, publish
from storage import Storage

W = '0x'+'1'*40
W2 = '0x'+'2'*40
T = '0x'+'3'*40
H = '0x'+'a'*64
NOW = 100000


@pytest.fixture
def cfg(tmp_path):
    return replace(Settings(), trusted_dex_registry=str(tmp_path/'registry.json'),
                   rpc_url='https://rpc.example.test', rpc_fallback_urls=(),
                   telegram_bot_token='', telegram_chat_id='',
                   historical_analysis_enabled=True, historical_transactions_per_step=1)


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path/'test.sqlite3')
    s.upsert_discovery_candidate(W, T, 1)
    initialize(s.db)
    yield s
    s.db.close()


def trade(n, side, cash, qty=1, token=T, ts=None, gas=0):
    return {'tx_hash': '0x'+f'{n:064x}', 'block': n, 'index': 0,
            'timestamp': ts if ts is not None else n*1000, 'gas_usdc': str(gas),
            'trades': [{'token': token, 'side': side, 'token_amount': qty,
                        'usdc_amount': cash, 'confirmation_eligible': True}]}


def page(hashes, nxt=None):
    return {'items': [{'tx_hash': h} for h in hashes], 'page': {'next': nxt, 'has_more': nxt is not None,
        'coverage': {'indexed': True, 'from_block': 0, 'to_block': 100,
                     'last_contiguous_block': 100, 'gaps': [], 'gap_blocks': 0}}}


def test_capital_allocation_dominates_raw_hit_rate(cfg):
    big = [trade(1,'BUY',1000),trade(2,'SELL',3000)]
    equal = [trade(1,'BUY',1),trade(2,'SELL',3)]
    for n in range(10):
        token = '0x'+f'{n+100:040x}'
        loss = [trade(3+n*2,'BUY',1,token=token),trade(4+n*2,'SELL',0.01,token=token)]
        big += loss; equal += loss
    a,b=analyze(big,cfg),analyze(equal,cfg)
    assert a['profitable_positions']==b['profitable_positions']==1
    assert a['capital_weighted_hit_rate'] > .99
    assert a['capital_weighted_return'] > 1
    assert b['capital_weighted_return'] < 0
    assert a['concentration_risk']=='HIGH'
    assert len(a['buy_details'])==11
    assert a['buy_details'][0]['capital_share'] > .99


def test_fifo_partial_sales_open_inventory_and_gas(cfg):
    rows=[trade(1,'BUY',100,qty=10,gas=1),trade(2,'BUY',200,qty=10,gas=1),
          trade(3,'SELL',225,qty=15,gas=1)]
    p=analyze(rows,cfg)
    assert p['gross_realized_pnl_usdc']==25
    assert p['realized_less_all_observed_gas_usdc']==22
    assert p['open_positions'][0]['cost_basis_usdc']==100
    assert p['open_positions'][0]['market_value_usdc'] is None
    assert p['conservative_pnl_usdc']==-78
    assert p['closed_positions']==0
    p=analyze(rows+[trade(4,'SELL',110,qty=5,gas=1)],cfg)
    assert p['closed_positions']==1 and not p['open_positions']
    assert p['conservative_pnl_usdc']==31
    assert p['closed_position_details'][0]['profit_usdc']==31


def test_duplicate_transactions_do_not_change_pnl(cfg):
    rows=[trade(1,'BUY',1),trade(2,'SELL',2)]
    assert analyze(rows,cfg)==analyze(rows+rows,cfg)


def test_missing_basis_and_external_inventory_remain_incomplete(cfg):
    r=trade(1,'SELL',100); r['inventory_gap']=True; r['gas_usdc']=None
    p=analyze([r],cfg)
    assert set(p['issues'])=={'sell_without_complete_cost_basis','external_or_unexplained_token_movement','gas_cost_unknown'}
    assert p['sale_proceeds_usdc']==0


def evidence(cfg):
    rows=[]
    for n in range(8):
        token='0x'+f'{n+100:040x}'
        rows += [trade(n*2+1,'BUY',10,token=token,ts=1000+n*6000),
                 trade(n*2+2,'SELL',12,token=token,ts=2000+n*6000)]
    return rows


def test_qualifies_from_history_without_any_forward_evidence(cfg):
    p=analyze(evidence(cfg),cfg)
    run={'version':1,'phase':'COMPLETE','cutoff_at':NOW}
    identity={'eligible':True,'independent_profitable_deployers':2}
    assert qualify(p,run,identity,False,cfg,NOW)==('HISTORICALLY_QUALIFIED',[])
    category,reasons=qualify(p,run,identity,True,cfg,NOW)
    assert category=='EXCLUDED'
    category,reasons=qualify(p,run,{},False,cfg,NOW)
    assert category=='PROMISING' and 'identity_unknown_or_shared_service' in reasons


@pytest.mark.parametrize('change,reason', [
    ({'phase':'FETCHING'},'historical_snapshot_incomplete'),
    ({'version':0},'historical_snapshot_incomplete'),
    ({'error':'outage'},'historical_collection_error'),
    ({'cutoff_at':1},'historical_snapshot_stale'),
])
def test_incomplete_or_stale_history_cannot_qualify(cfg,change,reason):
    run={'version':1,'phase':'COMPLETE','cutoff_at':NOW}; run.update(change)
    _,reasons=qualify(analyze(evidence(cfg),cfg),run,{'eligible':True,'independent_profitable_deployers':2},False,cfg,NOW+100000)
    assert reason in reasons


def test_single_winner_and_few_trades_stay_followed(cfg):
    p=analyze([trade(1,'BUY',1000),trade(2,'SELL',10000)],cfg)
    c,reasons=qualify(p,{'version':1,'phase':'COMPLETE','cutoff_at':NOW},
                      {'eligible':True,'independent_profitable_deployers':2},False,cfg,NOW)
    assert c=='WATCH' and 'profit_concentrated_in_one_position' in reasons
    assert performance_stage(p,cfg)=='PRELIMINARY_POSITIVE'


@pytest.mark.parametrize('mutation', [
    lambda p:p['page'].update(has_more=False),
    lambda p:p['page'].update(next=None),
    lambda p:p['page'].update(capped=True),
    lambda p:p.update(truncated=True),
    lambda p:p['page']['coverage'].update(to_block=50),
    lambda p:p['items'][0].clear(),
])
def test_page_guards(mutation):
    p=page([H],'next'); mutation(p)
    with pytest.raises(ArcscanError): parse_page(p,99,[])


def fake_collector(store,cfg):
    rpc=SimpleNamespace(block_number=lambda:102,get_block=lambda *a,**k:{'hash':b'a'*32,'timestamp':NOW})
    arc=SimpleNamespace(address_activity=Mock(return_value=page([H])))
    return HistoricalCollector(store,rpc,arc,cfg)


def test_pagination_resume_failure_and_cycle_preserve_state(store,cfg):
    c=fake_collector(store,cfg)
    c.arc.address_activity.return_value=page([H],'next')
    c.step(W,NOW)
    c=HistoricalCollector(store,c.rpc,c.arc,cfg)
    c.arc.address_activity.side_effect=ArcscanError('outage')
    with pytest.raises(ArcscanError): c.step(W,NOW+1)
    assert store.db.execute('SELECT cursor FROM historical_runs').fetchone()[0]=='next'
    c.arc.address_activity.side_effect=None
    c.arc.address_activity.return_value=page([H],'next')
    with pytest.raises(ArcscanError): c.step(W,NOW+2)
    assert store.db.execute('SELECT COUNT(*) FROM historical_transactions').fetchone()[0]==1


def test_reconstruction_resume_and_no_forward_authority(store,cfg):
    c=fake_collector(store,cfg)
    c.step(W,NOW)
    c.transaction=Mock(side_effect=ArcscanError('outage'))
    with pytest.raises(ArcscanError): c.step(W,NOW+1)
    assert store.db.execute('SELECT payload FROM historical_transactions').fetchone()[0] is None
    c.transaction=Mock(return_value=trade(1,'BUY',1))
    c.identity=Mock(return_value={})
    c.step(W,NOW+2)
    assert store.db.execute('SELECT phase FROM historical_runs').fetchone()[0]=='COMPLETE'
    assert store.db.execute('SELECT COUNT(*) FROM historical_transactions').fetchone()[0]==1
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0]==0
    assert store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert store.db.execute('SELECT COUNT(*) FROM scanner_state').fetchone()[0]==0


def test_report_exports_only_qualified_diverse_paper_wallets(store,cfg,tmp_path):
    for wallet in (W,W2):
        store.upsert_discovery_candidate(wallet,T,1)
        identity={'eligible':True,'members':[W,W2],'cluster':W,'independent_profitable_deployers':2}
        store.db.execute('INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,identity) VALUES(?,1,?,100,?,\'hash\',\'COMPLETE\',?)',
                         (wallet,NOW,NOW,json.dumps(identity)))
        for row in evidence(cfg):
            store.db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(wallet,row['tx_hash'],json.dumps(row)))
    store.db.commit()
    before=store.db.total_changes
    rows=build_report(store.path,cfg,NOW)
    assert len(rows)==2 and all(r['paper_selection_eligible'] for r in rows)
    payload=publish(rows,cfg,tmp_path,now=NOW)
    assert len(payload['wallets'])==1 and payload['live_signal_authority'] is False
    assert payload['wallets'][0]['expires_at']>NOW
    assert rows[0]['identity_cluster_size']==2
    assert store.db.total_changes==before
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0]==0


def test_known_infrastructure_excluded_and_smart_account_not_blanket_excluded(store,cfg,tmp_path):
    rows=build_report(store.path,cfg,NOW)
    assert rows[0]['historical_class']=='WATCH'  # No EOA/get_code requirement.
    (tmp_path/'registry.json').write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    rows=build_report(store.path,cfg,NOW)
    assert rows[0]['historical_class']=='EXCLUDED'


def test_historical_mode_does_not_run_forward_promotion(store,cfg):
    from auto_discovery import AutoDiscovery
    d=AutoDiscovery.__new__(AutoDiscovery); d.cfg=cfg
    d.detect_winners=Mock(return_value=1); d.discover_from_winners=Mock(return_value=1)
    d.hydrate_candidates=Mock(); d.promote_candidates=Mock()
    d.cycle()
    d.hydrate_candidates.assert_not_called(); d.promote_candidates.assert_not_called()


def test_small_trade_collected_with_same_trusted_dex_decoder(store,cfg):
    from test_audit_regressions import transfer, receipt, rpc, POOL
    from rpc import USDC_ERC20
    c=fake_collector(store,cfg)
    small=receipt([transfer(USDC_ERC20,W,POOL,10**6),transfer(T,POOL,W,10**18)])
    small.update(blockHash=b'a'*32,blockNumber=1,transactionIndex=2,gasUsed=21000,effectiveGasPrice=10**9)
    transport=rpc()
    transport.w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=lambda h:{'from':W,'value':0,'blockNumber':1,'blockHash':b'a'*32}))
    transport.get_receipt=lambda _:small
    transport.get_block=c.rpc.get_block
    c.rpc=transport
    data=c.transaction(W,H,{'cutoff_block':100})
    assert data['trades'][0]['usdc_amount']==1
    assert data['trades'][0]['confirmation_eligible'] is True
    assert float(data['gas_usdc'])==0.000021
    assert not data['inventory_gap']
    small['logs']=small['logs'][:-1]  # The swap emitter proof is still required.
    assert c.transaction(W,H,{'cutoff_block':100})['error']=='unsupported_or_ambiguous_reconstruction'


def test_failed_start_rotates_to_other_wallet(store,cfg):
    store.upsert_discovery_candidate(W2,T,1)
    cfg=replace(cfg,discovery_max_wallets_per_cycle=1)
    c=fake_collector(store,cfg)
    c.rpc.block_number=Mock(side_effect=ArcscanError('offline'))
    assert c.cycle()['incomplete_steps']==1
    attempted=store.db.execute('SELECT wallet FROM historical_runs').fetchone()[0]
    c.rpc.block_number=lambda:102
    c.cycle()
    assert store.db.execute('SELECT COUNT(*) FROM historical_runs').fetchone()[0]==2
    assert store.db.execute('SELECT version FROM historical_runs WHERE wallet=?',(attempted,)).fetchone()[0]==0


def test_reorganization_invalidates_snapshot(store,cfg):
    c=fake_collector(store,cfg); c.step(W,NOW)
    c.transaction=Mock(return_value=trade(1,'BUY',1))
    c.rpc.get_block=lambda *a,**k:{'hash':b'b'*32,'timestamp':NOW}
    with pytest.raises(ArcscanError): c.step(W,NOW+1)
    assert store.db.execute('SELECT version FROM historical_runs').fetchone()[0]==0
    rows=build_report(store.path,cfg,NOW)
    assert not rows[0]['paper_selection_eligible']


def test_migration_preserves_legacy_data_and_does_not_trust_old_complete_flag(store,cfg):
    store.db.execute('UPDATE discovery_candidates SET history_complete=1,hydrated_at=?',(NOW,))
    store.db.commit()
    initialize(store.db); initialize(store.db)
    rows=build_report(store.path,cfg,NOW)
    assert rows[0]['snapshot_phase']=='NOT_STARTED'
    assert 'historical_snapshot_incomplete' in rows[0]['blocking_reasons']
    assert store.db.execute('SELECT history_complete FROM discovery_candidates').fetchone()[0]==1


def test_unknown_profitable_deployer_prevents_qualification(cfg):
    category,reasons=qualify(analyze(evidence(cfg),cfg),{'version':1,'phase':'COMPLETE','cutoff_at':NOW},
        {'eligible':True,'independent_profitable_deployers':2,'unknown_profitable_deployers':1},False,cfg,NOW)
    assert category=='PROMISING' and 'profitable_deployer_identity_unknown' in reasons


def test_zero_or_negative_realized_pnl_never_removes_wallet(cfg):
    category,_=qualify(analyze([trade(1,'BUY',100),trade(2,'SELL',1)],cfg),{}, {},False,cfg,NOW)
    assert category=='WATCH'


def test_tiny_token_quantities_do_not_discard_remaining_cost_basis(cfg):
    p=analyze([trade(1,'BUY',1000,qty='1e-24'),trade(2,'SELL',200,qty='1e-25')],cfg)
    assert p['closed_positions']==0
    assert p['open_positions'][0]['cost_basis_usdc']==900
    assert p['gross_realized_pnl_usdc']==100


@pytest.mark.parametrize('message,count', [('database is locked',5),('disk I/O error',1)])
def test_sqlite_startup_retries_only_lock_errors(tmp_path,monkeypatch,message,count):
    import sqlite3
    import storage
    connection=Mock()
    connection.execute.side_effect=sqlite3.OperationalError(message)
    monkeypatch.setattr(storage.sqlite3,'connect',lambda *a,**k:connection)
    sleep=Mock(); monkeypatch.setattr(storage.time,'sleep',sleep)
    with pytest.raises(sqlite3.OperationalError): Storage(tmp_path/'startup.sqlite3')
    assert connection.execute.call_count==count
    assert sleep.call_count==count-1
    connection.close.assert_called_once()


def test_snapshot_uses_explorer_coverage_when_behind_rpc(store,cfg):
    c=fake_collector(store,cfg)
    c.arc.address_activity.return_value['page']['coverage'].update(to_block=80,last_contiguous_block=80)
    c.step(W,NOW)
    assert store.db.execute('SELECT cutoff_block FROM historical_runs').fetchone()[0]==80


def test_incoming_funding_does_not_create_unknown_gas_for_recipient(store,cfg):
    from test_audit_regressions import transfer, receipt
    from rpc import USDC_ERC20
    c=fake_collector(store,cfg)
    r=receipt([transfer(USDC_ERC20,W2,W,10**6)],False)
    r.update(blockHash=b'a'*32,blockNumber=1,transactionIndex=0)
    c.rpc.w3=SimpleNamespace(eth=SimpleNamespace(get_transaction=lambda h:{'from':W2,'value':0,'blockNumber':1,'blockHash':b'a'*32}))
    c.rpc.get_receipt=lambda _:r
    payload=c.transaction(W,H,{'cutoff_block':100})
    assert payload['gas_usdc']=='0' and not payload['trades'] and not payload['inventory_gap']


def seed_run(store, wallet, phase='DECODING', payloads=(), version=1, error=None):
    store.upsert_discovery_candidate(wallet,T,1)
    store.db.execute('''INSERT INTO historical_runs
        (wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,last_attempt,error)
        VALUES(?,?,?,100,?,'hash',?,?,?)''', (wallet,version,NOW,NOW,phase,NOW,error))
    for payload in payloads:
        store.db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',
                         (wallet,payload['tx_hash'],json.dumps(payload)))
    store.db.commit()


@pytest.mark.parametrize('limit', [1,4])
def test_scheduler_balances_decoding_pagination_and_new_wallets_across_restarts(store,cfg,monkeypatch,limit):
    cfg=replace(cfg,discovery_max_wallets_per_cycle=limit)
    wallets=['0x'+f'{n:040x}' for n in range(10,18)]
    for wallet,phase in zip(wallets, ['DECODING']*4+['FETCHING']*2):
        seed_run(store,wallet,phase)
    for wallet in wallets[6:]: store.upsert_discovery_candidate(wallet,T,1)
    clock=iter(range(NOW+1,NOW+100))
    monkeypatch.setattr('historical_collector.time.time',lambda: next(clock))
    seen=[]
    def step(wallet,now):
        row=store.db.execute('SELECT phase FROM historical_runs WHERE wallet=?',(wallet,)).fetchone()
        seen.append((wallet,row[0] if row else 'START'))
        if row:
            with store.db:
                store.db.execute('UPDATE historical_runs SET last_attempt=? WHERE wallet=?',(now,wallet))
        else:
            # Failure remains in the start/refresh queue, behind other new wallets.
            raise ArcscanError('transient')
    for _ in range(8//limit):
        c=fake_collector(store,cfg)  # Reinitialize exactly as a restarted worker does.
        c.step=step
        c.cycle()
    assert [phase for _,phase in seen]==['DECODING','FETCHING','DECODING','START']*2
    assert len({w for w,p in seen if p=='DECODING'})==4
    assert len({w for w,p in seen if p=='FETCHING'})==2
    assert len({w for w,p in seen if p=='START'})==2
    assert len({w for w,_ in seen})==8


def test_ready_history_finishes_before_starting_every_new_wallet(store,cfg):
    cfg=replace(cfg,discovery_max_wallets_per_cycle=2)
    c=fake_collector(store,cfg); c.step(W,NOW)
    for n in range(10,30): store.upsert_discovery_candidate('0x'+f'{n:040x}',T,1)
    c.transaction=Mock(return_value=trade(1,'BUY',1)); c.identity=Mock(return_value={})
    result=c.cycle()
    assert result=={'wallet_steps':2,'incomplete_steps':0}
    assert store.db.execute('SELECT phase FROM historical_runs WHERE wallet=?',(W,)).fetchone()[0]=='COMPLETE'
    assert store.db.execute('SELECT COUNT(*) FROM historical_runs').fetchone()[0]==2
    assert store.db.execute('SELECT COUNT(*) FROM discovery_candidates').fetchone()[0]==21


def test_failed_decoding_rotates_without_losing_transactions(store,cfg,monkeypatch):
    cfg=replace(cfg,discovery_max_wallets_per_cycle=1)
    c=fake_collector(store,cfg); c.step(W,NOW)
    store.upsert_discovery_candidate(W2,T,1); c.step(W2,NOW)
    c.transaction=Mock(side_effect=ArcscanError('outage'))
    monkeypatch.setattr('historical_collector.time.time',lambda: NOW+1)
    assert c.cycle()['incomplete_steps']==1
    c=fake_collector(store,cfg)
    c.transaction=Mock(return_value=trade(1,'BUY',1)); c.identity=Mock(return_value={})
    c.cycle()
    assert store.db.execute('SELECT payload FROM historical_transactions WHERE wallet=?',(W,)).fetchone()[0] is None
    assert store.db.execute('SELECT phase FROM historical_runs WHERE wallet=?',(W2,)).fetchone()[0]=='COMPLETE'
    assert store.db.execute('SELECT COUNT(*) FROM historical_transactions').fetchone()[0]==2


def test_pending_report_uses_null_metrics_and_explains_waiting(store,cfg,tmp_path,capsys):
    rows=build_report(store.path,cfg,NOW)
    r=rows[0]
    assert r['analysis_status']=='WAITING' and r['performance_stage']=='NOT_EVALUATED'
    assert r['metrics_status']=='UNAVAILABLE' and not r['qualification_evaluated']
    assert r['buys'] is None and r['gross_realized_pnl_usdc'] is None
    assert r['transactions_queued']==r['transactions_decoded']==0
    assert r['decoding_progress_pct'] is None and r['report_reasons']==['analysis_pending']
    publish(rows,cfg,tmp_path,now=NOW)
    console=capsys.readouterr().out
    assert 'EN ATTENTE' in console and 'N/D' in console
    assert 'insufficient_buys' not in console
    saved=json.loads((tmp_path/'historical_wallet_ranking.json').read_text())
    assert saved['wallets'][0]['buys'] is None
    import csv
    with (tmp_path/'historical_wallet_ranking.csv').open(encoding='utf-8-sig',newline='') as stream:
        assert next(csv.DictReader(stream))['buys']==''


def test_partial_decoding_shows_progress_without_classifying_performance(store,cfg):
    seed_run(store,W,payloads=evidence(cfg))
    with store.db:
        store.db.execute('INSERT INTO historical_transactions VALUES(?,?,NULL)',(W,H))
    r=build_report(store.path,cfg,NOW)[0]
    assert r['analysis_status']=='IN_PROGRESS' and r['metrics_status']=='PARTIAL'
    assert r['historical_class']=='WATCH' and r['performance_stage']=='NOT_EVALUATED'
    assert r['buys']==8 and r['transactions_decoded']==16 and r['transactions_pending']==1
    assert r['decoding_progress_pct']==pytest.approx(1600/17)
    assert not r['qualification_evaluated'] and not r['paper_selection_eligible']
    assert r['report_reasons']==['analysis_in_progress']


@pytest.mark.parametrize('phase,version,expected', [('FETCHING',1,'IN_PROGRESS'),('DECODING',0,'WAITING')])
def test_progress_not_presented_as_complete_while_fetching_or_invalidated(store,cfg,phase,version,expected):
    seed_run(store,W,phase,payloads=evidence(cfg),version=version)
    r=build_report(store.path,cfg,NOW)[0]
    assert r['analysis_status']==expected and r['decoding_progress_pct'] is None
    assert r['performance_stage']=='NOT_EVALUATED' and not r['paper_selection_eligible']
    if version==0: assert r['buys'] is None and r['metrics_status']=='UNAVAILABLE'


def test_completed_empty_snapshot_has_real_zero_metrics(store,cfg):
    seed_run(store,W,'COMPLETE')
    r=build_report(store.path,cfg,NOW)[0]
    assert r['analysis_status']=='ANALYZED' and r['data_complete']
    assert r['buys']==0 and r['gross_realized_pnl_usdc']==0
    assert r['decoding_progress_pct']==100 and r['performance_stage']=='NO_CLOSED_POSITION'
    assert 'insufficient_buys' in r['report_reasons']


def test_report_distinguishes_first_gain_from_repeated_positive_evidence(store,cfg,tmp_path,capsys):
    seed_run(store,W,'COMPLETE',payloads=[trade(1,'BUY',100),trade(2,'SELL',112.42)])
    seed_run(store,W2,'COMPLETE',payloads=evidence(cfg))
    rows=build_report(store.path,cfg,NOW)
    by_wallet={r['wallet']:r for r in rows}
    assert by_wallet[W]['historical_class']=='WATCH'
    assert by_wallet[W]['performance_stage']=='PRELIMINARY_POSITIVE'
    assert by_wallet[W2]['historical_class']=='PROMISING'
    assert by_wallet[W2]['performance_stage']=='REPEATED_POSITIVE'
    assert not any(r['paper_selection_eligible'] or r['live_signal_authority'] for r in rows)
    contract=publish(rows,cfg,tmp_path,now=NOW)
    assert not contract['wallets']
    assert 'PREMIER RESULTAT POSITIF' in capsys.readouterr().out
    assert store.db.execute('SELECT COUNT(*) FROM discovery_candidates').fetchone()[0]==2


def test_gross_profit_eaten_by_gas_is_not_a_positive_performance(cfg):
    p=analyze([trade(1,'BUY',100,gas=10),trade(2,'SELL',112,gas=10)],cfg)
    assert p['gross_realized_pnl_usdc']>0
    assert performance_stage(p,cfg)=='NO_NET_PROFIT'
    assert qualify(p,{'version':1,'phase':'COMPLETE','cutoff_at':NOW},{},False,cfg,NOW)[0]=='WATCH'


def test_reconstruction_errors_are_not_reported_as_valid_zero_metrics(store,cfg):
    seed_run(store,W,'COMPLETE',payloads=[{'tx_hash':H,'error':'unsupported_or_ambiguous_reconstruction'}])
    r=build_report(store.path,cfg,NOW)[0]
    assert r['analysis_status']=='ANALYZED' and not r['data_complete']
    assert r['metrics_status']=='UNAVAILABLE' and r['buys'] is None
    assert r['performance_stage']=='NOT_EVALUATED' and not r['paper_selection_eligible']
    assert 'unsupported_or_ambiguous_reconstruction' in r['report_reasons']
