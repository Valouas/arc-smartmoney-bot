"""Stage bounded log windows, then keep the existing per-block engine/checkpoints."""
from web3 import Web3
from rpc import TRANSFER_TOPIC
from rpc_transport import RPCError


def mismatch():
    return RPCError('eth_getLogs', 'range_consistency', 1, 'primary')


def prepare_window(rpc, first, last, wallets):
    if not 1 <= last-first+1 <= 10:
        raise ValueError('Scanner window must contain 1 to 10 blocks')
    wallets = sorted(set(w.lower() for w in wallets))
    blocks, found = {}, {}
    for n in range(first,last+1):
        b = rpc.get_block(n, full_transactions=True)
        if b.get('number') != n or not b.get('hash') or (n>first and b.get('parentHash') != blocks[n-1]['hash']):
            raise mismatch()
        blocks[n] = b; found[n] = {}
    for offset in range(0,len(wallets),100):
        batch = set(wallets[offset:offset+100])
        topics = ['0x'+'0'*24+w[2:] for w in sorted(batch)]
        for position in (1,2):
            query = [TRANSFER_TOPIC,topics] if position==1 else [TRANSFER_TOPIC,None,topics]
            logs = rpc.w3.eth.get_logs({'fromBlock':first,'toBlock':last,'topics':query})
            if not isinstance(logs,list): raise mismatch()
            for row in logs:
                n = row.get('blockNumber')
                if (type(n) is not int or n not in blocks or row.get('removed')
                        or row.get('blockHash') != blocks[n]['hash']): raise mismatch()
                if len(row['topics']) != 3: continue  # ERC-721 is not a fungible transfer.
                if Web3.to_hex(row['topics'][0]).lower() != TRANSFER_TOPIC.lower(): raise mismatch()
                w = '0x'+Web3.to_hex(row['topics'][position])[-40:]
                if w not in batch: raise mismatch()
                h = Web3.to_hex(row['transactionHash']).lower()
                if not any(Web3.to_hex(t['hash']).lower()==h for t in blocks[n]['transactions']): raise mismatch()
                found[n].setdefault(h,set()).add(w)
    # Even an empty log response must belong to the same canonical window.
    if rpc.get_block(last,False)['hash'] != blocks[last]['hash']: raise mismatch()
    return [(n,blocks[n],found[n]) for n in range(first,last+1)]
