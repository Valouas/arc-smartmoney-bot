"""Paper fills at detection-time quotes, never at the source wallet's entry price."""
import json
import time
from decimal import Decimal
from .policy import decimal


def quote_data(market, token, now):
    if not market or str(market.token).lower() != token: return None
    try:
        values = {k: decimal(getattr(market,k)) for k in ('price_usd','liquidity_usd','volume_m5')}
        if any(v <= 0 for v in values.values()): return None
        return {**{k:str(v) for k,v in values.items()}, 'observed_at':now,
                'pair':market.pair_address, 'source':'DexScreener_detection_time',
                'model':'spot_plus_slippage_and_liquidity_impact; not an executable route quote'}
    except (ValueError, AttributeError): return None


class Execution:
    def __init__(self, store, policy, market, clock=time.time):
        self.store,self.db,self.policy,self.market = store,store.db,policy,market
        self.clock = clock

    def pending(self, now):
        # Process in source order. A pending quote blocks later orders for that
        # wallet, avoiding sells overtaking buys or changing its cash allocation.
        blocked = set()
        for row in self.db.execute("SELECT * FROM observations WHERE status='PENDING' ORDER BY block,tx_index,id").fetchall():
            if row['wallet'] in blocked: continue
            if not self.fill(dict(row),now): blocked.add(row['wallet'])

    def finish(self, order, reason):
        with self.db:
            self.db.execute("UPDATE observations SET status='SKIPPED',reason=? WHERE id=? AND status='PENDING'",(reason,order['id']))
        return True

    def fill(self, order, now):
        cfg = self.policy
        if not 0 <= now-order['source_at'] <= cfg.max_signal_age:
            return self.finish(order,'STALE_SIGNAL_NO_RETROACTIVE_FILL')
        account = self.db.execute('SELECT * FROM wallets WHERE wallet=?',(order['wallet'],)).fetchone()
        position = self.db.execute('SELECT * FROM positions WHERE wallet=? AND token=?',(order['wallet'],order['token'])).fetchone()
        if order['side'] == 'BUY' and (not account['active'] or json.loads(account['selection'])['expires_at'] <= now):
            return self.finish(order,'SELECTION_NOT_AUTHORIZED')
        if order['side']=='BUY' and order['block'] < account['buy_after_block']:
            return self.finish(order,'BUY_BEFORE_AUTHORIZATION')
        if order['side'] == 'SELL' and (not position or decimal(position['quantity']) <= 0):
            return self.finish(order,'NO_COPIED_POSITION')
        # Each attempt gets a fresh quote. No receipt/source price fallback.
        market = self.market.token_snapshot(order['token'])
        now = int(self.clock())
        if not 0 <= now-order['source_at'] <= cfg.max_signal_age:
            return self.finish(order,'STALE_SIGNAL_NO_RETROACTIVE_FILL')
        if order['side']=='BUY' and json.loads(account['selection'])['expires_at'] <= now:
            return self.finish(order,'SELECTION_NOT_AUTHORIZED')
        quote = quote_data(market,order['token'],now)
        if quote is None:
            with self.db:
                self.db.execute("UPDATE observations SET attempts=attempts+1,reason='QUOTE_UNAVAILABLE' WHERE id=?",(order['id'],))
            return False
        spot,liquidity,volume = (decimal(quote[k]) for k in ('price_usd','liquidity_usd','volume_m5'))
        capacity = min(liquidity*decimal(cfg.liquidity_fraction),volume*decimal(cfg.volume_fraction))
        fee_rate,slippage,gas = decimal(cfg.fee_bps)/10000,decimal(cfg.slippage_bps)/10000,decimal(cfg.gas_usdc)
        cash = decimal(account['cash'])
        if order['side'] == 'BUY':
            # 10..100 is the TOTAL cash debit, including the simulated costs.
            budget = min(decimal(order['budget']),cash)
            if budget < decimal(cfg.min_buy) or budget <= gas: return self.finish(order,'INSUFFICIENT_CASH')
            notional = (budget-gas)/(1+fee_rate)
            if notional > capacity: return self.finish(order,'INSUFFICIENT_MARKET_CAPACITY')
            price = spot*(1+slippage+notional/liquidity)
            qty,fee,pnl = notional/price,notional*fee_rate,Decimal(0)
            old_qty = decimal(position['quantity']) if position else Decimal(0)
            old_cost = decimal(position['cost']) if position else Decimal(0)
            remaining,cost,cash = old_qty+qty,old_cost+budget,cash-budget
        else:
            fraction = decimal(order['sell_fraction'])
            if not 0 < fraction <= 1: return self.finish(order,'INVALID_SOURCE_SELL_FRACTION')
            qty = decimal(position['quantity'])*fraction
            # No invented partial exit: inability to simulate the requested sale
            # is retained explicitly; retry only until the signal expires.
            if qty*spot > capacity:
                with self.db:
                    self.db.execute("UPDATE observations SET attempts=attempts+1,reason='INSUFFICIENT_MARKET_CAPACITY' WHERE id=?",(order['id'],))
                return False
            price = spot*(1-slippage-qty*spot/liquidity)
            if price <= 0: return self.finish(order,'INVALID_EXECUTION_PRICE')
            notional = qty*price
            fee = notional*fee_rate
            proceeds = notional-fee-gas
            if cash+proceeds < 0: return self.finish(order,'INSUFFICIENT_CASH_FOR_EXIT_COSTS')
            basis = decimal(position['cost'])*fraction
            remaining = decimal(position['quantity'])-qty
            if not remaining: remaining = Decimal(0)
            cost,cash,pnl = decimal(position['cost'])-basis,cash+proceeds,proceeds-basis
        with self.db:
            # Single worker lock, plus idempotency on the durable observation ID.
            if self.db.execute('SELECT 1 FROM fills WHERE observation_id=?',(order['id'],)).fetchone(): return True
            self.db.execute('UPDATE wallets SET cash=? WHERE wallet=?',(str(cash),order['wallet']))
            self.db.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?)',(order['wallet'],order['token'],str(remaining),str(cost)))
            self.db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (order['id'],order['wallet'],order['token'],order['side'],now,str(qty),str(price),str(notional),str(fee),str(gas),str(pnl),json.dumps(quote)))
            self.db.execute("UPDATE observations SET status='FILLED',reason=NULL,attempts=attempts+1 WHERE id=?",(order['id'],))
        return True
