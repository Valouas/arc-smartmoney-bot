"""Bounded live work within the scanner's single engine ownership lock."""
import logging
import time
from decoder import hex_value
from wallet_policy import is_dex_infrastructure
from webhook_queue import SCHEMA

log=logging.getLogger(__name__)


class WebhookConsumer:
    def __init__(self,bot):
        self.bot,self.db=bot,bot.db.db
        self.db.executescript(SCHEMA)

    def finish(self,tx,wallet,status,error=None):
        with self.db:
            self.db.execute('UPDATE alchemy_jobs SET status=?,error=? WHERE tx_hash=? AND wallet=?',(status,error,tx,wallet))

    def process(self,tx_hash,wallet):
        bot=self.bot
        if wallet not in bot.watch or is_dex_infrastructure(wallet,bot.cfg):
            self.finish(tx_hash,wallet,'IGNORED','NOT_IN_EFFECTIVE_WATCHLIST'); return
        tx=bot.rpc.w3.eth.get_transaction(tx_hash)
        receipt=bot.rpc.get_receipt(tx_hash)
        n=tx['blockNumber']
        if type(n) is not int or n<0: raise ValueError('Transaction not mined')
        block=bot.rpc.get_block(n,False)
        if (hex_value(tx['hash'])!=hex_value(tx_hash) or hex_value(receipt['transactionHash'])!=hex_value(tx_hash)
                or receipt['blockNumber']!=n or block['number']!=n
                or receipt['blockHash']!=block['hash'] or tx['blockHash']!=block['hash']
                or type(receipt['status']) is not int or receipt['status'] not in (0,1)):
            raise ValueError('Canonical receipt mismatch')
        # The callback is a hint, never an authoritative chain/RPC response.
        if bot.rpc.block_number()<n+2: raise ValueError('Awaiting confirmations')
        if not 0 <= int(time.time())-int(block['timestamp']) <= 120:
            self.finish(tx_hash,wallet,'IGNORED','STALE_LIVE_TRIGGER_BACKFILL_REMAINS'); return
        if bot.rpc.get_block(n,False)['hash']!=block['hash']: raise ValueError('Block changed')
        bot.process_transaction(tx,receipt,{wallet},n,int(block['timestamp']),live=True)
        self.finish(tx_hash,wallet,'DONE')

    def drain(self,limit=3):
        now=int(time.time())
        rows=self.db.execute("SELECT * FROM alchemy_jobs WHERE status='PENDING' AND next_attempt<=? ORDER BY next_attempt,rowid LIMIT ?",(now,limit)).fetchall()
        for row in rows:
            try: self.process(row['tx_hash'],row['wallet'])
            except Exception as error:
                # Independent retry queue: a failure cannot advance the historical
                # checkpoint, and cannot prevent the backfill from running.
                category=type(error).__name__
                with self.db:
                    self.db.execute('UPDATE alchemy_jobs SET attempts=attempts+1,next_attempt=?,error=? WHERE tx_hash=? AND wallet=?',
                        (now+min(60,2**min(row['attempts']+1,6)),category,row['tx_hash'],row['wallet']))
                log.warning('Webhook trigger deferred | category=%s',category)
