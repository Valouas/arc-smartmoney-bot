import csv
import json
import sqlite3
from dataclasses import replace

import pytest

from config import Settings
from storage import Storage
from test_audit_regressions import W, T, POOL, H, event
from wallet_ranking import build_ranking, main, summarize, write_report

NOW = 200000


@pytest.fixture
def report_db(tmp_path):
    db = Storage(tmp_path / "ranking.sqlite3")
    db.db.execute("INSERT INTO observation_epochs VALUES('launch_universe',10)")
    db.commit()
    yield db
    db.db.close()


def candidate(db, wallet=W, **changes):
    values = dict(wallet=wallet, first_discovered_at=20, last_discovered_at=30,
                  source_winners=2, status="QUARANTINE", history_complete=1, hydrated_at=40)
    values.update(changes)
    db.db.execute("INSERT INTO discovery_candidates("+",".join(values)+") VALUES("+
                  ",".join("?" for _ in values)+")", tuple(values.values()))
    db.commit()


def profile(db, wallet=W, **changes):
    p = dict(wallet=wallet, generated_at=NOW, profile_version=2, cohort="forward_after_discovery",
             observation_start=20, data_quality_ok=True, confirmation_eligible=True,
             launches_bought=8, evaluable_trades=5, winners=2, smart_score=68,
             opportunity_count=40, opportunity_capture_rate=.20, independent_winners=2,
             profile_class="SMART", recommended_weight=2, adjusted_hit_rate=.37)
    p.update(changes)
    db.save_wallet_profile(p)


def promote(db, wallet=W, enabled=1):
    db.db.execute("UPDATE discovery_candidates SET status='PROMOTED' WHERE wallet=?", (wallet,))
    db.db.execute("INSERT INTO promoted_wallets VALUES(?,?,68,'SMART',2,'test',?)", (wallet, NOW, enabled))
    db.commit()


def ranking(db, tmp_path, **kwargs):
    return build_ranking(db.path, now=NOW, watchlist_path=tmp_path / "generated.json",
                         history_path=tmp_path / "history.json", **kwargs)


def test_union_order_and_no_engine_mutations(report_db, tmp_path):
    db = report_db
    candidate(db); profile(db); promote(db)
    candidate(db, T); profile(db, T)
    candidate(db, POOL, history_complete=0, hydrated_at=None)
    extra = '0x'+'5'*40
    profile(db, extra, observation_start=10, smart_score=10, profile_class="IGNORE")
    before = list(db.db.iterdump())
    rows = ranking(db, tmp_path)
    assert [r['report_class'] for r in rows] == ['VALIDATED', 'PROMISING', 'WATCH', 'REJECTED']
    assert {r['wallet'] for r in rows} == {W,T,POOL,extra}
    assert list(db.db.iterdump()) == before
    assert rows[1]['internal_promotion_enabled'] is False
    assert rows[3]['discovery_status'] == 'PROFILE_ONLY'


@pytest.mark.parametrize('changes', [dict(profile_version=1), dict(cohort='retrospective'),
                                     dict(observation_start=1)])
def test_retrospective_or_wrong_cohort_never_promising(report_db, tmp_path, changes):
    candidate(report_db); profile(report_db, smart_score=99, **changes)
    report_db.add_research_trade(event())
    row = ranking(report_db, tmp_path)[0]
    assert row['report_class'] == 'WATCH'
    assert row['research_trades'] == 1
    assert row['forward_hit_rate_pct'] is None
    assert row['forward_buys_observed'] == 0
    assert row['score'] == 99  # Still report the stored score, labelled by cohort blockers.


def test_forward_buys_dedup_and_discovery_cutoff(report_db, tmp_path):
    candidate(report_db)
    for ts, tx, verified, side in [(19,H,True,'BUY'), (20,'b',True,'BUY'),
                                  (21,'c',True,'BUY'), (22,'d',True,'BUY'),
                                  (23,'e',False,'BUY'), (24,'f',True,'SELL'), (20,'b',True,'BUY')]:
        report_db.add_event(event(timestamp=ts,tx_hash=tx,trade_verified=verified,side=side))
    rows = ranking(report_db, tmp_path)
    assert rows[0]['forward_buys_observed'] == 3
    summary = summarize(rows)
    assert summary['forward_buys_ge_1'] == summary['forward_buys_ge_3'] == 1
    assert summary['incomplete'] == 1


def test_unknown_independence_and_no_prior_as_hit_rate(report_db, tmp_path):
    candidate(report_db); profile(report_db, independent_winners=None, evaluable_trades=0, winners=0, launches_bought=0)
    row = ranking(report_db, tmp_path)[0]
    assert row['independent_sources'] is None
    assert row['forward_hit_rate_pct'] is None
    assert row['forward_adjusted_hit_rate_pct'] is None
    assert 'independent_sources_unknown' in row['promotion_blockers']


def test_source_progress_breaks_ties_without_validating_research(report_db, tmp_path):
    candidate(report_db, W, source_winners=1)
    candidate(report_db, T, source_winners=3)
    rows = ranking(report_db, tmp_path)
    assert [r['wallet'] for r in rows] == [T, W]
    assert all(r['report_class'] == 'WATCH' for r in rows)


@pytest.mark.parametrize('setting,value', [('promotion_min_score',69), ('promotion_min_launch_buys',9),
    ('profile_min_evaluable_trades',6), ('promotion_min_winners',3), ('promotion_max_capture_rate',.19)])
def test_promising_reuses_configured_gates(report_db, tmp_path, setting, value):
    candidate(report_db); profile(report_db)
    assert ranking(report_db, tmp_path)[0]['report_class'] == 'PROMISING'
    assert ranking(report_db, tmp_path, cfg=replace(Settings(), **{setting:value}))[0]['report_class'] == 'WATCH'


@pytest.mark.parametrize('problem', ['disabled', 'stale', 'quality', 'ineligible', 'history'])
def test_validation_requires_active_internal_promotion_and_current_evidence(report_db, tmp_path, problem):
    candidate(report_db, history_complete=0 if problem == 'history' else 1)
    profile(report_db, generated_at=NOW-121 if problem == 'stale' else NOW,
            data_quality_ok=problem != 'quality', confirmation_eligible=problem != 'ineligible')
    promote(report_db, enabled=0 if problem == 'disabled' else 1)
    assert ranking(report_db, tmp_path)[0]['report_class'] != 'VALIDATED'


def test_infrastructure_excluded_but_smart_account_not_excluded(report_db, tmp_path):
    candidate(report_db); profile(report_db); promote(report_db)
    candidate(report_db, T); profile(report_db, T)
    registry = tmp_path / 'dex.json'
    registry.write_text(json.dumps({'chain_id':5042,'emitters':[{'address':W}]}))
    rows = {r['wallet']:r for r in ranking(report_db, tmp_path, cfg=replace(Settings(),trusted_dex_registry=str(registry)))}
    assert rows[W]['report_class'] == 'REJECTED'
    assert rows[W]['infrastructure_excluded']
    assert rows[T]['report_class'] == 'PROMISING'
    assert not rows[T]['infrastructure_excluded']  # No RPC/get_code or EOA heuristic.


def test_watchlist_and_persisted_identity_flags(report_db, tmp_path):
    candidate(report_db, promotion_reason='funding_unknown_or_related')
    profile(report_db)
    (tmp_path/'generated.json').write_text(json.dumps({'wallets':[{'address':W.upper()}]}))
    (tmp_path/'history.json').write_text(json.dumps({'generated_at':100,'ranked_wallets':[
        {'wallet':W,'funded_by':T,'identity_cluster':POOL}]}))
    report_db.save_runtime_event(event(identity_id=T))
    row = ranking(report_db, tmp_path)[0]
    assert row['in_generated_watchlist'] is True
    assert row['historical_funder'] == T
    assert row['historical_identity_cluster'] == POOL
    assert row['last_runtime_identity_cluster'] == T
    assert 'funding_unknown_or_related' in row['promotion_blockers']
    assert row['report_class'] == 'PROMISING'  # A file entry grants no promotion.


def test_top_limits_console_only_csv_and_summary_are_full(report_db, tmp_path, capsys):
    candidate(report_db); profile(report_db)
    candidate(report_db, T, history_complete=0)
    rows = ranking(report_db, tmp_path)
    path = tmp_path/'ranking.csv'
    write_report(rows, path, top=1)
    output = capsys.readouterr().out
    assert W in output and T not in output
    assert 'total=2' in output
    with path.open(encoding='utf-8-sig', newline='') as f:
        exported = list(csv.DictReader(f))
    assert len(exported) == 2
    assert exported[0]['forward_hit_rate_pct'] == '40.0'
    assert exported[1]['forward_hit_rate_pct'] == ''


def test_empty_and_missing_database(report_db, tmp_path, capsys):
    assert ranking(report_db, tmp_path) == []
    write_report([], tmp_path/'empty.csv')
    assert 'total=0' in capsys.readouterr().out
    missing = tmp_path/'absent.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        build_ranking(missing)
    assert not missing.exists()


def test_malformed_profile_fails_closed(report_db, tmp_path):
    candidate(report_db); profile(report_db); promote(report_db)
    report_db.db.execute("UPDATE wallet_profiles SET payload='bad json'")
    report_db.commit()
    row = ranking(report_db, tmp_path)[0]
    assert row['report_class'] == 'WATCH'
    assert row['data_status'] == 'INCOMPLETE'


def test_no_epoch_means_unknown_forward_counts(report_db, tmp_path):
    candidate(report_db); profile(report_db)
    report_db.db.execute('DELETE FROM observation_epochs'); report_db.commit()
    row = ranking(report_db, tmp_path)[0]
    assert row['forward_buys_observed'] is None
    assert row['report_class'] == 'WATCH'


@pytest.mark.parametrize('top', ['0','-1'])
def test_invalid_top(top):
    with pytest.raises(SystemExit) as exc:
        main(['--top',top])
    assert exc.value.code == 2
