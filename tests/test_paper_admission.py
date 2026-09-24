import json
from types import SimpleNamespace

import pytest

from historical_metrics import analyze,qualify
from historical_report import build_report,publish
from identity_graph import IdentityGraph,WalletFacts
from paper_admission import assess,POLICY,ZERO_ADDRESS
from paper_copy.selection import load_selection,SelectionError
from paper_copy.scanner import Scanner
from paper_copy.store import Store
from paper_copy.policy import Policy
from paper_copy.execution import Execution
from paper_copy.report import report
from test_historical_analysis import cfg,store,evidence,NOW,W,W2,T
from test_paper_copy import order,market,cash


def review_row(cfg,wallet=W):
    metrics=analyze(evidence(cfg),cfg,as_of=NOW)
    metrics['issues']=['external_or_unexplained_token_movement','unsupported_or_ambiguous_reconstruction']
    run={'phase':'COMPLETE','version':1,'cutoff_at':NOW}
    category,reasons=qualify(metrics,run,{'eligible':True,'independent_profitable_deployers':0,
                                     'unknown_profitable_deployers':2},False,cfg,NOW)
    row={**metrics,'wallet':wallet,'identity_cluster_id':W,'identity_eligible':True,
         'historical_class':category,'blocking_reasons':reasons,'infrastructure_excluded':False,
         'paper_selection_eligible':False,'live_signal_authority':False,'shared_service':False,
         'analysis_status':'ANALYZED','snapshot_phase':'COMPLETE','snapshot_at':NOW,
         'transactions_pending':0,'collection_error':None,'expires_at':NOW+1000,'data_complete':False}
    return annotate(row,cfg)


def annotate(row,cfg):
    warnings,blockers=assess(row,cfg,NOW)
    row.update(paper_review_warnings=warnings,paper_review_blockers=blockers,paper_review_eligible=not blockers)
    return row


def document(rows):
    return {'schema_version':2,'purpose':'paper_review_selection','admission_policy':POLICY,
            'chain_id':5042,'generated_at':NOW,'live_signal_authority':False,
            'requires_consumer_validation':True,'one_wallet_per_identity_cluster':False,'wallets':rows}


def load(tmp_path,cfg,rows,now=NOW):
    path=tmp_path/'review.json'; path.write_text(json.dumps(document(rows)))
    return load_selection(path,cfg,now)


@pytest.mark.parametrize('buys,closed,periods,tokens,span,contribution,pnl,gas_unknown',[
    (42,34,5,21,105633,.1826687815,996.5942678,True),
    (50,49,3,17,101115,.2219186869,1265.2689563,False),
    (60,55,4,24,57914,.1803361941,548.4929605,False),
    (52,52,2,16,78453,.3111310402,481.0398651,False),
])
def test_four_observed_profiles_pass_without_lowering_performance_thresholds(cfg,tmp_path,buys,closed,periods,tokens,span,contribution,pnl,gas_unknown):
    row=review_row(cfg)
    row.update(buys=buys,closed_positions=closed,profitable_periods=periods,
               profitable_tokens=['0x'+f'{i+100:040x}' for i in range(tokens)],
               closed_evidence_span_seconds=span,largest_position_contribution=contribution,
               conservative_pnl_usdc=pnl,open_positions_count=0,
               buy_details=[{'timestamp':NOW-1000,'buy_usdc':100} for _ in range(buys)])
    if gas_unknown:
        row['issues'].append('gas_cost_unknown'); row['blocking_reasons'].append('gas_cost_unknown')
    annotate(row,cfg)
    assert row['historical_class']=='WATCH' and row['data_complete'] is False
    assert row['paper_review_eligible'] and not row['paper_selection_eligible']
    selected=load(tmp_path,cfg,[row])
    assert selected[W]['admission']=='PAPER_REVIEW' and selected[W]['warnings']


@pytest.mark.parametrize('change,reason',[
    ({'buys':7},'insufficient_buys'),({'closed_positions':4},'insufficient_closed_positions'),
    ({'profitable_tokens':[T]},'insufficient_profitable_tokens'),
    ({'closed_evidence_span_seconds':21599},'evidence_period_too_short'),
    ({'conservative_pnl_usdc':0},'conservative_profit_not_positive'),
    ({'open_inventory_risk':'OPEN_COST_NOT_COVERED'},'open_inventory_not_covered_by_realized_profit'),
    ({'snapshot_phase':'DECODING'},'historical_snapshot_incomplete'),
    ({'snapshot_at':NOW-49*3600},'historical_snapshot_stale'),
    ({'collection_error':'timeout'},'historical_collection_error'),
    ({'identity_eligible':False},'identity_unknown_or_shared_service'),
    ({'shared_service':True},'shared_service_excluded'),
    ({'issues':['sell_without_complete_cost_basis']},'sell_without_complete_cost_basis'),
])
def test_every_other_blocker_still_applies_and_consumer_rechecks(cfg,tmp_path,change,reason):
    row=review_row(cfg); row.update(change)
    assert reason in assess(row,cfg,NOW)[1]
    # Keep the stale eligible=True export flag: consumer must not trust it.
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


@pytest.mark.parametrize('changes,expected',[
    ({'profitable_periods':1},{'regularity_not_established'}),
    ({'largest_position_contribution':.9876},{'profit_concentrated_in_one_position'}),
    ({'profitable_periods':1,'largest_position_contribution':.9876},
     {'regularity_not_established','profit_concentrated_in_one_position'}),
])
def test_rare_concentrated_wallet_can_enter_exploratory_paper_only(cfg,tmp_path,changes,expected):
    row=review_row(cfg);row.update(changes)
    row['blocking_reasons']+=sorted(expected)
    annotate(row,cfg)
    assert not row['paper_review_blockers']
    assert expected <= set(row['paper_review_warnings'])
    selected=load(tmp_path,cfg,[row])[W]
    assert selected['admission']=='PAPER_REVIEW'
    assert selected['historical_class']=='WATCH'
    assert not row['live_signal_authority'] and not row['paper_selection_eligible']
    # Strict qualification still sees both performance failures.
    metrics={**row,'concentration_risk':'HIGH' if row['largest_position_contribution']>cfg.historical_max_concentration else 'LOW'}
    category,reasons=qualify(metrics,{'phase':'COMPLETE','version':1,'cutoff_at':NOW},
                            {'eligible':True,'independent_profitable_deployers':10},False,cfg,NOW)
    assert category!='HISTORICALLY_QUALIFIED' and expected<=set(reasons)


def test_consumer_recomputes_exploratory_warnings_even_when_export_hides_them(cfg,tmp_path):
    row=review_row(cfg)
    row.update(profitable_periods=1,largest_position_contribution=.99)
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])
    annotate(row,cfg)
    assert W in load(tmp_path,cfg,[row])
    row['paper_review_warnings'].remove('regularity_not_established')
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


def test_exploratory_policy_version_is_explicit(cfg,tmp_path):
    payload=document([review_row(cfg)])
    assert payload['admission_policy']=='paper_review_v2'
    payload['admission_policy']='paper_review_v1'
    path=tmp_path/'old.json';path.write_text(json.dumps(payload))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)


@pytest.mark.parametrize('change,reason',[
    ({'snapshot_at':NOW-49*3600},'historical_snapshot_stale'),
    ({'transactions_pending':1},'historical_snapshot_incomplete'),
    ({'shared_service':True},'shared_service_excluded'),
    ({'infrastructure_excluded':True},'dex_infrastructure_excluded'),
    ({'conservative_pnl_usdc':-1},'conservative_profit_not_positive'),
    ({'issues':['sell_without_complete_cost_basis']},'sell_without_complete_cost_basis'),
])
def test_exploratory_exceptions_never_override_other_blockers(cfg,tmp_path,change,reason):
    row=review_row(cfg);row.update(profitable_periods=1,largest_position_contribution=.99)
    row.update(change);annotate(row,cfg)
    assert reason in row['paper_review_blockers']
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


def test_paper_does_not_silently_accept_any_new_decoder_error(cfg,tmp_path):
    row=review_row(cfg); row['blocking_reasons'].append('unexpected_new_error')
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


def test_infrastructure_exclusion_rechecked_at_consumer(cfg,tmp_path):
    row=review_row(cfg)
    (tmp_path/'registry.json').write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


def test_shared_cluster_has_separate_cash_and_visible_warnings(cfg,tmp_path):
    selected=load(tmp_path,cfg,[review_row(cfg,W),review_row(cfg,W2)])
    store=Store(tmp_path/'paper.sqlite3',Policy())
    try:
        Scanner(store,None,Policy(),cfg).sync(selected,0)
        Execution(store,Policy(),market(),clock=lambda:NOW).fill(order(store),NOW)
        assert cash(store)==950 and cash(store,W2)==1000
        rows=report(store.path)
        assert rows[0]['identity_cluster']==rows[1]['identity_cluster']
        assert all(r['admission']=='PAPER_REVIEW' and r['warnings'] for r in rows)
    finally: store.close()


def test_strict_contract_does_not_gain_the_paper_exception(cfg,tmp_path):
    row=review_row(cfg)
    payload=document([row]); payload.update(schema_version=1,purpose='historical_selection_for_paper',one_wallet_per_identity_cluster=True)
    path=tmp_path/'strict.json'; path.write_text(json.dumps(payload))
    with pytest.raises(SelectionError): load_selection(path,cfg,NOW)


def test_expired_export_and_hidden_warnings_remain_fail_closed(cfg,tmp_path):
    row=review_row(cfg)
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row],NOW+1001)
    row['paper_review_warnings']=[]
    with pytest.raises(SelectionError): load(tmp_path,cfg,[row])


def test_zero_funder_never_connects_wallets_or_grants_authority():
    data={W:{'funded_by':ZERO_ADDRESS},W2:{'funded_by':{'address':ZERO_ADDRESS}}}
    graph=IdentityGraph(SimpleNamespace(address_facts=lambda a:data.get(a,{})))
    # Even an old cached zero funder must be discarded.
    graph.facts[W]=WalletFacts(W,funded_by=ZERO_ADDRESS,fetched_at=10**12)
    graph.refresh([W,W2])
    assert not graph.related(W,W2)
    assert not graph.eligible(W) and not graph.eligible(W2)
    assert graph.facts[W].null_funder and graph.facts[W].funded_by is None
    assert ZERO_ADDRESS not in graph.parent


def test_real_common_funder_still_links_wallets_and_services_remain_excluded():
    data={W:{'funded_by':T},W2:{'funded_by':T}}
    graph=IdentityGraph(SimpleNamespace(address_facts=lambda a:data.get(a,{})))
    graph.refresh([W,W2])
    assert graph.related(W,W2) and graph.eligible(W)
    facts=graph._parse_facts(W,{'funded_by':{'address':ZERO_ADDRESS,'category':'exchange'}})
    assert facts.null_funder and facts.shared_service


def test_legacy_zero_clusters_corrected_in_read_only_report_and_separate_export(store,cfg,tmp_path):
    for wallet in (W,W2):
        store.upsert_discovery_candidate(wallet,T,1)
        identity={'eligible':True,'funder':ZERO_ADDRESS,'cluster':ZERO_ADDRESS,
                  'members':[W,W2,ZERO_ADDRESS],'shared_service':False,
                  'independent_profitable_deployers':0,'unknown_profitable_deployers':2}
        store.db.execute('''INSERT INTO historical_runs(wallet,version,started_at,cutoff_block,cutoff_at,cutoff_hash,phase,identity)
                            VALUES(?,1,?,100,?,'hash','COMPLETE',?)''',(wallet,NOW,NOW,json.dumps(identity)))
        for tx in evidence(cfg):
            store.db.execute('INSERT INTO historical_transactions VALUES(?,?,?)',(wallet,tx['tx_hash'],json.dumps(tx)))
    store.db.commit(); before=store.db.total_changes
    rows=build_report(store.path,cfg,NOW)
    assert all(r['paper_review_eligible'] and not r['identity_eligible'] for r in rows)
    assert rows[0]['identity_cluster_id']!=rows[1]['identity_cluster_id']
    assert all(r['historical_class']!='HISTORICALLY_QUALIFIED' for r in rows)
    strict=publish(rows,cfg,tmp_path,now=NOW)
    assert not strict['wallets']
    selected=load_selection(tmp_path/'paper_review_selection.json',cfg,NOW)
    assert len(selected)==2 and all(not r['identity_known'] for r in selected.values())
    assert all('null_funder_identity_unknown' in r['warnings'] for r in selected.values())
    assert store.db.total_changes==before
    assert store.db.execute('SELECT COUNT(*) FROM promoted_wallets').fetchone()[0]==0
