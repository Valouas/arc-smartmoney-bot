"""Offline, pinned Aero Slipstream provenance for research only.

The official app's Arc (5042) deployment identifies FACTORY. The historical
pool is an EIP-1167 deterministic clone. Both the clone and its implementation
must be evidenced at the exact transaction block; trusting clone bytes alone
would fail to check the code actually executed by DELEGATECALL.
"""
from eth_abi import encode
from eth_abi.exceptions import EncodingError
from web3 import Web3

FACTORY = '0xb89df768af2cfe637ceb352c587fe8edaf491d03'
FACTORY_HASH = '0xfcb0b3e8be0294ebf546cc52702f2d1ed8981e64071f2d2e67175f01235ea9aa'
IMPLEMENTATION = '0x0df9fc8730eec048e9832b31b4bff4351158e19b'
IMPLEMENTATION_HASH = '0x24af4ff3d49c0a4eeb1ca6aca081d865c0a889d7c239017bdb78fde920ae6e02'
SOURCE = 'https://app.aero.xyz/assets/index-DyVaaWzS.js'
CREATE2_SOURCE = 'https://github.com/dromos-labs/metadex-slipstream-public/blob/contest/contracts/core/CLFactory.sol'


def reviewed_pool(proof, block=None):
    """No network or authority; membership alone never suffices."""
    try:
        if (proof.get('status') != 'MEMBERSHIP_OBSERVED_REVIEW_PENDING'
                or proof['factory'] != FACTORY
                or proof['implementation'] != IMPLEMENTATION
                or type(proof['block']) is not int or proof['block'] <= 0
                or (block is not None and proof['block'] != block)
                or proof['registered_pool'] != proof['pool']):
            return False
        a, b, spacing = proof['token0'], proof['token1'], proof['tick_spacing']
        if not 0 < int(a, 16) < int(b, 16) < 2**160 or type(spacing) is not int or not 0 < spacing < 16384:
            return False
        code = bytes.fromhex(proof['pool_code'].removeprefix('0x'))
        expected = bytes.fromhex('363d3d373d3d3d363d73' + IMPLEMENTATION[2:] + '5af43d82803e903d91602b57fd5bf3')
        if code != expected:
            return False
        for key, expected_hash in [('factory_code', FACTORY_HASH), ('implementation_code', IMPLEMENTATION_HASH)]:
            if Web3.to_hex(Web3.keccak(bytes.fromhex(proof[key].removeprefix('0x')))) != expected_hash:
                return False
            if proof[key + '_hash'] != expected_hash:
                return False
        if Web3.to_hex(Web3.keccak(code)) != proof['pool_code_hash']:
            return False
        salt = Web3.keccak(encode(['address', 'address', 'int24'], [a, b, spacing]))
        init = bytes.fromhex('3d602d80600a3d3981f3') + code
        predicted = Web3.to_hex(Web3.keccak(b'\xff' + bytes.fromhex(FACTORY[2:]) + salt + Web3.keccak(init))[12:])
        return predicted == proof['pool']
    except (ValueError, TypeError, KeyError, OverflowError, EncodingError):
        return False
