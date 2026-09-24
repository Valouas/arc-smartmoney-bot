"""Opt-in, read-only checks against both public Arc RPC endpoints."""
import os

import pytest
from web3 import Web3

from rpc import ArcRPC
from rpc_check import check_rpc
from rpc_transport import ResilientRPCProvider, SharedRateLimiter

pytestmark = [pytest.mark.network, pytest.mark.skipif(
    os.getenv('ARC_RUN_NETWORK') != '1', reason='opt-in live public network')]


@pytest.mark.parametrize('url', ['https://rpc.mainnet.arc.io', 'https://rpc.arc-scan.org'],
                         ids=['arc-mainnet', 'arcscan-rpc'])
def test_live_rpc_transport(tmp_path, url):
    rpc = ArcRPC.__new__(ArcRPC)
    rpc.chain_id = 5042
    provider = ResilientRPCProvider(url, 5042, SharedRateLimiter(tmp_path / 'rate.sqlite3', 2))
    provider.session.trust_env = False
    rpc.w3 = Web3(provider)
    try:
        result = check_rpc(rpc)
        assert result['head'] >= result['block'] > 0
        assert result['provider'] == 'primary'
    finally:
        provider.session.close()
