"""Opt-in public contract checks. No secrets, alerts, signing or transactions.

ARC_RUN_NETWORK=1 python -m pytest -m network -v
Availability/schema failures fail visibly; they are not counted as successful
empty histories. Normal pytest runs skip these tests.
"""
import os
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
