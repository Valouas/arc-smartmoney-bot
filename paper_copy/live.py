"""Independent forward cursor; preserve the previous historical paper checkpoint.

Empty ranges are covered by both Transfer queries and a stable canonical anchor.
Blocks containing activity still use Scanner.block's ordered, atomic decoder.
"""
import time

from decoder import hex_value
from .policy import decimal
from .scanner import Scanner, ChainConsistencyError
from rpc_transport import RPCError


class LiveStore:
    def __init__(self, store):
        self.original, self.db, self.path = store, store.db, store.path

    def get(self, key):
        return self.original.get('live_'+key if key in ('next_block', 'last_hash') else key)

    def set(self, key, value):
        self.original.set('live_'+key if key in ('next_block', 'last_hash') else key, value)


class LiveScanner(Scanner):
    def __init__(self, store, rpc, policy, cfg):
        super().__init__(LiveStore(store), rpc, policy, cfg)

    def participants(self, first, last, wallets):
        """Bounded wider transport, with full validation before any checkpoint.

        Only a recognized provider range limit can split a query. Timeout, 429,
        malformed responses and chain errors still abort the staged window.
        """
        provider=self.rpc.w3.provider
        limits=getattr(provider,'paper_range_limits',None)
        if not isinstance(limits,dict):
            limits={}; provider.paper_range_limits=limits
        configured=self.cfg.paper_log_query_blocks
        def read(start,end):
            try: return self._participants_range(start,end,wallets)
            except RPCError as error:
                if error.category!='log_range' or end-start+1<=10: raise
                size=max(10,(end-start+1)//2)
                key=getattr(provider,'active',0)
                limits[key]=min(limits.get(key,configured),size)
                result={}
                for part in range(start,end+1,size):
                    result.update(read(part,min(end,part+size-1)))
                return result
        result={}; start=first
        while start<=last:
            size=min(configured,limits.get(getattr(provider,'active',0),configured))
            end=min(last,start+size-1)
            result.update(read(start,end))
            start=end+1
        return result

    def header(self, number):
        block = self.rpc.get_block(number, False)
        if type(block['number']) is not int or block['number'] != number:
            raise ChainConsistencyError('Wrong live boundary header')
        if len(hex_value(block['hash'])) != 64:
            raise ChainConsistencyError('Invalid live boundary hash')
        return block

    def initialize(self, head):
        if self.store.get('next_block') is not None:
            return
        # Starting a forward cohort cannot strand an existing copied position or
        # reorder an outstanding order. Never reset balances or historical data.
        if (any(decimal(r[0]) != 0 for r in self.db.execute('SELECT quantity FROM positions'))
                or self.db.execute("SELECT 1 FROM observations WHERE status='PENDING' LIMIT 1").fetchone()):
            raise ValueError('Live activation requires no open positions or pending orders')
        anchor = self.header(head)
        if hex_value(self.header(head)['hash']) != hex_value(anchor['hash']):
            raise ChainConsistencyError('Live activation anchor changed')
        with self.db:
            self.store.set('live_started_at', int(time.time()))
            self.store.set('live_start_block', head+1)
            old = self.store.original.get('next_block')
            if old is not None:
                self.store.set('live_uncovered_from', old)
                self.store.set('live_uncovered_to', head)
            self.store.set('next_block', head+1)
            self.store.set('last_hash', hex_value(anchor['hash']))

    def stable(self, number, expected):
        if hex_value(self.header(number)['hash']) != expected:
            raise ChainConsistencyError('Live range reorganization')

    def empty_range(self, first, last, anchor_number, anchor_hash):
        if first > last:
            return
        end = self.header(last)
        self.stable(anchor_number, anchor_hash)
        with self.db:
            self.db.execute('''INSERT INTO live_empty_ranges(first_block,last_block,end_hash,checked_at)
                VALUES(?,?,?,?)''', (first,last,hex_value(end['hash']),int(time.time())))
            self.store.set('next_block',last+1)
            self.store.set('last_hash',hex_value(end['hash']))

    def cycle(self, selected, on_block=None):
        head = self.rpc.block_number()
        self.initialize(head)
        self.sync(selected, head)
        wallets = self.watched()
        safe = max(0,head-self.policy.confirmations)
        first = int(self.store.get('next_block'))
        with self.db:
            self.store.set('live_head',head)
            self.store.set('live_checked_at',int(time.time()))
        if not wallets or first > safe:
            return 0
        self.stable(first-1,self.store.get('last_hash'))
        # Transport coverage only: frozen cash, fills and execution rules stay
        # identical, including stale-signal rejection and ordered active blocks.
        window=max(self.policy.block_window,self.cfg.paper_catchup_window_blocks)
        last = min(safe,first+window-1)
        anchor_hash = hex_value(self.header(last)['hash'])
        found = self.participants(first,last,wallets)
        # Every subrequest must have succeeded before any checkpoint is advanced.
        self.stable(last,anchor_hash)
        self.stable(first-1,self.store.get('last_hash'))
        cursor = first
        for number in sorted({b for b,_ in found}):
            self.empty_range(cursor,number-1,last,anchor_hash)
            self.stable(last,anchor_hash)
            entries = [(tx,item) for (b,tx),item in found.items() if b == number]
            self.block(number,entries)
            if on_block is not None:
                on_block()
            cursor = number+1
        self.empty_range(cursor,last,last,anchor_hash)
        return last-first+1
