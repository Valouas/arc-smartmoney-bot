"""Narrow offline explanation of reviewed Synthra launchpad BUY receipts.

Only the exact reviewed deployment/block and simple ERC20-funded form are
handled. No production emitter registry, live signal or portfolio authority.
"""
from eth_abi import decode
from eth_abi.exceptions import DecodingError
from web3 import Web3
from decoder import TRANSFER_TOPIC, hex_value, topic_to_address
from rpc import USDC_ERC20, NATIVE_MIRROR

LAUNCHPAD = '0x18d33de5eefb2f91b09385f35f6a1317659cc1f9'
BLOCK = 21189850
CODE_HASH = '0x766cc2f05d1be671f29f3f0eb7c6cb5b1cb47bba123cb47e2583b789b71b9400'
BUY = Web3.to_hex(Web3.keccak(text='buy(address,uint256,uint256,uint256)')[:4])
# Docs describe the event fields; the official app's getCurve ABI gives the
# uint128 reserve types. This typed signature exactly matches the stored topic.
TRADE = Web3.to_hex(Web3.keccak(text='Trade(address,address,bool,uint256,uint256,uint256,uint128,uint128,uint128,uint128)'))
FEE_SPLIT = Web3.to_hex(Web3.keccak(text='FeeSplit(address,uint256,uint256,uint256,uint128)'))
CASH, MIRROR = USDC_ERC20.lower(), NATIVE_MIRROR.lower()


def reviewed_code(proof):
    try:
        return bool(proof['status'] == 'EXACT_CODE_OBSERVED_REVIEW_PENDING'
            and proof['address'] == LAUNCHPAD and proof['block'] == BLOCK
            and proof['code_hash'] == CODE_HASH
            and int(proof['nativeScale()'], 16) == 10**12
            and int(proof['wrappedNative()'], 16) == 0
            and Web3.to_hex(Web3.keccak(bytes.fromhex(proof['code'][2:]))) == CODE_HASH)
    except (ValueError, TypeError, KeyError):
        return False


def prove_buy(wallet, payload, tx, receipt, info, codes):
    try:
        return _prove(wallet.lower(), payload, tx, receipt, info, codes)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, DecodingError):
        return None


def _prove(wallet, payload, tx, receipt, info, codes):
    code = codes.get((LAUNCHPAD, BLOCK), set())
    if (payload['block'] != BLOCK or len(code) != 1
            or Web3.to_hex(Web3.keccak(next(iter(code)))) != CODE_HASH
            or tx['from'].lower() != wallet or tx['to'].lower() != LAUNCHPAD
            or int(tx['value'], 16) != 0 or not info['success'] or info['swap']):
        return None
    raw = bytes.fromhex(tx['input'][2:])
    if len(raw) != 132 or '0x' + raw[:4].hex() != BUY:
        return None
    token, paid, minimum, deadline = decode(['address', 'uint256', 'uint256', 'uint256'], raw[4:])
    if token in {CASH, MIRROR, LAUNCHPAD, wallet} or paid <= 0 or (deadline and payload['timestamp'] > deadline):
        return None
    logs = receipt['logs']
    # This reviewed simple form has three transfers, one fee event and one
    # Trade. Graduation, extra transfers/events or multiple trades stay unknown.
    if len(logs) != 5 or any(l.get('removed') for l in logs):
        return None
    indices = [int(l['logIndex'], 16) for l in logs]
    if indices != sorted(set(indices)) or any(
            l['transactionHash'].lower() != payload['tx_hash']
            or int(l['blockNumber'], 16) != BLOCK
            or l['blockHash'] != receipt['blockHash']
            or int(l['transactionIndex'], 16) != payload['index'] for l in logs):
        return None
    trade = [l for l in logs if l['address'].lower() == LAUNCHPAD and hex_value(l['topics'][0]) == hex_value(TRADE)]
    fees = [l for l in logs if l['address'].lower() == LAUNCHPAD and hex_value(l['topics'][0]) == hex_value(FEE_SPLIT)]
    transfers = [l for l in logs if hex_value(l['topics'][0]) == hex_value(TRANSFER_TOPIC)]
    if len(trade) != 1 or len(fees) != 1 or len(transfers) != 3:
        return None
    event = trade[0]
    if len(event['topics']) != 3 or len(event['data']) != 514:
        return None
    for topic, expected in zip(event['topics'][1:], (token, wallet)):
        if len(topic) != 66 or int(topic[2:26], 16) or topic_to_address(topic).lower() != expected:
            return None
    buy, gross, amount, fee, *_ = decode(['bool'] + ['uint256'] * 3 + ['uint128'] * 4, bytes.fromhex(event['data'][2:]))
    if not buy or gross != paid or not 0 < amount or amount < minimum or not 0 <= fee < gross:
        return None
    f = fees[0]
    if (len(f['topics']) != 2 or len(f['data']) != 258 or len(f['topics'][1]) != 66
            or int(f['topics'][1][2:26], 16) or topic_to_address(f['topics'][1]).lower() != token):
        return None
    protocol, creator, buyback, _ = decode(['uint256'] * 3 + ['uint128'], bytes.fromhex(f['data'][2:]))
    if protocol + creator + buyback != fee:
        return None
    expected = {(CASH, wallet, LAUNCHPAD): paid, (MIRROR, wallet, LAUNCHPAD): paid * 10**12,
                (token, LAUNCHPAD, wallet): amount}
    observed = {}
    for log in transfers:
        if len(log['topics']) != 3 or len(log['data']) != 66:
            return None
        ends = []
        for topic in log['topics'][1:]:
            if len(topic) != 66 or int(topic[2:26], 16):
                return None
            ends.append(topic_to_address(topic).lower())
        key = (log['address'].lower(), *ends)
        if key in observed:
            return None
        observed[key] = int(log['data'], 16)
    if observed != expected or info['deltas'] != {CASH: -paid, MIRROR: -paid * 10**12, token: amount}:
        return None
    return dict(method='EXACT_RECONCILED_SYNTHRA_LAUNCHPAD_BUY', token=token,
                raw_token_amount=str(amount), raw_usdc_amount=str(paid), raw_fee=str(fee),
                emitter=LAUNCHPAD, block=BLOCK, event_topic=TRADE, code_hash=CODE_HASH,
                engine_authority=False, paper_selected=False)
