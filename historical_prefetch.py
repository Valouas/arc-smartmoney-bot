"""Bounded RPC reads; no database access or mutation in worker threads."""
from concurrent.futures import ThreadPoolExecutor
from threading import local


def read_transaction(rpc, tx_hash, cutoff):
    tx = rpc.w3.eth.get_transaction(tx_hash)
    if int(tx['blockNumber']) > cutoff:
        return tx, None, None
    receipt = rpc.get_receipt(tx_hash)
    block = rpc.get_block(int(tx['blockNumber']), False)
    return tx, receipt, block


def ordered_reads(hashes, cutoff, workers, factory, transform=None):
    """Yield in input order, with at most `workers` outstanding transactions.

    Each thread owns its RPC/session/provider state. On failure, later results
    are discarded; they never mark a transaction complete. Running reads finish
    before sessions close, including when the consumer fails during decoding.
    """
    state = local()
    clients = []

    def read(h):
        if not hasattr(state, 'rpc'):
            state.rpc = factory()
            clients.append(state.rpc)
        bundle = read_transaction(state.rpc, h, cutoff)
        return transform(h, bundle, state.rpc) if transform else bundle

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='historical-rpc')
    pending = []
    source = iter(hashes)
    try:
        for h in source:
            pending.append((h, pool.submit(read, h)))
            if len(pending) == workers:
                break
        while pending:
            h, future = pending.pop(0)
            yield h, future.result()
            nxt = next(source, None)
            if nxt is not None:
                pending.append((nxt, pool.submit(read, nxt)))
    finally:
        for _, future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        for rpc in clients:
            rpc.w3.provider.session.close()
