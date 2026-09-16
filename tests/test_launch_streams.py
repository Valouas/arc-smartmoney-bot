"""A supplemental transfer page is not a certified deployment universe."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arcscan import ArcscanClient, ArcscanError
from config import Settings
from forward_metrics import build_forward_profile, collect_outcomes
from launch_indexer import LaunchIndexer
from models import MarketSnapshot
from test_audit_regressions import db, fixture, W, T
from test_followup import setup_forward


@pytest.fixture
def transfer_page(monkeypatch):
    data = fixture('arcscan_transfers_supplemental.json')
    # Keep the captured page inside the supplemental initial time window.
    monkeypatch.setattr('launch_indexer.time.time', lambda: max(r['timestamp'] for r in data['items']) + 1)
    return data


def indexer(storage, transfers, contracts=None):
    obj = LaunchIndexer.__new__(LaunchIndexer)
    obj.db = storage
    obj.cfg = replace(Settings(), launch_discovery_pages=1, launch_transfer_page_size=3)
    obj.arcscan = SimpleNamespace(
        explore_contracts=lambda **kwargs: contracts,
        explore_token_transfers=lambda **kwargs: transfers,
        next_cursor=ArcscanClient.next_cursor,
    )
    return obj


def test_live_shaped_supplemental_page_is_accepted_and_clears_error(db, transfer_page):
    assert transfer_page['page']['next'] and transfer_page['page']['has_more'] is True
    assert transfer_page['ranking']['complete'] is False
    assert all('tx_hash' in r and 'log_index' in r for r in transfer_page['items'])
    db.db.execute("INSERT INTO discovery_cursors(stream,error) VALUES('transfers','incomplete_page_or_transport')")
    db.commit()

    assert indexer(db, transfer_page)._discover_stream('transfers') > 0

    state = db.db.execute("SELECT * FROM discovery_cursors WHERE stream='transfers'").fetchone()
    assert state['cursor'] == transfer_page['page']['next']
    assert state['error'] is None and state['completed_at'] is None
    assert json.loads(state['seen_cursors']) == [transfer_page['page']['next']]
    assert all(r['deployment_block'] is None and r['deployed_at'] is None
               for r in db.db.execute('SELECT * FROM launch_candidates'))


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(capped=True),
    lambda p: p.update(truncated=True),
    lambda p: p.pop('items'),
    lambda p: p.update(items={}),
    lambda p: p.pop('page'),
    lambda p: p.update(page=['malformed']),
    lambda p: p['page'].update(has_more='true'),
    lambda p: p['page'].update(has_more=False),
    lambda p: p['page'].pop('next'),
    lambda p: p['items'][0].pop('tx_hash'),
    lambda p: p['items'][0].pop('log_index'),
])
def test_supplemental_pages_keep_integrity_guards(db, transfer_page, mutation):
    mutation(transfer_page)
    with pytest.raises(ArcscanError):
        indexer(db, transfer_page)._discover_stream('transfers')
    assert db.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0] == 0
    assert db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] == 0


def test_supplemental_cursor_cycle_after_restart_keeps_saved_state(db, transfer_page):
    indexer(db, transfer_page)._discover_stream('transfers')
    previous = dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone())
    with pytest.raises(ArcscanError, match='cursor cycle'):
        indexer(db, transfer_page)._discover_stream('transfers')
    assert dict(db.db.execute('SELECT * FROM discovery_cursors').fetchone()) == previous


@pytest.mark.parametrize('ranking', [None, {}, {'complete':False}, {'complete':1}, {'complete':'true'}, []])
def test_contracts_require_explicit_complete_ranking(db, ranking):
    page = fixture('arcscan_live_3.json')
    page['ranking'] = ranking
    with pytest.raises(ArcscanError):
        indexer(db, None, page)._discover_stream('contracts')
    assert db.db.execute('SELECT COUNT(*) FROM discovery_cursors').fetchone()[0] == 0


def test_completed_transfers_do_not_establish_deployment_epoch(db, transfer_page):
    transfer_page['page'].update(has_more=False, next=None)
    contracts = fixture('arcscan_live_3.json')
    contracts['ranking']['complete'] = False
    _, transfers = indexer(db, transfer_page, contracts).discover()
    assert transfers > 0
    assert db.db.execute("SELECT completed_at FROM discovery_cursors WHERE stream='transfers'").fetchone()[0]
    assert db.db.execute('SELECT COUNT(*) FROM observation_epochs').fetchone()[0] == 0
    assert db.db.execute('SELECT COUNT(*) FROM promoted_wallets WHERE enabled=1').fetchone()[0] == 0
    p = build_forward_profile(db, W, Settings())
    assert not p['data_quality_ok'] and not p['confirmation_eligible']


def test_transfers_cannot_replace_contract_health_for_existing_profiles(db):
    cfg = setup_forward(db)
    build_forward_profile(db, W, cfg, now=50)
    collect_outcomes(db, SimpleNamespace(token_snapshot=lambda _: MarketSnapshot(T, price_usd=30)), now=126)
    assert build_forward_profile(db, W, cfg, now=130)['confirmation_eligible']
    db.db.execute("DELETE FROM discovery_cursors WHERE stream='contracts'")
    db.db.execute("INSERT INTO discovery_cursors(stream,completed_at) VALUES('transfers',130)")
    db.commit()
    p = build_forward_profile(db, W, cfg, now=130)
    assert not p['data_quality_ok'] and not p['confirmation_eligible']
