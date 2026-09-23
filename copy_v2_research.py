"""Pinned Uniswap V2 provenance for offline holding research, no live authority."""
import re
from web3 import Web3

FACTORY = '0x89e5db8b5aa49aa85ac63f691524311aeb649eba'
FACTORY_HASH = '0xbab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0'
INIT_HASH = '0x96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f'
POOL = '0x4a2fc1fbe246f797894ba3d993a39c1e4879a101'
POOL_HASH = '0x5b83bdbcc56b2e630f2807bbadd2b0c21619108066b92a58de081261089e9ce5'
BLOCKS = {21196066, 21197159}


def reviewed_pool(proof, block):
    """Exact reviewed blocks, reciprocal membership, CREATE2 and code hashes."""
    try:
        if (type(block) is not int or block not in BLOCKS or proof['block'] != block
                or type(proof['block']) is not int
                or proof['status'] != 'CANONICAL_V2_EXACT_BLOCK'
                or proof['factory'] != FACTORY or proof['init_code_hash'] != INIT_HASH
                or proof['pool'] != POOL or proof['registered_pool'] != POOL
                or proof['predicted_pool'] != POOL):
            return False
        a, b = proof['token0'], proof['token1']
        if (not all(isinstance(t, str) and re.fullmatch(r'0x[0-9a-f]{40}', t) for t in (a, b))
                or not 0 < int(a, 16) < int(b, 16)):
            return False
        salt = Web3.keccak(bytes.fromhex(a[2:] + b[2:]))
        derived = Web3.to_hex(Web3.keccak(b'\xff' + bytes.fromhex(FACTORY[2:])
            + salt + bytes.fromhex(INIT_HASH[2:]))[12:])
        if derived != POOL:
            return False
        for key, hash_key, expected in [('code', 'runtime_code_hash', POOL_HASH),
                                         ('factory_code', 'factory_code_hash', FACTORY_HASH)]:
            if proof[hash_key] != expected or Web3.to_hex(Web3.keccak(bytes.fromhex(proof[key].removeprefix('0x')))) != expected:
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
