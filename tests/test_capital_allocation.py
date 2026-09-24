import csv
import json

import pytest

from capital_allocation import (SizingPolicy, allocation_metrics, describe_buy,
                                event_sizing, forward_buys, sizing_policy)
from cluster import ClusterBook
from ranking_clusters import cluster_ranking, select_view
from test_audit_regressions import W, T, POOL, H, event
from test_followup import make_bot
from test_notification_policy import events as alert_events
from test_wallet_ranking import report_db, candidate, profile, ranking, NOW
from wallet_ranking import main, summarize, write_report


def trades(sizes, returns):
    return [dict(wallet=W,tx_hash=str(i),token=str(i),timestamp=20+i,
                 buy_usdc=s,return_ratio=r) for i,(s,r) in enumerate(zip(sizes, returns))]


def add_buy(db, wallet=W, amount=100, ret=None, token=T, timestamp=30, tx=H):
    db.add_event(event(wallet=wallet,usdc_amount=amount,token_amount=1,token=token,
                       timestamp=timestamp,tx_hash=tx))
    if ret is not None:
        db.db.execute("""INSERT INTO trade_outcomes
            (tx_hash,wallet,token,entry_at,target_at,deadline_at,observed_at,price,status)
            VALUES(?,?,?,?,?,?,?,?, 'OBSERVED')""",
            (tx.lower(),wallet.lower(),token.lower(),timestamp,timestamp+100,timestamp+110,timestamp+100,amount*(1+ret)))
        db.commit()


def test_large_winner_small_tests_differ_from_equal_sized_losses():
    good = allocation_metrics(trades([1000]+[1]*10, [2]+[-1]*10))
    equal = allocation_metrics(trades([1000]*11, [2]+[-1]*10))
    assert good['capital_weighted_hit_rate'] == pytest.approx(1000/1010)
    assert good['capital_weighted_return'] == pytest.approx(1990/1010)
    assert good['profit_factor'] == 200
    assert equal['capital_weighted_return'] < 0
    assert good['capital_share_per_trade'][0]['capital_share'] == pytest.approx(1000/1010)
    assert good['largest_trade_contribution'] == pytest.approx(2000/1990)


def test_one_lucky_shot_is_concentrated_not_statistically_robust():
    p = allocation_metrics(trades([1000], [100]))
    assert p['capital_weighted_return'] == 100
    assert p['concentration_risk'] == 'HIGH'
    assert p['sizing_confidence'] == 'LOW'
    assert p['allocation_evidence_confidence'] == 'LOW'
    assert p['largest_trade_contribution'] == 1
    assert p['profit_factor'] is None
    assert p['profit_factor_status'] == 'NO_LOSSES_OBSERVED'
    assert p['conviction_accuracy'] is None


def test_conviction_accuracy_improves_with_large_winners():
    sizes = [100,100,100]+[1]*7
    before = allocation_metrics(trades(sizes, [2,-1,-1]+[-1]*7))
    after = allocation_metrics(trades(sizes, [2,2,2]+[-1]*7))
    assert before['conviction_accuracy'] == pytest.approx(1/3)
    assert after['conviction_accuracy'] == 1
    assert after['concentration_risk'] == 'LOW'


@pytest.mark.parametrize('count,expected', [(0,'LOW'),(2,'LOW'),(4,'LOW'),(5,'MEDIUM'),(9,'MEDIUM'),(10,'HIGH'),(20,'HIGH')])
def test_progressive_sizing_confidence(count, expected):
    p = allocation_metrics(trades([10]*max(0,count-1)+([100000] if count else []), [None]*count))
    assert p['sizing_confidence'] == expected
    assert (p['usual_buy_usdc'] is not None) == (count >= 5)
    if count >= 5:
        assert p['median_buy_usdc'] == 10
        assert p['capital_share_per_trade'][-1]['position_size_percentile'] == 100


def test_capital_share_works_without_baseline():
    p = allocation_metrics(trades([800,10,5,5,2], [None]*5))
    assert p['capital_share_per_trade'][0]['capital_share'] == pytest.approx(800/822)
    assert allocation_metrics(trades([800,10],[None,None]))['capital_share_per_trade'][0]['capital_share'] == pytest.approx(800/810)


def test_unknown_outcomes_never_become_losses_or_successes():
    p = allocation_metrics(trades([1000,100],[None,2]))
    assert p['capital_weighted_return'] == 2
    assert p['outcome_capital_coverage'] == pytest.approx(100/1100)
    assert p['capital_evaluable_trades'] == 1
    assert allocation_metrics(trades([100],[None]))['capital_weighted_return'] is None


@pytest.mark.parametrize('amount,label', [(10,'TINY'),(50,'SMALL'),(100,'NORMAL'),(200,'LARGE'),(500,'EXCEPTIONAL')])
def test_labels_descriptive_and_causal_baseline(amount,label):
    p = describe_buy(amount, [100]*10)
    assert p['conviction_label'] == label
    assert p['usual_buy_usdc'] == 100
    assert p['capital_share'] == pytest.approx(amount/(1000+amount))
    assert describe_buy(amount,[100,100])['conviction_label'] is None


def test_policy_configuration(monkeypatch):
    monkeypatch.setenv('SIZING_MEDIUM_BUYS','3')
    monkeypatch.setenv('SIZING_HIGH_BUYS','7')
    assert sizing_policy() == SizingPolicy(3,7,.8)
    with pytest.raises(ValueError): SizingPolicy(5,3)


def test_forward_only_cutoff_dedup_fixed_outcome_window(report_db):
    candidate(report_db)
    add_buy(report_db, timestamp=19, tx='old', ret=100)
    add_buy(report_db, timestamp=20, tx='valid', ret=2)
    report_db.add_event(event(timestamp=20,tx_hash='valid',usdc_amount=100,token_amount=1))
    add_buy(report_db, timestamp=30, tx='late', ret=100)
    report_db.db.execute("UPDATE trade_outcomes SET observed_at=deadline_at+1 WHERE tx_hash='late'")
    report_db.add_research_trade(event(usdc_amount=100000))
    report_db.commit()
    rows = forward_buys(report_db.db, NOW)[W]
    assert len(rows) == 2
    assert [t['return_ratio'] for t in rows] == [2,None]
    assert forward_buys(report_db.db, 100)[W][0]['return_ratio'] is None


def test_signal_metadata_uses_prior_only_no_future_or_current_double_count(report_db):
    candidate(report_db)
    for i in range(10): add_buy(report_db,amount=100,tx=str(i),timestamp=20+i)
    add_buy(report_db, amount=999999, tx='future',timestamp=50)
    current = event(timestamp=40,tx_hash='current',usdc_amount=200,token_amount=1)
    before = event_sizing(report_db.db,current)
    report_db.add_event(current)
    assert event_sizing(report_db.db,current) == before
    assert before['usual_buy_usdc'] == 100
    assert before['capital_share'] == pytest.approx(200/1200)


def test_bad_sizing_keeps_watch_and_single_winner_never_promotes(report_db,tmp_path):
    candidate(report_db); candidate(report_db,T)
    add_buy(report_db,amount=1,ret=-1)
    add_buy(report_db,wallet=T,amount=1000,ret=100)
    before = list(report_db.db.iterdump())
    rows = ranking(report_db,tmp_path)
    assert [r['wallet'] for r in rows] == [T,W]
    assert all(r['report_class'] == 'WATCH' for r in rows)
    assert rows[0]['concentration_risk'] == 'HIGH'
    assert not any(r['internal_promotion_enabled'] for r in rows)
    assert list(report_db.db.iterdump()) == before


def make_related(db, count=10):
    addresses = ['0x'+format(i+100,'040x') for i in range(count)]
    for i,w in enumerate(addresses):
        candidate(db,w)
        db.save_runtime_event(event(wallet=w,tx_hash=f'identity{i}',identity_id=POOL,identity_known=True))
        db.db.execute('INSERT INTO candidate_sources VALUES(?,?,1,20)',(w,T))
        add_buy(db,wallet=w,amount=100,ret=2,tx=f'buy{i}')
    db.commit()
    return addresses


def test_ten_related_wallets_are_one_identity_and_one_winner_source(report_db,tmp_path):
    make_related(report_db)
    rows = ranking(report_db,tmp_path)
    clusters = cluster_ranking(rows)
    assert len(clusters) == 1
    c = clusters[0]
    assert c['wallet_count'] == c['forward_buys'] == 10
    assert c['independent_cluster_count'] == c['winner_sources_distinct'] == 1
    assert c['forward_winner_tokens_distinct'] == 1
    assert c['capital_total_observed'] == 1000
    assert c['capital_weighted_return'] == 2
    assert all(r['related_wallets_count'] == 9 for r in rows)
    assert summarize(rows)['known_identity_clusters'] == 1


def test_diverse_caps_display_only_preserves_full_csv(report_db,tmp_path,capsys):
    related = make_related(report_db)
    candidate(report_db,W)
    rows = ranking(report_db,tmp_path)
    full, selected = select_view(rows,top=20,diverse=True,per_cluster=2)
    assert len(full) == 11 and len(selected) == 3
    assert sum(r['wallet'] in related for r in selected) == 2
    assert all(r['same_cluster_wallet_count_in_top'] == 2 for r in full if r['wallet'] in related)
    assert all(r['same_cluster_wallet_count_in_top'] == 0 for r in rows)
    path=tmp_path/'full.csv'
    write_report(rows,path,top=20,diverse=True)
    assert len(list(csv.DictReader(path.open(encoding='utf-8-sig')))) == 11
    assert 'total=11' in capsys.readouterr().out


def test_unknown_identities_are_not_asserted_independent(report_db,tmp_path):
    candidate(report_db); candidate(report_db,T)
    rows = ranking(report_db,tmp_path)
    assert all(r['independent_cluster_count'] == 0 for r in rows)
    summary=summarize(rows)
    assert summary['total_identity_clusters'] == 2
    assert summary['known_identity_clusters'] == 0
    assert summary['unknown_identity_wallets'] == 2


def test_historical_links_conservatively_bridge_runtime_snapshots(report_db,tmp_path):
    candidate(report_db); candidate(report_db,T)
    report_db.save_runtime_event(event(identity_id=W,identity_known=True))
    report_db.save_runtime_event(event(wallet=T,identity_id=T,identity_known=True))
    (tmp_path/'history.json').write_text(json.dumps({'ranked_wallets':[
        {'wallet':W,'identity_cluster':POOL}, {'wallet':T,'identity_cluster':POOL}]}))
    rows=ranking(report_db,tmp_path)
    assert rows[0]['identity_cluster_id'] == rows[1]['identity_cluster_id']


def test_cluster_aggregate_is_capital_weighted_not_mean_of_wallet_returns(report_db,tmp_path):
    candidate(report_db); candidate(report_db,T)
    for w in (W,T): report_db.save_runtime_event(event(wallet=w,identity_id=POOL,identity_known=True))
    add_buy(report_db,amount=1000,ret=2)
    add_buy(report_db,wallet=T,amount=1,ret=-1,tx='other')
    c=cluster_ranking(ranking(report_db,tmp_path))[0]
    assert c['capital_weighted_return'] == pytest.approx(1999/1001)
    assert c['internal_capital_concentration'] == pytest.approx(1000/1001)
    assert c['concentration_risk'] == 'HIGH'
    assert c['cluster_confidence'] == 'LOW'


def test_watch_cluster_observation_and_existing_authority_are_unchanged():
    book=ClusterBook(1200,600)
    for i in range(3):
        e=event(wallet=str(i),identity_id=str(i),confirmation_eligible=False,wallet_weight=0,
                intelligence_class='WATCH',sizing={'capital_share':1,'conviction_label':'EXCEPTIONAL'})
        cluster=book.add(e)
    assert cluster.distinct_wallets == 3 and cluster.independent_identities == 0
    # The existing engine can count eligible independent events regardless of
    # descriptive class. Sizing neither grants nor removes that permission.
    book=ClusterBook(1200,600)
    for i in range(3):
        cluster=book.add(event(wallet=str(i),identity_id=str(i),intelligence_class='WATCH',
                               confirmation_eligible=True,wallet_weight=2))
    assert cluster.distinct_wallets == cluster.independent_identities == 3


def test_enriched_alert_is_single_authorized_message_and_survives_restart(report_db,tmp_path):
    bot=make_bot(report_db,tmp_path)
    first,second=alert_events()
    for i in range(10): add_buy(report_db,amount=100,tx=str(i),timestamp=20+i)
    first.usdc_amount=1200
    bot.handle_event(first); bot.handle_event(second)
    rows=report_db.db.execute('SELECT text FROM alert_outbox').fetchall()
    assert len(rows) == 1
    text=rows[0]['text']
    assert 'Sizing confidence: HIGH' in text
    assert 'Conviction: EXCEPTIONAL' in text
    assert 'Independent identities on token: 2' in text
    assert len(text) < 4096
    restored=report_db.recent_runtime_events(0)
    assert restored[0].sizing['usual_buy_usdc'] == 100
    assert restored[0].confirmation_eligible


def test_unauthorized_wallet_never_enriched_or_granted_authority(report_db,tmp_path):
    bot=make_bot(report_db,tmp_path)
    candidate(report_db)
    e=event(timestamp=100,confirmation_eligible=False,wallet_weight=0,
            sizing={'conviction_label':'EXCEPTIONAL'})
    bot.handle_event(e)
    assert e.sizing == {} and not e.confirmation_eligible
    assert report_db.db.execute('SELECT count(*) FROM alert_outbox').fetchone()[0] == 0
    assert report_db.db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


def test_sizing_does_not_change_signal_score_status_or_paper(report_db,tmp_path,monkeypatch):
    import bot as module
    from scoring import score_buy_cluster, classify_status
    from config import Settings
    from test_followup import signal
    s = signal()
    before = score_buy_cluster(s,s.risk,Settings())
    status = classify_status(s,s.risk,before,Settings())
    for e in s.events:
        e.sizing={'size_multiplier':0.0001,'sizing_confidence':'LOW','conviction_label':'TINY'}
    assert score_buy_cluster(s,s.risk,Settings()) == before
    assert classify_status(s,s.risk,before,Settings()) == status
    bot=make_bot(report_db,tmp_path)
    monkeypatch.setattr(module,'event_sizing',lambda *a: describe_buy(1,[10000]*10))
    first,second=alert_events()
    bot.handle_event(first); bot.handle_event(second)
    assert report_db.db.execute('SELECT count(*) FROM paper_fills').fetchone()[0] == 1
    assert report_db.db.execute('SELECT count(*) FROM proposals').fetchone()[0] == 1


def test_malformed_or_nonfinite_capital_never_produces_performance(report_db):
    candidate(report_db)
    add_buy(report_db,tx='futureentry',ret=2)
    report_db.db.execute("UPDATE trade_outcomes SET target_at=1,observed_at=2 WHERE tx_hash='futureentry'")
    add_buy(report_db,tx='invalid',amount=float('inf'))
    add_buy(report_db,tx='underflow',amount=1e-300,ret=2)
    report_db.db.execute("UPDATE events SET token_amount=1e300 WHERE tx_hash='underflow'")
    report_db.commit()
    rows=forward_buys(report_db.db,NOW)[W]
    assert len(rows) == 2
    assert all(r['return_ratio'] is None for r in rows)


def test_no_epoch_has_no_fabricated_capital_share(report_db):
    report_db.db.execute('DELETE FROM observation_epochs'); report_db.commit()
    p=event_sizing(report_db.db,event())
    assert p['capital_share'] is None and p['usual_buy_usdc'] is None
    assert p['sizing_confidence'] == 'LOW'


def test_optional_sizing_failure_does_not_block_observation(report_db,tmp_path,monkeypatch):
    import bot as module
    bot=make_bot(report_db,tmp_path)
    def broken(*a): raise ValueError('malformed optional config')
    monkeypatch.setattr(module,'sizing_policy',broken)
    first,second=alert_events()
    bot.handle_event(first); bot.handle_event(second)
    assert report_db.db.execute('SELECT count(*) FROM alert_outbox').fetchone()[0] == 1


def test_cli_cluster_view_exports_both_complete_tables(report_db,tmp_path,capsys):
    make_related(report_db,3)
    args=['--db',report_db.path,'--watchlist',str(tmp_path/'absent.json'),
          '--history',str(tmp_path/'absent.json'),'--csv',str(tmp_path/'wallets.csv'),
          '--cluster-csv',str(tmp_path/'clusters.csv'),'--clusters']
    assert main(args) == 0
    assert 'Clusters descriptifs' in capsys.readouterr().out
    assert len(list(csv.DictReader((tmp_path/'wallets.csv').open(encoding='utf-8-sig')))) == 3
    assert len(list(csv.DictReader((tmp_path/'clusters.csv').open(encoding='utf-8-sig')))) == 1
