"""Opt-in public contract checks. No secrets, alerts, signing or transactions.

ARC_RUN_NETWORK=1 python -m pytest -m network -v
Availability/schema failures fail visibly; they are not counted as successful
empty histories. Normal pytest runs skip these tests.
"""
import os
import runpy
import sys
from dataclasses import replace
from pathlib import Path
import pytest
import requests
from arcscan import ArcscanClient
from discovery_parser import extract_transfer_rows, extract_tx_hashes
from launch_parser import extract_contract_candidates, extract_tokens_from_transfers

pytestmark = [pytest.mark.network, pytest.mark.skipif(os.getenv('ARC_RUN_NETWORK') != '1', reason='opt-in live public network')]
USDC='0x3600000000000000000000000000000000000000'
ARGUS='0xece5ca8bf9220718e5727754026757512212cb3c'
WALLET='0xa6d09bfa73dcc201b6e9b30ca9cabfb59abbf26c'

@pytest.fixture
def api():
    c=ArcscanClient('https://api.arc-scan.org/v1')
    # No inherited proxy credentials or .netrc; only unauthenticated public URLs.
    c.s.trust_env=False
    yield c
    c.s.close()


def test_live_chain_identity(api):
    chain=api._json('/chain')
    assert chain['chain_id']==5042 and chain['native']['decimals']==18
    assert chain['erc20_native']['address']==USDC and chain['erc20_native']['decimals']==6
    response=api.s.post('https://rpc.arc-scan.org',json={'jsonrpc':'2.0','id':1,'method':'eth_chainId','params':[]},timeout=15)
    response.raise_for_status()
    assert int(response.json()['result'],16)==5042


@pytest.mark.parametrize('kind',['overview','facts','activity','contracts','transfers','token_transfers','concentration'])
def test_live_endpoint_contracts(api,kind):
    if kind=='overview':
        assert api.address_overview(USDC)['address']==USDC
    elif kind=='facts':
        result=api.address_facts(WALLET)
        assert isinstance(result['available'],bool) and isinstance(result['history_complete'],bool)
    elif kind=='activity':
        result=api.address_activity(USDC,limit=1)
        assert 'coverage' in result['page']
        if result['items']: assert extract_tx_hashes(result)
    elif kind=='contracts':
        assert extract_contract_candidates(api.explore_contracts(limit=1,filter_name='token'))
    elif kind=='transfers':
        assert extract_tokens_from_transfers(api.explore_token_transfers(limit=1))
    elif kind=='token_transfers':
        result=api.token_transfers(USDC,limit=1)
        assert 'coverage' in result['page'] and extract_transfer_rows(result,USDC)
    elif kind=='concentration':
        pct,holders=api.concentration(ARGUS)
        assert pct is not None and 0<=pct<=100 and holders>0


@pytest.mark.parametrize('saved_contract_cursor,page_budget,page_size', [
    (None, 2, 3),
    ('eyJhIjoiMHgyZmNmZmYyMjAwNzVhMDI0MzljMDQyNmI1Y2FkOWE2NjQ3ZTcwNmM1IiwiYiI6MjExNzMxMzZ9', 1, 100),
    ('eyJhIjoiMHg2OWZkYmI5YWE5OTNiZDM0YTRhYjFiMTM5OTRhMGIzMmUwMmFmMjk0IiwiYiI6MjExMzU1NTV9', 3, 100),
], ids=['fresh', 'saved-unclassified-page', 'saved-multi-page-retry'])
def test_live_launch_indexer_once(tmp_path, monkeypatch, saved_contract_cursor, page_budget, page_size):
    """Exercise the real --once entry point without touching the user's DB/env."""
    import config
    from storage import Storage
    script = Path(__file__).resolve().parents[1] / 'launch_indexer.py'
    monkeypatch.chdir(tmp_path)
    if saved_contract_cursor:
        storage = Storage()
        storage.db.execute("INSERT INTO discovery_cursors(stream,cursor,error) VALUES('contracts',?,'incomplete_page_or_transport')",
                           (saved_contract_cursor,))
        storage.commit()
        storage.db.close()
    monkeypatch.setattr(sys, 'argv', [str(script), '--once'])
    monkeypatch.setattr(config, 'SETTINGS', replace(
        config.SETTINGS,
        arcscan_api_url='https://api.arc-scan.org/v1',
        launch_discovery_pages=page_budget,
        launch_contract_page_size=page_size,
        launch_transfer_page_size=3,
        # This regression exercises both discovery streams, not market quotes.
        launch_max_market_checks_per_cycle=0,
    ))
    original_init = ArcscanClient.__init__
    def public_client(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.s.trust_env = False
    monkeypatch.setattr(ArcscanClient, '__init__', public_client)

    namespace = runpy.run_path(str(script), run_name='__main__')
    indexer = namespace['x']
    try:
        states = {r['stream']:dict(r) for r in indexer.db.db.execute('SELECT * FROM discovery_cursors')}
        assert set(states) == {'contracts', 'transfers'}
        for stream, state in states.items():
            assert state['error'] is None, (stream, state)
            assert state['cursor'] or state['completed_at'] is not None
        assert indexer.db.db.execute('SELECT COUNT(*) FROM launch_candidates').fetchone()[0] > 0
        if saved_contract_cursor:
            assert states['contracts']['cursor'] != saved_contract_cursor
            for address in ('0x7ed6c2f2df8971bc5f648d117c6cb931a1cd9aa8',
                            '0xac167b3d0636914785b56c19f1af83cea9ca66bf'):
                # The two live unclassified rows must not acquire deployment
                # authority. A supplemental transfer may still mention them.
                candidate = indexer.db.db.execute('SELECT deployment_block FROM launch_candidates WHERE token=?', (address,)).fetchone()
                assert candidate is None or candidate['deployment_block'] is None
    finally:
        indexer.arcscan.s.close()
        indexer.db.db.close()
