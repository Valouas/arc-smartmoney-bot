"""Explicit deployment trust; a Swap topic is not a pool identity."""
import json
from pathlib import Path
from web3 import Web3


class TrustedDexRegistry:
    def __init__(self, path, chain_id):
        self.entries = {}
        try:
            data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict): return
            if data.get('chain_id') != chain_id: return
            for item in data.get('emitters',[]):
                address=Web3.to_checksum_address(item['address'])
                code_hash=item['runtime_code_hash'].lower()
                if len(code_hash)!=66 or not code_hash.startswith('0x'): continue
                int(code_hash[2:],16)
                self.entries[address.lower()]=code_hash
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            self.entries={}

    def trusts(self, address, code):
        expected=self.entries.get(address.lower())
        return bool(expected and code and Web3.to_hex(Web3.keccak(code)).lower()==expected)
