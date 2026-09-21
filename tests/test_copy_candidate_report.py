import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from copy_candidate_report import main, report, score, shortlist, wilson, frequency

W = '0x' + '1'*40
OTHER = '0x' + '2'*40
MISSING = '0x' + '3'*40
ROOT = Path(__file__).resolve().parents[1]


def position(i, multiple=2, cost=100, holding=0, latent=0):
    return dict(token_address='0x'+f'{i:040x}', bought_usd=cost,
                pnl_usd_realised=cost*(multiple-1)-latent, pnl_usd_unrealised=latent,
                holding_amount=holding, token_price=1)


def payload(positions=None, opened=None, wallet=W):
    return dict(wallet=wallet, source='nansen', chain_id=5042, pages_complete=True,
                summary={'traded_times': 100},
                date={'from': '2026-07-15T00:00:00Z', 'to': '2026-09-20T23:10:14Z'},
                positions=dict(realized=positions if positions is not None else [position(1)],
                               open=opened or []), metrics=dict(total_gain=100))


@pytest.fixture
def stored(tmp_path):
    db_path, path = tmp_path/'test.sqlite3', tmp_path/'shortlist.json'
    path.write_text(json.dumps(dict(schema_version=1, purpose='copy_research_shortlist',
                                   wallets=[W, MISSING])), encoding='utf-8')
    with sqlite3.connect(db_path) as db:
        db.execute('''CREATE TABLE external_wallet_metrics(wallet,provider,snapshot_key,
                    observed_at,retrieved_at,chain_id,payload,data_complete,error_category)''')
        for w, provider, stamp, p in [(W, 'nansen', 1, payload()),
                (W, 'nansen', 2, payload([position(1, 10)])),
                (W, 'zerion', 3, payload([position(1, 100)])),
                (OTHER, 'nansen', 2, payload(wallet=OTHER)),
                (MISSING, 'nansen_summary', 2, {'summary': {'realized_pnl_usd': 100000}})]:
            db.execute('INSERT INTO external_wallet_metrics VALUES(?,?,?,?,?,?,?,0,NULL)',
                       (w, provider, str(stamp), stamp, stamp, 5042, json.dumps(p)))
    return db_path, path


def test_only_shortlist_latest_detail_no_summary_fallback_or_writes(stored):
    db, path = stored
    before = db.read_bytes(), path.read_bytes()
    result = report(db, path)
    assert len(result['rows']) == 2
    assert {r['wallet'] for r in result['rows']} == {W, MISSING}
    assert result['rows'][0]['x10'] == 1
    assert result['rows'][0]['retrieved_at'] == 2
    assert result['rows'][1]['status'] == 'MISSING_NANSEN_DETAIL'
    assert result['wallets_scored'] == result['wallets_missing_stored_detail'] == 1
    assert all(not r['live_signal_authority'] and not r['paper_selected'] for r in result['rows'])
    assert (db.read_bytes(), path.read_bytes()) == before


def test_actual_cli_top20_has_no_network_or_provider_imports(stored):
    db, path = stored
    # A display of 20 must still analyze the full 50-wallet shortlist, never
    # expand to non-shortlisted rows or fetch missing details.
    selected = [W, MISSING] + ['0x'+f'{i:040x}' for i in range(100, 148)]
    path.write_text(json.dumps(dict(schema_version=1, purpose='copy_research_shortlist',
                                   wallets=selected)), encoding='utf-8')
    code = '''
import sys, runpy, socket, builtins
def audit(event, args):
    if event.startswith(('socket.connect', 'socket.getaddrinfo', 'socket.sendto')):
        raise AssertionError('Network forbidden during scoring')
sys.addaudithook(audit)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('nansen_client', 'nansen_bulk', 'nansen_audit', 'external_http'):
        raise AssertionError('Provider import forbidden: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['copy_candidate_report.py', '--top', '20', '--db', sys.argv[1], '--shortlist', sys.argv[2]]
runpy.run_path('copy_candidate_report.py', run_name='__main__')
'''
    env = dict(os.environ, COPY_SHORTLIST_ONLY='true', PYTHON_DOTENV_DISABLED='1')
    proc = subprocess.run([sys.executable, '-c', code, str(db), str(path)], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert 'Nansen HTTP calls during scoring: 0' in proc.stdout
    assert 'Nansen credits consumed: 0' in proc.stdout
    assert '50 wallets analyses' in proc.stdout
    assert 'Wallets missing stored detail: 49' in proc.stdout
    assert len([line for line in proc.stdout.splitlines() if line.startswith('0x')]) == 20
    assert OTHER not in proc.stdout


def test_multiple_boundaries_and_open_losses_not_ignored():
    rows = [position(1, 2), position(2, 5), position(3, 10), position(4, .5, holding=10, latent=-150)]
    result = score(payload(rows, [rows[-1]]), W)
    assert result['sample_size'] == 4
    assert [result[k] for k in ('x2', 'x5', 'x10')] == [3, 2, 1]
    assert result['win_rate'] == .75
    assert result['concentration'] == pytest.approx(900/1400)


def test_one_lucky_token_wilson_and_concentration():
    one = score(payload([position(1, 100)]), W)
    many = score(payload([position(i, 10) for i in range(10)]), W)
    assert one['concentration'] == 1
    assert many['historical_copy_score'] > one['historical_copy_score']
    assert wilson(1, 1) == pytest.approx(.20655, abs=.00001)
    assert wilson(0, 0) is None


def test_absolute_pnl_never_increases_score():
    small = score(payload([position(1, 2, cost=10)]), W)
    large = score(payload([position(1, 2, cost=1000000)]), W)
    assert small['historical_copy_score'] == large['historical_copy_score']


def test_missing_database_never_created(stored):
    db, path = stored
    missing = db.with_name('absent.sqlite3')
    with pytest.raises(sqlite3.OperationalError):
        report(missing, path)
    assert not missing.exists()


@pytest.mark.parametrize('bad', [None, float('nan'), float('inf'), -1, 0, True])
def test_bad_cost_never_invents_multiple(bad):
    row = position(1)
    row['bought_usd'] = bad
    result = score(payload([row]), W)
    assert result['historical_copy_score'] is None
    assert result['sample_size'] == 0
    assert result['excluded_positions']


def test_unpriced_open_and_conflicting_views_remain_visible_unscored():
    r = position(1, holding=2)
    o = dict(r, bought_usd=300)
    result = score(payload([r], [o]), W)
    assert result['excluded_positions'][0]['reason'] == 'conflicting_views'
    o = dict(r, token_price=0)
    result = score(payload([r], [o]), W)
    assert result['status'] == 'PARTIAL_POSITION_DATA'
    assert result['historical_copy_score'] is None


def test_empty_is_not_missing_or_zero_score():
    result = score(payload([]), W)
    assert result['status'] == 'NO_EVALUABLE_POSITIONS'
    assert result['historical_copy_score'] is None


@pytest.mark.parametrize('change', [{'chain_id': 1}, {'pages_complete': False}, {'positions': {}}, {'wallet': OTHER}])
def test_invalid_detail_fail_closed(change):
    with pytest.raises(ValueError):
        score(dict(payload(), **change), W)


def test_duplicate_token_rejected():
    with pytest.raises(ValueError):
        score(payload([position(1), position(1)]), W)


def test_infrastructure_no_score_and_no_contract_heuristic(stored):
    db, path = stored
    assert report(db, path)['wallets_scored'] == 1
    result = report(db, path, excluded={W})
    assert result['wallets_scored'] == 0
    assert next(r for r in result['rows'] if r['wallet'] == W)['status'] == 'EXCLUDED_INFRASTRUCTURE'


def test_no_fallback_to_older_valid_snapshot(stored):
    db, path = stored
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO external_wallet_metrics VALUES(?,'nansen','bad',9,9,5042,'{}',0,NULL)", (W,))
    result = report(db, path)
    assert result['wallets_scored'] == 0
    assert next(r for r in result['rows'] if r['wallet'] == W)['status'] == 'INVALID_NANSEN_DETAIL'


def test_explicit_false_never_expands_scope(monkeypatch):
    monkeypatch.setenv('COPY_SHORTLIST_ONLY', 'false')
    with pytest.raises(SystemExit):
        main([])


def test_shortlist_add_remove_dedup_and_missing_file(stored):
    db, path = stored
    path.write_text(json.dumps(dict(schema_version=1, purpose='copy_research_shortlist',
                                   wallets=[OTHER, OTHER.upper().replace('0X','0x')])), encoding='utf-8')
    assert shortlist(path) == [OTHER]
    assert [r['wallet'] for r in report(db, path)['rows']] == [OTHER]
    with pytest.raises(FileNotFoundError):
        shortlist(path.with_name('absent.json'))


@pytest.mark.parametrize('count,status', [(0,'WITHIN_FREQUENCY_LIMIT'), (100,'WITHIN_FREQUENCY_LIMIT'),
                                        (101,'EXCLUDED_FREQUENCY')])
def test_frequency_boundary_inclusive(count,status):
    p=payload()
    p.update(summary={'traded_times':count},date={'from':'2026-09-01T00:00:00Z','to':'2026-09-03T00:00:00Z'})
    f=frequency(p)
    assert f['operations_per_day']==count/2
    assert f['frequency_status']==status


def test_roi_weighted_capital_and_no_double_count_of_open_view():
    rows=[position(1,10,cost=1000,holding=1,latent=100),position(2,0,cost=10)]
    r=score(payload(rows,[rows[0]]),W)
    assert r['total_buy_usd']==1010
    assert r['roi_pct']==pytest.approx(100*8990/1010)
    assert r['capital_multiple']==pytest.approx(1+8990/1010)


@pytest.mark.parametrize('rows', [[],[position(1,cost=0)]])
def test_roi_missing_sample_or_zero_cost_not_invented(rows):
    r=score(payload(rows),W)
    assert r['roi_pct'] is None and r['capital_multiple'] is None


def test_partial_roi_never_uses_only_winners():
    r=score(payload([position(1,10),position(2,cost=0)]),W)
    assert r['roi_pct'] is None


def test_roi_includes_open_loss_and_does_not_change_score():
    closed=position(1,1.5,cost=100)
    opened=position(2,.1,cost=100,holding=1,latent=-90)
    r=score(payload([closed],[opened]),W)
    assert r['roi_pct']==-20 and r['capital_multiple']==.8


@pytest.mark.parametrize('count', [None, -1, True, 1.5, float('inf'), '100'])
def test_unknown_operation_count_not_treated_as_zero(count):
    p=payload();p['summary']['traded_times']=count
    assert frequency(p)['frequency_status']=='UNKNOWN_FREQUENCY'


@pytest.mark.parametrize('dates', [{}, {'from':'invalid','to':'invalid'},
    {'from':'2026-09-01','to':'2026-09-03'},
    {'from':'2026-09-03T00:00:00Z','to':'2026-09-01T00:00:00Z'}])
def test_invalid_dates_no_frequency(dates):
    p=payload();p['date']=dates
    assert frequency(p)['operations_per_day'] is None


def test_frequency_uses_fractional_days_not_rounded_days():
    p=payload();p.update(summary={'traded_times':51},date={'from':'2026-09-01T00:00:00Z','to':'2026-09-02T12:00:00Z'})
    assert frequency(p)['operations_per_day']==34


def test_exclusion_preserves_shortlist_pnl_score_and_authority(stored):
    db,path=stored
    with sqlite3.connect(db) as con:
        p=payload();p['summary']['traded_times']=100000
        con.execute("UPDATE external_wallet_metrics SET payload=? WHERE wallet=? AND provider='nansen'",(json.dumps(p),W))
    before=db.read_bytes(),path.read_bytes()
    result=report(db,path)
    r=next(r for r in result['rows'] if r['wallet']==W)
    assert r['frequency_status']=='EXCLUDED_FREQUENCY'
    assert r['historical_copy_score'] is not None and r['pnl_informative']
    assert not r['copy_research_eligible'] and not r['paper_selected'] and not r['live_signal_authority']
    assert result['wallets_excluded_frequency']==1
    assert (db.read_bytes(),path.read_bytes())==before
    assert report(db,path,max_operations_per_day=100000)['copy_research_candidates']==1
