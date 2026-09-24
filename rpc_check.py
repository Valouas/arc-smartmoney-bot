"""Small, read-only smoke check of configured RPC. No scanner/alerts are started."""
import logging

from config import SETTINGS
from rpc import ArcRPC, USDC_ERC20
from rpc_transport import RPCError


def check_rpc(rpc):
    rpc.sanity_check()
    head = rpc.block_number()
    number = max(0, head - 2)
    block = rpc.get_block(number, full_transactions=False)
    if block['number'] != number:
        raise ValueError('Unexpected block number')
    logs = rpc.w3.eth.get_logs({'fromBlock': number, 'toBlock': number, 'address': USDC_ERC20})
    if any(row['blockNumber'] != number or row['blockHash'] != block['hash'] for row in logs):
        raise ValueError('Inconsistent log block')
    rpc.watched_participants(number, [USDC_ERC20.lower()], block['hash'])
    if block['transactions']:
        receipt = rpc.get_receipt(block['transactions'][0])
        if receipt['blockHash'] != block['hash']:
            raise ValueError('Inconsistent receipt block')
    if not rpc.get_code(USDC_ERC20):
        raise ValueError('Native ERC20 code unavailable')
    if rpc.token_meta(USDC_ERC20)['decimals'] != 6:
        raise ValueError('Native ERC20 decimals mismatch')
    return {'head': head, 'block': number, 'logs': len(logs),
            'provider': rpc.w3.provider.label(rpc.w3.provider.active)}


def main():
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    rpc = ArcRPC(SETTINGS.rpc_url, SETTINGS.chain_id)
    try:
        result = check_rpc(rpc)
    except RPCError as error:
        print(f'RPC FAILED | {error}')
        return 1
    except Exception:
        print('RPC FAILED | category=validation')
        return 1
    finally:
        rpc.w3.provider.session.close()
    print('RPC OK | ' + ' | '.join(f'{key}={value}' for key, value in result.items()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
