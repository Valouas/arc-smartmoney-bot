import csv
import http.client
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import dashboard
from paper_copy.store import SCHEMA


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'paper.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.executemany('INSERT INTO state VALUES(?,?)', [
            ('application', 'arc_paper_copy_v1'), ('policy', '{"initial_cash":"1000"}'),
            ('next_block', '200'), ('secret', 'DO_NOT_EXPOSE')])
        db.execute('INSERT INTO wallets VALUES(?,?,?,?,?,?,?)',
                   ('0xabc', '970', 1, 1, '{}', '{"admission":"PAPER_REVIEW","secret":"DO_NOT_EXPOSE"}', 1))
        db.execute('INSERT INTO positions VALUES(?,?,?,?)', ('0xabc', '0xtoken', '5', '50'))
        db.execute('INSERT INTO positions VALUES(?,?,?,?)', ('0xabc', '0xclosed', '0', '0'))
        for ident, side, at, pnl in [('buy', 'BUY', 86400, '0'), ('sell', 'SELL', 172800, '20')]:
            db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, '0xabc', '0xtoken', side, at, '1', '1', '100', '1', '1', pnl, '{"secret":"DO_NOT_EXPOSE"}'))
    return path


def test_paper_open_cost_is_not_hidden_or_treated_as_market_value(ledger):
    data = dashboard.paper_snapshot(ledger)
    wallet = data['wallets'][0]
    assert wallet['realized_pnl'] == 20
    assert wallet['cash_only_pnl'] == -30
    assert wallet['open_cost'] == 50 and len(wallet['positions']) == 1
    assert wallet['market_equity'] is None and wallet['unrealized_pnl'] is None
    assert data['totals']['buys'] == data['totals']['sells'] == 1
    assert data['totals']['initial_cash'] == 1000
    assert data['curve'] == [{'at': 86400, 'pnl': 0}, {'at': 172800, 'pnl': 20}]


def test_only_allowlisted_ledger_fields_are_exposed(ledger):
    data = dashboard.paper_snapshot(ledger)
    assert 'DO_NOT_EXPOSE' not in json.dumps(data)
    assert data['progress'] == {'next_block': '200'}


def test_reads_do_not_modify_ledger_and_connection_rejects_writes(ledger):
    before = ledger.read_bytes()
    dashboard.paper_snapshot(ledger)
    with dashboard.read_db(ledger) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE wallets SET cash='9999'")
    assert ledger.read_bytes() == before


def test_no_fills_means_empty_curve_not_invented_profit(ledger):
    with sqlite3.connect(ledger) as db:
        db.execute('DELETE FROM fills')
    data = dashboard.paper_snapshot(ledger)
    assert data['curve'] == [] and data['last_fill_at'] is None


def test_unknown_database_is_not_treated_as_paper(ledger):
    with sqlite3.connect(ledger) as db:
        db.execute("UPDATE state SET value='other' WHERE key='application'")
    with pytest.raises(ValueError):
        dashboard.paper_snapshot(ledger)


def test_missing_or_corrupt_data_never_creates_database_or_claims_zero(tmp_path):
    data = dashboard.snapshot(tmp_path, now=100)
    assert all(data[k] is None for k in ('paper', 'history', 'analysis', 'processes'))
    assert not list(tmp_path.iterdir())
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/paper_copy.sqlite3').write_text('DO_NOT_EXPOSE')
    result = dashboard.snapshot(tmp_path, now=100)
    assert result['errors']['paper'] == 'UNAVAILABLE'
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)


@pytest.mark.parametrize('at,status,expected', [(99, 'RUNNING', 'RUNNING'), (80, 'RUNNING', 'UNKNOWN'), (99, 'STOPPED', 'UNKNOWN')])
def test_stale_process_state_is_not_reported_as_running(tmp_path, at, status, expected):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'updated_at': at, 'status': status, 'secret': 'DO_NOT_EXPOSE',
        'processes': {'bot': {'exit_code': None, 'secret': 'DO_NOT_EXPOSE'}, 'unknown': {'exit_code': None}}}))
    result = dashboard.process_snapshot(path, 100)
    assert result['components'] == [{'name': 'bot', 'status': expected}]
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)


def test_historical_export_is_separate_timestamped_and_whitelisted(tmp_path):
    path = tmp_path / 'history.csv'
    with path.open('w', newline='', encoding='utf-8') as target:
        writer = csv.DictWriter(target, fieldnames=[*dashboard.HISTORY_FIELDS, 'collection_error'])
        writer.writeheader()
        writer.writerow({'wallet': '0xabc', 'conservative_pnl_usdc': '200', 'buys': '',
                         'paper_review_eligible': 'True', 'collection_error': 'DO_NOT_EXPOSE'})
    result = dashboard.history_snapshot(path)
    assert result['exported_at'] == path.stat().st_mtime
    assert result['wallets'][0]['conservative_pnl_usdc'] == 200
    assert result['wallets'][0]['buys'] is None
    assert result['wallets'][0]['paper_review_eligible'] is True
    assert 'DO_NOT_EXPOSE' not in json.dumps(result)


def test_large_historical_detail_field_does_not_break_overview(tmp_path):
    (tmp_path/'data').mkdir()
    path = tmp_path/'data/historical_wallet_ranking.csv'
    with path.open('w', newline='', encoding='utf-8') as target:
        writer = csv.DictWriter(target, fieldnames=['wallet', 'conservative_pnl_usdc', 'buy_details'])
        writer.writeheader()
        writer.writerow({'wallet':'0xabc', 'conservative_pnl_usdc':'42',
                         'buy_details':'DO_NOT_EXPOSE' * 20000})
    body = dashboard.SnapshotCache(tmp_path).get()
    result = json.loads(body)
    assert result['history']['wallets'][0]['conservative_pnl_usdc'] == 42
    assert 'history' not in result['errors']
    assert b'DO_NOT_EXPOSE' not in body


def test_oversized_csv_only_disables_history_and_http_stays_available(tmp_path):
    (tmp_path/'data').mkdir()
    (tmp_path/'data/historical_wallet_ranking.csv').write_text('wallet,detail\nabc,'+'x'*(8*1024*1024+1))
    (tmp_path/'data/run_all_state.json').write_text(json.dumps({
        'updated_at':time.time(), 'status':'RUNNING', 'processes':{'bot':{'exit_code':None}}}))
    instance = dashboard.make_server(tmp_path, port=0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', instance.server_port, timeout=5)
    try:
        conn.request('GET', '/api/overview')
        response = conn.getresponse()
        assert response.status == 200
        result = json.loads(response.read())
        assert result['errors']['history'] == 'UNAVAILABLE'
        assert result['history'] is None
        assert result['processes']['fresh'] is True
    finally:
        conn.close(); instance.shutdown(); instance.server_close(); thread.join(timeout=2)


def test_analysis_counts_union_wallets_and_invalidated_snapshots(tmp_path):
    path = tmp_path / 'analysis.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE discovery_candidates(wallet,status,first_discovered_at,source_winners,profile_score,profile_class,history_complete);
            CREATE TABLE wallet_profiles(wallet,smart_score,profile_class);
            CREATE TABLE historical_runs(wallet,version,phase,last_attempt,completed_at);
            INSERT INTO discovery_candidates VALUES('one','WATCH',10,2,40,'WATCH',1);
            INSERT INTO wallet_profiles VALUES('one',45,'WATCH'),('two',20,'WATCH');
            INSERT INTO historical_runs VALUES('one',1,'COMPLETE',50,50),('two',0,'COMPLETE',40,40);
        ''')
    data = dashboard.analysis_snapshot(path)
    assert len(data['wallets']) == 2
    assert data['history'] == {'COMPLETE': 1, 'WAITING': 1}
    assert data['wallets'][0]['profile_score'] == 45


def test_cache_avoids_repeated_queries_until_expiry(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, 'snapshot', lambda root: calls.append(root) or {'ok': True})
    monkeypatch.setattr(dashboard.time, 'monotonic', lambda: 100)
    cache = dashboard.SnapshotCache(tmp_path)
    assert cache.get() == cache.get()
    assert calls == [tmp_path]
    monkeypatch.setattr(dashboard.time, 'monotonic', lambda: 111)
    cache.get()
    assert calls == [tmp_path, tmp_path]


@pytest.fixture
def server(tmp_path):
    instance = dashboard.make_server(tmp_path, port=0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize('method,path,headers,status', [
    ('GET', '/health', {}, 200), ('GET', '/api/overview', {}, 200),
    ('GET', '/.env', {}, 404), ('GET', '/../data/paper_copy.sqlite3', {}, 404),
    ('GET', '/api/overview', {'Host': 'attacker.example'}, 403),
    ('GET', '/api/overview', {'Sec-Fetch-Site': 'cross-site'}, 403),
    ('POST', '/api/overview', {}, 501),
])
def test_local_http_surface_is_read_only(server, method, path, headers, status):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        assert response.status == status
        body = response.read()
        if path == '/api/overview' and status == 200:
            assert json.loads(body)['paper'] is None
            assert response.getheader('Access-Control-Allow-Origin') is None
            assert "frame-ancestors 'none'" in response.getheader('Content-Security-Policy')
    finally:
        conn.close()
