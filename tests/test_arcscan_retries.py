"""Bounded retries never turn transport/schema failure into accepted coverage."""
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import pytest
import requests

from arcscan import ArcscanClient, ArcscanError
from test_audit_regressions import db, T, W
from test_followup import contract_page
from test_launch_streams import indexer


def response(status=200, payload=None, retry_after=None):
    headers = {} if retry_after is None else {'Retry-After':retry_after}
    result = Mock(status_code=status, headers=headers)
    result.json.return_value = {'items':[]} if payload is None else payload
    return result


@pytest.fixture
def client(monkeypatch):
    obj = ArcscanClient('https://user:private-token@example.invalid')
    obj.s.get = Mock()
    monkeypatch.setattr('arcscan.time.sleep', Mock())
    monkeypatch.setattr('arcscan.random.uniform', Mock(return_value=0.125))
    yield obj
    obj.s.close()


@pytest.mark.parametrize('failure,category', [
    (429,'429'), (500,'500'), (502,'502'), (503,'503'), (504,'504'),
    (requests.Timeout('private-token'),'timeout'),
    (requests.ConnectionError('private-token'),'connection'),
])
def test_transient_failure_then_success_retries_identical_page(client, failure, category, caplog):
    import arcscan
    first = response(failure) if isinstance(failure,int) else failure
    success = response(payload=contract_page(T))
    client.s.get.side_effect = [first, success]
    assert client.explore_contracts(cursor='private-token', page_index=2) == contract_page(T)
    assert client.s.get.call_count == 2
    assert client.s.get.call_args_list[0] == client.s.get.call_args_list[1]
    assert 'page_index' not in client.s.get.call_args.kwargs['params']
    arcscan.time.sleep.assert_called_once_with(0.625)
    assert f'category={category} page=2 attempt=1/3' in caplog.text
    assert 'private-token' not in caplog.text and 'example.invalid' not in caplog.text
    success.close.assert_called_once()
    if isinstance(failure,int): first.close.assert_called_once()


def test_retry_after_seconds_overrides_local_backoff(client):
    import arcscan
    first = response(429, retry_after='7')
    client.s.get.side_effect = [first, response()]
    def sleep(seconds):
        assert seconds == 7
        first.close.assert_called_once()  # Release connection before waiting.
    arcscan.time.sleep.side_effect = sleep
    assert client._json('/endpoint') == {'items':[]}


def test_retry_after_http_date_is_respected(client, monkeypatch):
    import arcscan
    now = 1700000000
    monkeypatch.setattr('arcscan.time.time', lambda: now)
    header = format_datetime(datetime.fromtimestamp(now + 12, timezone.utc), usegmt=True)
    client.s.get.side_effect = [response(503, retry_after=header), response()]
    client._json('/endpoint')
    arcscan.time.sleep.assert_called_once_with(12)


@pytest.mark.parametrize('header', ['private-token', '-1', '', '0', 'Wed, 01 Jan 2020 00:00:00 GMT'])
def test_invalid_or_elapsed_retry_after_keeps_local_backoff(client, header, caplog):
    import arcscan
    client.s.get.side_effect = [response(429, retry_after=header), response()]
    client._json('/endpoint')
    arcscan.time.sleep.assert_called_once_with(0.625)
    assert 'private-token' not in caplog.text


def test_exponential_backoff_with_jitter_stops_at_three_attempts(client):
    import arcscan
    client.s.get.side_effect = [response(500), response(502), response(504), response()]
    with pytest.raises(ArcscanError, match='category=504 attempts=3'):
        client._json('/private-token')
    assert client.s.get.call_count == 3
    assert [call.args[0] for call in arcscan.time.sleep.call_args_list] == [0.625, 1.125]
    assert arcscan.random.uniform.call_count == 2


@pytest.mark.parametrize('status', [400, 401, 403, 404, 408, 422, 501, 505])
def test_other_http_errors_are_not_retried(client, status):
    import arcscan
    client.s.get.side_effect = [response(status), response()]
    with pytest.raises(ArcscanError, match=f'category={status} attempts=1'):
        client._json('/endpoint')
    assert client.s.get.call_count == 1
    arcscan.time.sleep.assert_not_called()


@pytest.mark.parametrize('payload', [{'error':'private-token'}, {'truncated':True}, 'private-token'])
def test_error_or_partial_payload_is_never_retried_or_accepted(client, payload):
    import arcscan
    client.s.get.side_effect = [response(payload=payload), response()]
    with pytest.raises(ArcscanError, match='category=invalid_payload attempts=1'):
        client._json('/endpoint')
    assert client.s.get.call_count == 1
    arcscan.time.sleep.assert_not_called()


def test_invalid_json_is_not_retried(client):
    import arcscan
    bad = response()
    bad.json.side_effect = ValueError('private-token')
    client.s.get.side_effect = [bad, response()]
    with pytest.raises(ArcscanError, match='category=invalid_json attempts=1'):
        client._json('/endpoint')
    assert client.s.get.call_count == 1
    arcscan.time.sleep.assert_not_called()


def test_other_request_errors_do_not_retry_or_leak_details(client, caplog):
    import arcscan
    client.s.get.side_effect = requests.RequestException('private-token')
    with pytest.raises(ArcscanError, match='category=request_error attempts=1') as exc:
        client._json('/endpoint')
    assert client.s.get.call_count == 1
    assert 'private-token' not in str(exc.value) + caplog.text
    arcscan.time.sleep.assert_not_called()


def saved_indexer(db, client):
    db.db.execute("INSERT INTO discovery_cursors(stream,cursor,head_key,seen_cursors) VALUES('contracts','saved','head','[]')")
    db.commit()
    obj = indexer(db, None)
    obj.cfg = replace(obj.cfg, launch_discovery_pages=3)
    obj.arcscan = client
    return obj


def test_three_5xx_keep_entire_saved_cursor_state(client, db):
    obj = saved_indexer(db, client)
    before = dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone())
    client.s.get.side_effect = [response(503), response(503), response(503)]
    with pytest.raises(ArcscanError, match='category=503 attempts=3'):
        obj._discover_stream('contracts')
    assert dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone()) == before
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0
    assert [call.kwargs['params']['cursor'] for call in client.s.get.call_args_list] == ['saved'] * 3


def test_later_page_failure_keeps_last_valid_page_and_reports_index(client, db, caplog):
    obj = saved_indexer(db, client)
    client.s.get.side_effect = [response(payload=contract_page(T, 'page2')),
        response(502), response(502), response(502),
        response(payload={'items':[], 'page':{'next':None, 'has_more':False}})]
    obj.discover()
    state = db.db.execute("SELECT * FROM discovery_cursors WHERE stream='contracts'").fetchone()
    assert state['cursor'] == 'page2' and state['head_key'] == 'head'
    assert state['error'] == 'incomplete_page_or_transport' and state['completed_at'] is None
    assert [r[0] for r in db.db.execute('SELECT token FROM launch_candidates')] == [T]
    assert [call.kwargs['params']['cursor'] for call in client.s.get.call_args_list[:4]] == ['saved', 'page2', 'page2', 'page2']
    assert 'category=502 page=2 attempt=3/3' in caplog.text
    assert 'contracts discovery incomplete: Arcscan request failed: category=502 attempts=3; page=2/3;' in caplog.text
    assert db.db.execute('SELECT COUNT(*) FROM observation_epochs').fetchone()[0] == 0


@pytest.mark.parametrize('defect', ['missing_standard', 'capped'])
def test_successful_http_retry_with_invalid_page_still_cannot_advance(client, db, defect):
    obj = saved_indexer(db, client)
    before = dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone())
    bad = contract_page(W, 'must-not-advance')
    if defect == 'missing_standard': bad['items'][0].pop('standard')
    else: bad['capped'] = True
    client.s.get.side_effect = [response(429), response(payload=bad), response(payload=contract_page(T))]
    with pytest.raises(ArcscanError):
        obj._discover_stream('contracts')
    assert client.s.get.call_count == 2
    assert dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone()) == before
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0
