"""Pinned reviewed third-party distributions, never a generic gift rule.

The original receipt and input remain preserved. No BUY/SELL, PnL, identity
independence or engine authority is inferred from this receipt annotation.
"""
import hashlib
import json
from eth_abi import decode
from eth_abi.exceptions import DecodingError

TRANSACTION = '0x28c537dff9efb992ac8301cfbe28b89d055fb7cfaf34354f57442336a5a9a3fe'
BLOCK = 21186465
BLOCK_HASH = '0xbbd6425a92afa11623d807327ad421818bc6dae1e518c8d710ae326aa5d1a134'
PAYER = '0xad7c8ae8f80d791d473a9611af581536d711baf2'
DISTRIBUTOR = '0x69fc5e25a07bd1e185d0cd24f844bad74af8ba44'
TOKEN = '0x228be7649b3c5370fdffa5c0d630e020dffd3eb7'
INPUT_SHA256 = 'bb48fd09e9e7e91538c2d02216665c6119f9ee7bb164765903aea38ca3386a98'
LOGS_SHA256 = '076aabfd1bf5e9855b924abd205f17b6c598c809ed70d8c3f1f7b2b83f17ed5d'
REVIEWED = {
    TRANSACTION: (BLOCK, BLOCK_HASH, INPUT_SHA256, LOGS_SHA256),
    '0x2d4e7f8be1a8b10427866b3211b8893d5389e49fdaa38415a40047598206273b': (
        21185506,
        '0x596739b89d093a5e4f13037cedbcb4ccf57f61c289e57f5de703c36c024fc724',
        '35ab8314209a0f443b7c62a4c71a34010ceaefaec9fadecbb2ac1e6b79daa4f7',
        '33830f3483a127bcefc9a8ea9205aa58029679c088ce91519eb70c4a587aabc7'),
}


def reviewed_distribution(wallet, tx, receipt, info, disposed, traded):
    """Caller must supply complete snapshot coverage and all token disposals."""
    try:
        wallet=wallet.lower()
        transaction=tx['hash']
        block,block_hash,input_sha,logs_sha=REVIEWED[transaction]
        if (receipt['transactionHash'] != transaction
                or tx['blockHash'] != block_hash or receipt['blockHash'] != block_hash
                or tx['blockNumber'] != hex(block) or receipt['blockNumber'] != hex(block)
                or tx['from'].lower() != PAYER or tx['to'].lower() != DISTRIBUTOR
                or wallet in {PAYER,DISTRIBUTOR} or not info['success']
                or info['cash_touch'] or info['deltas'] != {TOKEN:10**18}
                or TOKEN in disposed or TOKEN in traded):
            return None
        calldata=bytes.fromhex(tx['input'].removeprefix('0x'))
        if hashlib.sha256(calldata).hexdigest()!=input_sha:
            return None
        log_bytes=json.dumps(receipt['logs'],sort_keys=True,separators=(',',':')).encode()
        if hashlib.sha256(log_bytes).hexdigest()!=logs_sha:
            return None
        cost,_,recipients=decode(['uint256','uint256','address[]'],calldata[4:])
        if len(recipients)!=500 or len(set(recipients))!=500 or wallet not in recipients:
            return None
        if int(tx['value'],16)!=cost*10**12:
            return None
        return dict(reason='REVIEWED_THIRD_PARTY_DISTRIBUTION',tx_hash=transaction,
                    payer=PAYER,distributor=DISTRIBUTOR,token=TOKEN,recipient_count=500,
                    received_raw=str(10**18),wallet_payment_observed=False,
                    observed_disposal=False,engine_authority=False,
                    identity_independence_inferred=False)
    except (ValueError,TypeError,KeyError,AttributeError,DecodingError):
        return None
