import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from copy_candidate_report import score, frequency
from test_copy_holding_duration import save_history
from copy_recovery_report import analyze, main, outcome_metrics, recovery_report, zero_value_position

W='0x'+'1'*40
OTHER='0x'+'2'*40


def position(i, buy=100, pnl=100, holding=0, price=1, sold=None):
    return dict(token_address='0x'+f'{i:040x}',bought_usd=buy,
                sold_usd=buy+pnl if sold is None and holding==0 else (sold or 0),
                pnl_usd_realised=pnl if holding==0 else 0,
                pnl_usd_unrealised=0 if holding==0 else -buy,
                bought_amount=100,sold_amount=100-holding,holding_amount=holding,token_price=price)


def payload(realized=None, opened=None):
    return dict(wallet=W,source='nansen',chain_id=5042,pages_complete=True,
                positions={'realized':realized or [],'open':opened or []},
                date={'from':'2026-07-15T00:00:00Z','to':'2026-09-20T23:10:14Z'},
                summary={'traded_times':1},metrics={'total_gain':100})


def strict(p):
    return dict(score(p,W),**frequency(p),min_roi_pct=20,
                holding_duration_eligible=True,holding_duration_status='WITHIN_HOLDING_LIMIT')


def test_small_unpriced_position_can_be_audited_without_changing_strict_score():
    p=payload([position(1),position(2)], [position(3,buy=1,holding=100,price=0)])
    old=copy.deepcopy(p);s=strict(p);before=copy.deepcopy(s)
    r=analyze(p,s)
    assert r['prudent_scenario']['pnl_usd']==199
    assert r['prudent_scenario']['roi_pct']==pytest.approx(100*199/201)
    assert r['known_buy_capital_coverage']==pytest.approx(200/201)
    assert r['audit_priority']=='ONE_POSITION'
    assert r['research_recovery_candidate']
    assert s['historical_copy_score'] is None and s['roi_pct'] is None
    assert s==before and p==old
    assert not r['live_signal_authority'] and not r['paper_selected']


def test_partial_sale_uses_remaining_cost_not_whole_buy_twice():
    row=position(1,buy=100,holding=50,price=0,sold=80)
    row['pnl_usd_realised']=30
    r=zero_value_position(None,row)
    assert r['implied_sold_cost_usd']==50
    assert r['remaining_cost_usd']==50
    assert r['pnl']==-20


def test_missing_open_view_can_only_use_coherent_realized_inventory():
    p=payload([position(1,holding=100,price=0)])
    r=analyze(p,strict(p))
    assert r['prudent_scenario']['roi_pct']==-100
    assert not r['research_recovery_candidate']


def test_large_open_loss_is_not_dropped():
    p=payload([position(1)], [position(2,buy=1000,holding=100,price=0)])
    r=analyze(p,strict(p))
    assert r['prudent_scenario']['pnl_usd']==-900
    assert r['prudent_scenario']['roi_pct']<0
    assert r['prudent_scenario']['position_count']==2
    assert not r['research_recovery_candidate']


def test_zero_cost_position_blocks_scenario_without_losing_other_diagnostics():
    p=payload([position(1),position(2,buy=0,pnl=100000)], [position(3,buy=1,holding=100,price=0)])
    r=analyze(p,strict(p))
    assert r['prudent_scenario'] is None
    assert not r['research_recovery_candidate']
    assert r['capital_coverage_status']=='UNKNOWN_OR_ZERO_COST'
    assert r['known_buy_capital_coverage'] is None
    assert r['zero_value_position_count']==1
    assert 'identify_transfer' in r['unresolved_positions'][0]['review_action']


@pytest.mark.parametrize('key,value,reason',[
    ('sold_usd',None,'missing_accounting_numbers'),
    ('sold_usd',-1,'invalid_cash_or_quantities'),
    ('sold_usd',float('inf'),'missing_accounting_numbers'),
    ('bought_amount',0,'invalid_cash_or_quantities'),
    ('holding_amount',90,'unreconciled_inventory'),
    ('pnl_usd_realised',1,'inconsistent_remaining_cost'),
    ('pnl_usd_realised',-101,'inconsistent_remaining_cost'),
    ('sold_usd',1,'sales_without_sold_quantity'),
])
def test_bad_accounting_never_infers_remaining_cost(key,value,reason):
    r=position(1,holding=100,price=0);r[key]=value
    with pytest.raises(ValueError,match=reason):zero_value_position(None,r)


@pytest.mark.parametrize('key',['bought_usd','sold_usd','pnl_usd_realised','bought_amount','sold_amount','holding_amount'])
def test_unpriced_position_does_not_mask_view_conflict(key):
    r=position(1,holding=100,price=0);o=dict(r);o[key]+=2
    p=payload([r],[o]);out=analyze(p,strict(p))
    assert out['prudent_scenario'] is None
    assert out['unresolved_positions'][0]['reason']=='conflicting_or_missing_accounting_views'


def test_overlapping_views_count_cost_and_scenario_once():
    r=position(1,holding=100,price=0)
    p=payload([position(2),r],[dict(r)])
    out=analyze(p,strict(p))
    assert out['prudent_scenario']['buy_usd']==200
    assert out['prudent_scenario']['pnl_usd']==0
    assert out['zero_value_position_count']==1


def test_known_positions_match_existing_formula_and_roi():
    rows=[position(1),position(2,buy=100,pnl=400),position(3,buy=100,pnl=-50)]
    s=score(payload(rows),W)
    m=outcome_metrics([dict(bought_usd=r['bought_usd'],pnl=r['pnl_usd_realised']) for r in rows])
    assert m['historical_scenario_score']==pytest.approx(s['historical_copy_score'])
    assert m['roi_pct']==pytest.approx(s['roi_pct'])
    assert m['position_profit_factor']==10


def test_capital_weighting_does_not_equate_tiny_tests_with_large_positions():
    m=outcome_metrics([dict(bought_usd=1000,pnl=1000)]+[dict(bought_usd=1,pnl=-1)]*10)
    assert m['capital_weighted_hit_rate']==pytest.approx(1000/1010)
    assert m['win_rate']==pytest.approx(1/11)
    assert m['concentration']==1


def test_no_losses_profit_factor_unavailable_not_infinite():
    assert outcome_metrics([dict(bought_usd=100,pnl=100)])['position_profit_factor'] is None


def test_rare_wallet_remains_research_candidate_without_authority():
    p=payload([position(1)], [position(2,buy=1,holding=100,price=0)])
    r=analyze(p,strict(p))
    assert r['research_recovery_candidate']
    assert r['copyability_checks']['liquidity']=='NOT_CHECKED'
    assert not r['live_signal_authority'] and not r['paper_selected']


def test_existing_frequency_and_roi_thresholds_remain_required():
    p=payload([position(1)], [position(2,buy=1,holding=100,price=0)])
    s=strict(p);s['frequency_status']='EXCLUDED_FREQUENCY'
    assert not analyze(p,s)['research_recovery_candidate']
    s['frequency_status']='WITHIN_FREQUENCY_LIMIT';s['min_roi_pct']=200
    assert not analyze(p,s)['research_recovery_candidate']


@pytest.mark.parametrize('holding',[False,None])
def test_prudent_scenario_cannot_bypass_holding_filter(holding):
    p=payload([position(1)], [position(2,buy=1,holding=100,price=0)])
    s=strict(p)
    if holding is None:
        s.pop('holding_duration_eligible')
    else:
        s['holding_duration_eligible']=holding
    r=analyze(p,s)
    assert r['performance_recovery_candidate']
    assert not r['research_recovery_candidate']
    assert r['prudent_scenario']['roi_pct']>20


@pytest.fixture
def stored(tmp_path):
    db=tmp_path/'test.sqlite3';scope=tmp_path/'scope.json';registry=tmp_path/'dex.json'
    scope.write_text(json.dumps({'schema_version':1,'purpose':'copy_research_shortlist','wallets':[W,OTHER]}),encoding='utf-8')
    registry.write_text(json.dumps({'chain_id':5042,'emitters':[]}),encoding='utf-8')
    p=payload([position(1)],[position(2,buy=1,holding=100,price=0)])
    with sqlite3.connect(db) as c:
        save_history(c)
        c.execute('CREATE TABLE external_wallet_metrics(wallet,provider,snapshot_key,observed_at,retrieved_at,chain_id,payload,error_category)')
        c.execute('INSERT INTO external_wallet_metrics VALUES(?,?,?,?,?,?,?,?)',(W,'nansen','saved',1,1,5042,json.dumps(p),None))
    return db,scope,registry


def test_readonly_report_exact_snapshot_and_missing_details(stored):
    db,scope,_=stored;before=db.read_bytes(),scope.read_bytes()
    r=recovery_report(db,scope)
    assert r['summary']['recovered_research_candidates']==1
    assert r['summary']['single_position_candidates']==1
    assert r['rows'][0]['snapshot_key']=='saved'
    assert not r['rows'][0]['copy_research_eligible']
    assert r['rows'][0]['historical_copy_score'] is None
    assert (db.read_bytes(),scope.read_bytes())==before
    missing=next(x for x in r['rows'] if x['wallet']==OTHER)
    assert missing['recovery_status']=='NOT_ANALYZABLE'
    assert r['token_review_queue'][0]['date']['to']=='2026-09-20T23:10:14Z'


def test_infrastructure_is_never_recovered(stored):
    db,scope,_=stored
    r=recovery_report(db,scope,excluded={W})
    assert r['summary']['recovered_research_candidates']==0
    assert all(not x['research_recovery_candidate'] for x in r['rows'])


def test_cli_rejects_overwrite_and_malformed_registry(stored,tmp_path):
    db,scope,registry=stored;out=tmp_path/'report'
    argv=['--db',str(db),'--shortlist',str(scope),'--registry',str(registry),'--output-dir',str(out)]
    main(argv);saved=(out/'report.json').read_bytes()
    with pytest.raises(SystemExit):main(argv)
    assert (out/'report.json').read_bytes()==saved
    registry.write_text('{"chain_id": 1,"emitters":[]}',encoding='utf-8')
    with pytest.raises(SystemExit):main(argv[:-1]+[str(tmp_path/'other')])


def test_cli_has_no_network_or_provider_import_and_keeps_sources(stored,tmp_path):
    db,scope,registry=stored;before=db.read_bytes()
    code='''
import sys,runpy,builtins
def audit(event,args):
    if event.startswith(('socket.connect','socket.getaddrinfo','socket.sendto')):raise AssertionError('Network forbidden')
sys.addaudithook(audit)
original=builtins.__import__
def guarded(name,*args,**kw):
    if name in ('requests','nansen_client','nansen_bulk','external_http','rpc'):raise AssertionError('Forbidden import: '+name)
    return original(name,*args,**kw)
builtins.__import__=guarded
sys.argv=['copy_recovery_report.py']+sys.argv[1:]
runpy.run_path('copy_recovery_report.py',run_name='__main__')
'''
    out=tmp_path/'network_proof'
    proc=subprocess.run([sys.executable,'-c',code,'--db',str(db),'--shortlist',str(scope),
                         '--registry',str(registry),'--output-dir',str(out)],
                        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert proc.returncode==0,proc.stderr
    assert 'Nansen credits consumed: 0' in proc.stdout
    assert db.read_bytes()==before
