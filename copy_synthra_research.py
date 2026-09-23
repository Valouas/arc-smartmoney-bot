"""Pinned Synthra V3 evidence for offline research, never production trust."""
import re
from eth_abi import encode
from eth_abi.exceptions import EncodingError
from web3 import Web3

FACTORY = '0x6307fc239c7964942c1bffe51930e55606619c74'
INIT_HASH = '0xd57f04bba45e2c271c16c370a90dc3bbfa3e4f5c9ffff0981b979a7bcdeac7c3'
SOURCE = 'https://docs.synthra.org/assets/index-jIUJ8fq1.js'
SOURCE_SHA256 = '2627917875e2dd0aa75bdde9af1758a0140ae43b331f13ea573b2936a2ffc9b8'
# Deliberately limited to the reviewed pool/block pairs, even for identical code.
REVIEWED = {
    ('0xcee68e8e409b182388ef7a24b968c098f9642ad8', 21319266): '0xefb339874a4263f15ef0f80e033363d329d30ecf1891e3978d6d0dd156c84d08',
    ('0xcee68e8e409b182388ef7a24b968c098f9642ad8', 21319289): '0xefb339874a4263f15ef0f80e033363d329d30ecf1891e3978d6d0dd156c84d08',
    ('0xee765f55b122d7ae01cdc3dab164512245405120', 21189907): '0x1fdd0ad7935fedc5d2823ba62f299fadb20c1f8fd652881bff73ee2f31ac65e1',
}


def reviewed_pool(proof, block):
    try:
        if (type(block) is not int or proof['block'] != block
                or proof.get('status') != 'SYNTHRA_EXACT_POOL_OBSERVED'
                or proof['factory'] != FACTORY or proof['reported_factory'] != FACTORY
                or proof['init_code_hash'] != INIT_HASH
                or proof['registered_pool'] != proof['pool']):
            return False
        expected = REVIEWED.get((proof['pool'], block))
        if expected is None or proof['pool_code_hash'] != expected:
            return False
        a, b, fee = proof['token0'], proof['token1'], proof['fee']
        if (not all(isinstance(t, str) and re.fullmatch('0x[0-9a-f]{40}', t) for t in (a, b))
                or not 0 < int(a, 16) < int(b, 16)
                or type(fee) is not int or not 0 < fee < 1000000):
            return False
        salt = Web3.keccak(encode(['address', 'address', 'uint24'], [a, b, fee]))
        derived = Web3.to_hex(Web3.keccak(b'\xff' + bytes.fromhex(FACTORY[2:]) + salt + bytes.fromhex(INIT_HASH[2:]))[12:])
        code = bytes.fromhex(proof['pool_code'].removeprefix('0x'))
        return bool(derived == proof['pool'] == proof['predicted_pool']
                    and code and Web3.to_hex(Web3.keccak(code)) == expected)
    except (ValueError, TypeError, KeyError, OverflowError, EncodingError):
        return False
