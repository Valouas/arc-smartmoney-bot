"""Campaign-only bounded reads; historical decoder identity remains unchanged."""
from concurrent.futures import ThreadPoolExecutor
from threading import local


def ordered_tasks(hashes, workers, factory, task):
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
        return task(h, state.rpc)

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
