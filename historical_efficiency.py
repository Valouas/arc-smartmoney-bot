"""Transport optimizations around the unchanged historical decoder.

Never change transaction(), parse_page(), evidence validation or qualification
here. Their existing fingerprint remains the provenance of decoded payloads.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing, nullcontext
from threading import local
import json
import time

from web3 import Web3
from historical_collector import HistoricalCollector
from rpc import ArcRPC, ERC20_ABI
from rpc_transport import RPCError


class ResearchRPC(ArcRPC):
    """Thread-owned persistent client; request only decoder-consumed metadata."""
    metadata_ttl = 60
    metadata_limit = 4096

    def __init__(self, *args):
        super().__init__(*args)
        self._metadata = OrderedDict()

    def token_meta(self, address):
        address = Web3.to_checksum_address(address)
        now = time.monotonic()
        saved = self._metadata.get(address)
        if saved and 0 <= now-saved[0] < self.metadata_ttl:
            self._metadata.move_to_end(address)
            return dict(saved[1])
        contract = self.w3.eth.contract(address=address, abi=ERC20_ABI)
        result = {'address': address, 'symbol': '?', 'decimals': None}
        try:
            result['symbol'] = contract.functions.symbol().call()
        except RPCError:
            raise
        except Exception:
            pass  # Same optional-symbol behavior as ArcRPC.token_meta.
        try:
            result['decimals'] = int(contract.functions.decimals().call())
        except RPCError:
            raise
        except Exception as error:
            raise ValueError('token decimals unavailable') from error
        # Invalid/failed metadata is never cached as an acceptable token.
        if not 0 <= result['decimals'] <= 255:
            raise ValueError('token decimals unavailable')
        self._metadata[address] = (now, dict(result))
        self._metadata.move_to_end(address)
        while len(self._metadata) > self.metadata_limit:
            self._metadata.popitem(last=False)
        return result


class PersistentReads:
    """Bounded ordered prefetch; clients and threads survive batch boundaries."""
    def __init__(self, workers, factory):
        if type(workers) is not int or workers < 1:
            raise ValueError('Positive worker count required')
        self.workers, self.factory = workers, factory
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='research-rpc')
        self.state = local()
        self.clients = []
        self.closed = False

    def _read(self, h, cutoff, transform):
        if not hasattr(self.state, 'rpc'):
            self.state.rpc = self.factory()
            self.clients.append(self.state.rpc)
        rpc = self.state.rpc
        tx = rpc.w3.eth.get_transaction(h)
        if int(tx['blockNumber']) > cutoff:
            bundle = (tx, None, None)
        else:
            receipt = rpc.get_receipt(h)
            context = getattr(rpc.w3.provider, 'historical_headers', nullcontext)
            with context():
                block = rpc.get_block(int(tx['blockNumber']), False)
            bundle = (tx, receipt, block)
        return transform(h, bundle, rpc)

    def ordered(self, hashes, cutoff, transform):
        if self.closed:
            raise RuntimeError('Reader is closed')
        source, pending = iter(hashes), []
        try:
            for h in source:
                pending.append((h, self.pool.submit(self._read, h, cutoff, transform)))
                if len(pending) == self.workers:
                    break
            while pending:
                h, future = pending.pop(0)
                yield h, future.result()
                nxt = next(source, None)
                if nxt is not None:
                    pending.append((nxt, self.pool.submit(self._read, nxt, cutoff, transform)))
        finally:
            for _, future in pending:
                future.cancel()
            # Drain before the next batch. No late result can write to SQLite.
            wait([future for _, future in pending])

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        for rpc in self.clients:
            rpc.w3.provider.session.close()


class EfficientCollector(HistoricalCollector):
    """The base collector owns schemas, cutoffs, payload semantics and reuse."""
    def __init__(self, storage, rpc, arc, cfg, *, rpc_factory=None):
        factory = rpc_factory or (lambda: ResearchRPC(cfg.rpc_url, cfg.chain_id))
        super().__init__(storage, rpc, arc, cfg, rpc_factory=factory)
        self.reads = PersistentReads(cfg.historical_rpc_workers, factory)

    def decode_queue(self, wallet, queue, run):
        snapshot = dict(run)
        def decode(h, bundle, rpc):
            return self.transaction(wallet, h, snapshot, reads=bundle, rpc=rpc)
        with closing(self.reads.ordered([r[0] for r in queue], run['cutoff_block'], decode)) as rows:
            for h, payload in rows:
                with self.db:
                    self.db.execute('UPDATE historical_transactions SET payload=? WHERE wallet=? AND tx_hash=?',
                                    (json.dumps(payload, allow_nan=False), wallet, h))

    def close(self):
        self.reads.close()
