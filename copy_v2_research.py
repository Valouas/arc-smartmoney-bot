"""Pinned V2 provenance for offline holding research, no live authority."""
import re
from web3 import Web3

FACTORY = '0x89e5db8b5aa49aa85ac63f691524311aeb649eba'
FACTORY_HASH = '0xbab145d02e7005f0d84c6c1639d39b799b0ea16df99ebbdaf5a14d9da820b4e0'
INIT_HASH = '0x96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f'
POOL = '0x4a2fc1fbe246f797894ba3d993a39c1e4879a101'
POOL_HASH = '0x5b83bdbcc56b2e630f2807bbadd2b0c21619108066b92a58de081261089e9ce5'
BLOCKS = {21196066, 21197159}

# DYOR's archived frontend maps ARC=5042 to this factory and init hash.
# Only the two independently checked historical blocks are reviewed here.
DYOR_POOL = '0xcf924acee7eb1f169a922bf19b0a732810971985'
REVIEWED = {
    POOL: (FACTORY, FACTORY_HASH, INIT_HASH, POOL_HASH, BLOCKS,
           'CANONICAL_V2_EXACT_BLOCK'),
    DYOR_POOL: (
        '0x942bd5bfdc5317c5507e326f8eb4bb6058ab5c10',
        '0x5e4d0bba019e88c06f4d3b09c66febd6226321cad381ae2670da5febefeb1e09',
        '0x92ddb604a61717111df354b619e8fb1a1440b9f3dbfcbdeb95792375de4c4257',
        '0x9d4b521d72caceb24f2ceb056b66f89b5bebc70bf8f765ec830b099872bbc873',
        {21148353, 21173493}, 'REVIEWED_DYOR_V2_EXACT_BLOCK'),
}


def reviewed_pool(proof, block):
    """Exact reviewed blocks, reciprocal membership, CREATE2 and code hashes."""
    try:
        pool = proof['pool']
        factory, factory_hash, init_hash, pool_hash, blocks, status = REVIEWED[pool]
        if (type(block) is not int or block not in blocks or proof['block'] != block
                or type(proof['block']) is not int
                or proof['status'] != status
                or proof['factory'] != factory or proof['init_code_hash'] != init_hash
                or proof['registered_pool'] != pool or proof['predicted_pool'] != pool):
            return False
        a, b = proof['token0'], proof['token1']
        if (not all(isinstance(t, str) and re.fullmatch(r'0x[0-9a-f]{40}', t) for t in (a, b))
                or not 0 < int(a, 16) < int(b, 16)):
            return False
        salt = Web3.keccak(bytes.fromhex(a[2:] + b[2:]))
        derived = Web3.to_hex(Web3.keccak(b'\xff' + bytes.fromhex(factory[2:])
            + salt + bytes.fromhex(init_hash[2:]))[12:])
        if derived != pool:
            return False
        for key, hash_key, expected in [('code', 'runtime_code_hash', pool_hash),
                                         ('factory_code', 'factory_code_hash', factory_hash)]:
            if proof[hash_key] != expected or Web3.to_hex(Web3.keccak(bytes.fromhex(proof[key].removeprefix('0x')))) != expected:
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
