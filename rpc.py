from __future__ import annotations
from web3 import Web3
from functools import lru_cache
from typing import Any

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
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
        self.chain_id = chain_id

    def sanity_check(self) -> None:
        got = self.w3.eth.chain_id
        if got != self.chain_id:
            raise RuntimeError(f"Wrong chain: expected {self.chain_id}, RPC returned {got}")

    def block_number(self) -> int:
        return self.w3.eth.block_number

    def get_block(self, number: int, full_transactions: bool = True):
        return self.w3.eth.get_block(number, full_transactions=full_transactions)

    def get_receipt(self, tx_hash):
        return self.w3.eth.get_transaction_receipt(tx_hash)

    def get_code(self, address: str) -> bytes:
        return bytes(self.w3.eth.get_code(Web3.to_checksum_address(address)))

    @lru_cache(maxsize=4096)
    def token_meta(self, address: str) -> dict[str, Any]:
        addr = Web3.to_checksum_address(address)
        c = self.w3.eth.contract(address=addr, abi=ERC20_ABI)
        out = {"address": addr, "symbol": "?", "name": "?", "decimals": None, "total_supply": None}
        try: out["symbol"] = c.functions.symbol().call()
        except Exception: pass
        try: out["name"] = c.functions.name().call()
        except Exception: pass
        try: out["decimals"] = int(c.functions.decimals().call())
        except Exception as exc: raise ValueError("token decimals unavailable") from exc
        try: out["total_supply"] = int(c.functions.totalSupply().call())
        except Exception: pass
        return out
