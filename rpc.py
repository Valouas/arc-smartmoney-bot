from __future__ import annotations
from web3 import Web3
from functools import lru_cache
from typing import Any
import logging
from dex_registry import TrustedDexRegistry
from rpc_transport import RPCError, ResilientRPCProvider, SharedRateLimiter, SharedComputeLimiter

USDC_ERC20 = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
NATIVE_MIRROR = Web3.to_checksum_address("0xfffffffffffffffffffffffffffffffffffffffe")
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name":"","type":"string"}], "type":"function"},
    {"constant": True, "inputs": [], "name": "name", "outputs": [{"name":"","type":"string"}], "type":"function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name":"","type":"uint8"}], "type":"function"},
    {"constant": True, "inputs": [], "name": "totalSupply", "outputs": [{"name":"","type":"uint256"}], "type":"function"},
    {"constant": True, "inputs": [{"name":"account","type":"address"}], "name": "balanceOf", "outputs":[{"name":"","type":"uint256"}], "type":"function"},
]
TRANSFER_TOPIC = Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)"))

class ArcRPC:
    def __init__(self, url: str, chain_id: int):
        self.chain_id = chain_id
        from config import SETTINGS
        limiter = (SharedComputeLimiter(SETTINGS.rpc_rate_limit_file,
                       SETTINGS.rpc_requests_per_second, SETTINGS.rpc_compute_units_per_second)
                   if SETTINGS.rpc_compute_units_per_second else
                   SharedRateLimiter(SETTINGS.rpc_rate_limit_file, SETTINGS.rpc_requests_per_second))
        self.w3 = Web3(ResilientRPCProvider(url, chain_id,
            limiter,
            SETTINGS.rpc_fallback_urls,
            fallback_limiter=SharedRateLimiter(
                SETTINGS.rpc_rate_limit_file + '.fallback',
                SETTINGS.rpc_fallback_requests_per_second) if SETTINGS.rpc_fallback_urls else None))
        self.dex_registry = TrustedDexRegistry(SETTINGS.trusted_dex_registry,chain_id)
        if not self.dex_registry.entries:
            logging.getLogger(__name__).warning("No reviewed DEX deployments configured; swaps remain research-only")

    def sanity_check(self) -> None:
        got = self.w3.eth.chain_id
        if got != self.chain_id:
            raise RuntimeError(f"Wrong chain: expected {self.chain_id}, RPC returned {got}")

    def block_number(self) -> int:
        return self.w3.eth.block_number

    def get_block(self, number: int, full_transactions: bool = True):
        return self.w3.eth.get_block(number, full_transactions=full_transactions)

    def watched_participants(self, block_number: int, wallets: list[str], block_hash=None):
        # Indexed Transfer topics include ERC-20 and Arc's native mirror. Reading
        # both sides also finds relayed/smart-account actions absent from tx.from.
        result = {}
        for offset in range(0,len(wallets),100):
            batch = wallets[offset:offset+100]
            topics = ["0x"+"0"*24+w[2:].lower() for w in batch]
            for position in (1,2):
                query = [TRANSFER_TOPIC,topics] if position==1 else [TRANSFER_TOPIC,None,topics]
                block_filter = ({"blockHash": Web3.to_hex(block_hash)} if block_hash is not None
                                else {"fromBlock": block_number, "toBlock": block_number})
                for log in self.w3.eth.get_logs({**block_filter, "topics":query}):
                    if len(log["topics"]) != 3: continue
                    addr = "0x"+Web3.to_hex(log["topics"][position])[-40:]
                    if addr.lower() in batch:
                        result.setdefault(Web3.to_hex(log["transactionHash"]).lower(),set()).add(addr.lower())
        return result

    def get_receipt(self, tx_hash):
        return self.w3.eth.get_transaction_receipt(tx_hash)

    def get_code(self, address: str) -> bytes:
        return bytes(self.w3.eth.get_code(Web3.to_checksum_address(address)))

    @lru_cache(maxsize=4096)
    def swap_emitter_trusted(self, address, block_number):
        if address.lower() not in self.dex_registry.entries: return False
        # Historical code identity, never today's code substituted for old state.
        code = bytes(self.w3.eth.get_code(Web3.to_checksum_address(address),block_identifier=block_number))
        return self.dex_registry.trusts(address,code)

    @lru_cache(maxsize=4096)
    def token_meta(self, address: str) -> dict[str, Any]:
        addr = Web3.to_checksum_address(address)
        c = self.w3.eth.contract(address=addr, abi=ERC20_ABI)
        out = {"address": addr, "symbol": "?", "name": "?", "decimals": None, "total_supply": None}
        try: out["symbol"] = c.functions.symbol().call()
        except RPCError: raise
        except Exception: pass
        try: out["name"] = c.functions.name().call()
        except RPCError: raise
        except Exception: pass
        try: out["decimals"] = int(c.functions.decimals().call())
        except RPCError: raise
        except Exception as exc: raise ValueError("token decimals unavailable") from exc
        try: out["total_supply"] = int(c.functions.totalSupply().call())
        except RPCError: raise
        except Exception: pass
        return out
