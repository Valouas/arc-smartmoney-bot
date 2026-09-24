"""One pinned native-USDC input, research only; not a general native route rule."""
import hashlib
import json

TX_HASH = '0x27e17dd914c5b06231cda320431bed456a574e66a2478931505bb936c041666a'
BLOCK = 22083268
WALLET = '0x06b98521ba2cfe389a4ffbd48279ee9f0752bfb8'
ROUTER = '0x9689992f5b5c09447f15906d8d11214944488341'
TOKEN = '0xfa0aaca99f7cd65c726957b79294c00c8b58498a'
LOGS_SHA256 = '80a668855f988acb061af1e9a52845651955c44654576ef2bdb6e64991345667'
INPUT_SHA256 = '39879ed3384196ab7901ec45e5f497130f1f80267ed987ca339fa6a3d24a2db3'


def reviewed_native_payment(tx, receipt, wallet, trade):
    """Exact cached transaction and complete logs; caller must prove both swaps.

    Observed 90 native USDC splits into 0.9 router fee and 89.1 pool input.
    The pool input has equal native/6-decimal ERC20 mirror logs. Full receipt
    pin preserves the fee and output-token fee rather than discarding them.
    A matching packet alone never confirms a swap or admits a wallet.
    """
    try:
        if (wallet != WALLET or tx['from'].lower() != WALLET
                or tx['to'].lower() != ROUTER or tx['hash'].lower() != TX_HASH
                or receipt['transactionHash'].lower() != TX_HASH
                or trade['side'] != 'BUY' or trade['token'].lower() != TOKEN
                or int(tx['value'],16) != 90*10**18
                or int(tx['blockNumber'],16) != BLOCK
                or receipt['blockNumber'] != BLOCK or receipt['status'] != 1
                or tx['blockHash'] != receipt['blockHash']):
            return None
        logs=receipt['logs']
        if hashlib.sha256(json.dumps(logs,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=LOGS_SHA256:
            return None
        if hashlib.sha256(bytes.fromhex(tx['input'].removeprefix('0x'))).hexdigest()!=INPUT_SHA256:
            return None
        if not logs or any(l['blockHash']!=receipt['blockHash'] or l['transactionHash'].lower()!=TX_HASH
                           or int(l['blockNumber'],16)!=BLOCK or l.get('removed') for l in logs):
            return None
        return dict(method='PINNED_NATIVE_USDC_INPUT',tx_hash=TX_HASH,block=BLOCK,
                    gross_usdc_raw=90_000_000,router_fee_usdc_raw=900_000,
                    pool_input_usdc_raw=89_100_000,logs_sha256=LOGS_SHA256,
                    engine_authority=False)
    except (ValueError,TypeError,KeyError,AttributeError):
        return None
