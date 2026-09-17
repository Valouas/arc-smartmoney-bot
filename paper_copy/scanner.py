"""Live range scanning with atomic per-block observations and restart checkpoints."""
import json
import time
from decimal import Decimal
from web3 import Web3
from decoder import classify_trade, hex_value, wallet_token_deltas
from rpc import TRANSFER_TOPIC, ERC20_ABI
from wallet_policy import infrastructure_addresses
from .policy import size_buy


class ChainConsistencyError(RuntimeError): pass


class Scanner:
    def __init__(self, store, rpc, policy, cfg):
        self.store,self.db,self.rpc,self.policy,self.cfg = store,store.db,rpc,policy,cfg

    def sync(self, selected, head):
        previous = {r['wallet']:dict(r) for r in self.db.execute('SELECT * FROM wallets')}
        now = int(time.time())
        with self.db:
            self.db.execute('UPDATE wallets SET active=0')
            for wallet,entry in selected.items():
                # Activation is causal; existing cash, positions and baseline are
                # never reset by a selection refresh or by restarting the process.
                self.db.execute('''INSERT OR IGNORE INTO wallets
                    (wallet,cash,started_block,buy_after_block,baseline,selection,active) VALUES(?,?,?,?,?,?,1)''',
                    (wallet,self.policy.initial_cash,head+1,head+1,json.dumps(entry['baseline']),json.dumps(entry)))
                old = previous.get(wallet)
                if old and (not old['active'] or json.loads(old['selection'])['expires_at'] <= now):
                    self.db.execute('UPDATE wallets SET buy_after_block=? WHERE wallet=?',(head+1,wallet))
                self.db.execute('UPDATE wallets SET active=1,selection=? WHERE wallet=?',(json.dumps(entry),wallet))

    def watched(self):
        excluded = infrastructure_addresses(self.cfg)
        rows = self.db.execute('''SELECT wallet FROM wallets WHERE active=1 UNION
            SELECT wallet FROM positions WHERE quantity<>'0' UNION
            SELECT wallet FROM observations WHERE status='PENDING' ''')
        return sorted({r[0] for r in rows if r[0] not in excluded})

    def participants(self, first, last, wallets):
        result = {}
        for offset in range(0,len(wallets),100):
            batch = set(wallets[offset:offset+100])
            topics = ['0x'+'0'*24+w[2:] for w in sorted(batch)]
            for position in (1,2):
                query = [TRANSFER_TOPIC,topics] if position == 1 else [TRANSFER_TOPIC,None,topics]
                logs = self.rpc.w3.eth.get_logs({'fromBlock':first,'toBlock':last,'topics':query})
                if not isinstance(logs,list): raise ChainConsistencyError('Invalid log response')
                for log in logs:
                    if log.get('removed') or type(log['blockNumber']) is not int or not first <= log['blockNumber'] <= last:
                        raise ChainConsistencyError('Invalid log range')
                    if len(log['topics']) != 3: continue  # ERC-721 is not an ERC-20 Transfer.
                    if hex_value(log['topics'][0]) != hex_value(TRANSFER_TOPIC):
                        raise ChainConsistencyError('Unexpected log signature')
                    wallet = '0x'+hex_value(log['topics'][position])[-40:]
                    if wallet not in batch: raise ChainConsistencyError('Invalid indexed participant')
                    key = (int(log['blockNumber']),hex_value(log['transactionHash']))
                    item = result.setdefault(key,{'wallets':set(),'hash':hex_value(log['blockHash'])})
                    if item['hash'] != hex_value(log['blockHash']): raise ChainConsistencyError('Conflicting block hashes')
                    item['wallets'].add(wallet)
        return result

    def cycle(self, selected, on_block=None):
        head = self.rpc.block_number()
        self.sync(selected,head)
        wallets = self.watched()
        if not wallets: return 0
        safe = max(0,head-self.policy.confirmations)
        if self.store.get('next_block') is None:
            block = self.rpc.get_block(safe,False)
            with self.db:
                self.store.set('next_block',safe+1)
                self.store.set('last_hash',hex_value(block['hash']))
        first = int(self.store.get('next_block'))
        if first > safe: return 0
        previous = self.rpc.get_block(first-1,False)
        if hex_value(previous['hash']) != self.store.get('last_hash'):
            raise ChainConsistencyError('Checkpoint reorganization; simulation halted')
        last = min(safe,first+self.policy.block_window-1)
        found = self.participants(first,last,wallets)  # Any failed query leaves the cursor untouched.
        processed = 0
        for number in range(first,last+1):
            entries = [(tx,item) for (b,tx),item in found.items() if b==number]
            self.block(number,entries)
            if on_block is not None: on_block()
            processed += 1
        return processed

    def block(self, number, entries):
        header = self.rpc.get_block(number,False)
        if header['number'] != number: raise ChainConsistencyError('Wrong block header')
        block_hash = hex_value(header['hash'])
        if hex_value(header['parentHash']) != self.store.get('last_hash'):
            raise ChainConsistencyError('Parent mismatch')
        receipts = []
        for tx_hash,item in entries:
            if item['hash'] != block_hash: raise ChainConsistencyError('Log/header mismatch')
            receipt = self.rpc.get_receipt('0x'+tx_hash)
            tx = self.rpc.w3.eth.get_transaction('0x'+tx_hash)
            if (receipt['blockNumber'] != number or tx['blockNumber'] != number
                    or hex_value(receipt['blockHash']) != block_hash or hex_value(tx['blockHash']) != block_hash
                    or hex_value(receipt['transactionHash']) != tx_hash or hex_value(tx['hash']) != tx_hash):
                raise ChainConsistencyError('Receipt/transaction mismatch')
            if (type(receipt['status']) is not int or receipt['status'] not in (0,1)
                    or type(receipt['transactionIndex']) is not int or receipt['transactionIndex'] < 0):
                raise ChainConsistencyError('Invalid receipt status/index')
            receipts.append((receipt['transactionIndex'],tx_hash,item['wallets'],receipt,tx))
        observations,gaps,cumulative,balances = [],[],{},{}
        for index,tx_hash,wallets,receipt,tx in sorted(receipts,key=lambda r:(r[0],r[1])):
            for wallet in sorted(wallets):
                account = self.db.execute('SELECT * FROM wallets WHERE wallet=?',(wallet,)).fetchone()
                if number < account['started_block']: continue
                deltas = wallet_token_deltas(receipt,wallet)
                try:
                    trades = classify_trade(self.rpc,receipt,wallet,1e-18,
                        native_value_wei=int(tx.get('value',0)) if str(tx.get('from','')).lower()==wallet else 0,strict=True)
                except ValueError:
                    trades = []
                    gaps.append((tx_hash+':'+wallet,number,'AMBIGUOUS_SWAP_NOT_COPIED'))
                for trade in trades:
                    if trade['confirmation_eligible'] is not True:
                        gaps.append((tx_hash+':'+wallet,number,'UNTRUSTED_SWAP_NOT_COPIED')); continue
                    token = trade['token'].lower()
                    fraction = None
                    if trade['side']=='SELL':
                        key = (wallet,token)
                        if key not in balances:
                            contract = self.rpc.w3.eth.contract(address=Web3.to_checksum_address(token),abi=ERC20_ABI)
                            balances[key] = int(contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call(block_identifier=number-1))
                        before = balances[key]+cumulative.get(key,0)
                        raw_sold = -deltas.get(Web3.to_checksum_address(token),0)
                        if before <= 0 or not 0 < raw_sold <= before:
                            gaps.append((tx_hash+':'+wallet,number,'SOURCE_INVENTORY_INCONSISTENT')); continue
                        fraction = str(Decimal(raw_sold)/Decimal(before))
                    observations.append({'id':tx_hash+':'+wallet+':'+token,'wallet':wallet,'token':token,
                        'side':trade['side'],'block':number,'tx_index':index,'source_at':int(header['timestamp']),
                        'source_usdc':str(trade['usdc_amount']),'source_qty':str(trade['token_amount']),
                        'sell_fraction':fraction})
                # Includes transfers and ambiguous swaps: needed for exact source
                # balances before later transactions in the same block.
                for token,delta in deltas.items():
                    key = (wallet,token.lower()); cumulative[key] = cumulative.get(key,0)+delta
        if hex_value(self.rpc.get_block(number,False)['hash']) != block_hash:
            raise ChainConsistencyError('Block changed during reconstruction')
        now = int(time.time())
        with self.db:
            for obs in observations:
                if self.db.execute('SELECT 1 FROM observations WHERE id=?',(obs['id'],)).fetchone(): continue
                account = self.db.execute('SELECT * FROM wallets WHERE wallet=?',(obs['wallet'],)).fetchone()
                prior = json.loads(account['baseline'])
                sizing = size_buy(obs['source_usdc'],prior,self.policy) if obs['side']=='BUY' else {}
                status,reason = ('PENDING',None)
                if obs['side']=='BUY' and not account['active']: status,reason = 'SKIPPED','SELECTION_NOT_AUTHORIZED'
                elif obs['side']=='BUY' and number < account['buy_after_block']:
                    status,reason = 'SKIPPED','BUY_BEFORE_AUTHORIZATION'
                self.db.execute('''INSERT INTO observations
                    (id,wallet,token,side,block,tx_index,source_at,detected_at,source_usdc,source_qty,sell_fraction,budget,sizing,status,reason)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    tuple(obs[k] for k in ('id','wallet','token','side','block','tx_index','source_at'))+
                    (now,obs['source_usdc'],obs['source_qty'],obs['sell_fraction'],sizing.get('budget'),json.dumps(sizing),status,reason))
                if obs['side']=='BUY':
                    prior.append(obs['source_usdc'])
                    self.db.execute('UPDATE wallets SET baseline=? WHERE wallet=?',(json.dumps(prior),obs['wallet']))
            self.db.executemany('INSERT OR IGNORE INTO gaps VALUES(?,?,?)',gaps)
            self.store.set('last_hash',block_hash)
            self.store.set('next_block',number+1)
