"""The saved Arcscan cursor includes explicit unclassified contracts."""
from unittest.mock import Mock

import pytest
import requests

from arcscan import ArcscanClient, ArcscanError
from test_audit_regressions import db, fixture
from test_launch_streams import indexer


@pytest.fixture
def page():
    return fixture('arcscan_contracts_unclassified.json')


@pytest.mark.parametrize('standard', ['unknown', 'erc721', 'erc1155', 'ERC-721', 'ERC-1155'])
def test_documented_non_erc20_rows_do_not_invalidate_contract_page(db, page, standard):
    page['items'][0]['token']['standard'] = standard
    ignored = page['items'][0]['address']['address']
    erc20 = page['items'][1]['address']['address']
    assert indexer(db, None, page)._discover_stream('contracts') == 1
    assert [r[0] for r in db.db.execute('SELECT token FROM launch_candidates')] == [erc20]
    state = db.db.execute("SELECT * FROM discovery_cursors WHERE stream='contracts'").fetchone()
    assert state['head_key'] == ignored
    assert state['cursor'] == page['page']['next']
    assert state['error'] is None


@pytest.mark.parametrize('mutation', [
    lambda p: p['items'][0]['token'].pop('standard'),
    lambda p: p['items'][0]['token'].update(standard=None),
    lambda p: p['items'][0]['token'].update(standard=''),
    lambda p: p['items'][0]['token'].update(standard=20),
    lambda p: p['items'][0]['token'].update(standard={'value':'erc20'}),
    lambda p: p['items'][0]['token'].update(standard='future-format'),
    lambda p: p['items'][0]['token'].update(standard='erc20-like'),
    lambda p: p['items'][0].update(standard='erc20'),  # Conflicts with nested unknown.
    lambda p: p['items'][0].update(standard=None),
    lambda p: p['items'][0].update(token=['erc20']),
    lambda p: p['items'][0].update(token=None),
    lambda p: p['items'][0].update(address='not-an-address'),
    lambda p: p['items'][0]['token'].update(address=p['items'][1]['token']['address']),
    lambda p: p['items'][1].pop('creation_block'),
    lambda p: p.update(items=[None]),
    lambda p: p.update(items={}),
])
def test_missing_malformed_or_ambiguous_contract_rows_fail_closed(db, page, mutation):
    mutation(page)
    with pytest.raises(ArcscanError):
        indexer(db, None, page)._discover_stream('contracts')
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0
    assert db.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(capped=True),
    lambda p: p.update(truncated=True),
    lambda p: p['ranking'].update(complete=False),
    lambda p: p.pop('ranking'),
    lambda p: p.update(page=[]),
    lambda p: p['page'].update(has_more=False),
    lambda p: p['page'].pop('next'),
])
def test_mixed_contract_page_keeps_completeness_guards(db, page, mutation):
    mutation(page)
    with pytest.raises(ArcscanError):
        indexer(db, None, page)._discover_stream('contracts')
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0


def test_ignored_contract_is_still_a_stop_boundary(db, page):
    ignored = page['items'][0]['address']['address']
    db.db.execute("INSERT INTO discovery_cursors(stream,stop_key) VALUES('contracts',?)", (ignored,))
    db.commit()
    assert indexer(db, None, page)._discover_stream('contracts') == 1
    state = db.db.execute("SELECT * FROM discovery_cursors WHERE stream='contracts'").fetchone()
    # Stop at the previous head even though this row is not an ERC-20 candidate.
    assert state['cursor'] is None and state['completed_at'] is not None
    assert state['stop_key'] == ignored


def test_only_unclassified_rows_advance_cursor_without_candidates(db, page):
    page['items'] = page['items'][:1]
    assert indexer(db, None, page)._discover_stream('contracts') == 0
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0
    assert db.db.execute('SELECT cursor FROM discovery_cursors').fetchone()[0] == page['page']['next']


def test_mixed_contract_page_still_rejects_cyclic_cursor(db, page):
    indexer(db, None, page)._discover_stream('contracts')
    before = dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone())
    with pytest.raises(ArcscanError, match='cursor cycle'):
        indexer(db, None, page)._discover_stream('contracts')
    assert dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone()) == before


def test_warning_has_specific_schema_reason_without_reflecting_payload(db, page, caplog):
    page['items'][0]['token']['standard'] = 'private-token'
    x = indexer(db, {'items':[], 'page':{'has_more':False, 'next':None}}, page)
    x.discover()
    assert 'contracts discovery incomplete: unknown deployment standard;' in caplog.text
    assert 'private-token' not in caplog.text


def test_warning_reports_sanitized_transport_reason(db, caplog, monkeypatch):
    monkeypatch.setattr('arcscan.time.sleep', lambda _: None)
    x = indexer(db, None)
    x.arcscan = ArcscanClient('https://user:private-token@example.invalid')
    x.arcscan.s.get = Mock(side_effect=requests.Timeout('private-token in raw request URL'))
    try:
        x.discover()
    finally:
        x.arcscan.s.close()
    assert 'Arcscan request failed: category=timeout attempts=3' in caplog.text
    assert 'private-token' not in caplog.text and 'example.invalid' not in caplog.text


def test_warning_does_not_print_arbitrary_value_error(db, caplog):
    x = indexer(db, None)
    x.arcscan.explore_contracts = Mock(side_effect=ValueError('private-token'))
    x.discover()
    assert 'contracts discovery incomplete: ValueError;' in caplog.text
    assert 'private-token' not in caplog.text
