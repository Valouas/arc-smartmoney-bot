"""Paper-only fills with explicit, configurable cost and capacity assumptions."""
import hashlib
import math
import time


def finite_positive(value):
    return value is not None and math.isfinite(value) and value > 0


class PaperTrader:
    def __init__(self, storage, settings):
        self.db=storage
        self.cfg=settings
        if not (0<=settings.paper_fee_bps<10000 and 0<=settings.paper_slippage_bps<10000
                and 0<settings.paper_liquidity_fraction<=1 and 0<settings.paper_volume_fraction<=1):
            raise ValueError('Invalid paper execution assumptions')

    def _capacity(self, market):
        if not market or not finite_positive(market.price_usd) or not finite_positive(market.liquidity_usd) or not finite_positive(market.volume_m5):
            return 0.0
        return min(market.liquidity_usd*self.cfg.paper_liquidity_fraction,market.volume_m5*self.cfg.paper_volume_fraction)

    def _price(self, market, amount, side):
        impact=amount/market.liquidity_usd
        friction=self.cfg.paper_slippage_bps/10000+impact
        return market.price_usd*(1+friction if side=='BUY' else max(0,1-friction))

    def maybe_open(self, signal):
        if signal.status!='VALIDATED' or not signal.risk: return None
        m=signal.risk.market
        amount=min(self.cfg.paper_notional_usd,self._capacity(m))
        if amount<=0: return None
        token=signal.token.lower()
        key=hashlib.sha256(f'paper-buy:{token}:{signal.first_seen}'.encode()).hexdigest()
        with self.db.transaction():
            if self.db.db.execute('SELECT 1 FROM paper_fills WHERE id=?',(key,)).fetchone(): return None
            pos=self.db.get_position(token)
            if pos and pos['status']=='OPEN': return None
            price=self._price(m,amount,'BUY')
            principal=amount/(1+self.cfg.paper_fee_bps/10000)
            qty=principal/price; fee=amount-principal
            if pos:
                self.db.db.execute("DELETE FROM paper_positions WHERE token=? AND status='CLOSED'",(pos['token'],))
            self.db.db.execute("""INSERT OR REPLACE INTO paper_positions(token,symbol,opened_at,entry_price,notional_usd,quantity,status,pnl_usd)
                VALUES(?,?,?,?,?,?,'OPEN',0)""",(token,m.symbol,signal.last_seen,price,amount,qty))
            self.db.db.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?)',(key,token,'BUY',signal.last_seen,qty,price,fee,0))
        return {'action':'PAPER_BUY','token':token,'symbol':m.symbol,'price':price,'notional':amount,'qty':qty,'fee':fee,'partial':amount<self.cfg.paper_notional_usd}

    def _sell(self, token, market, timestamp, reason, key):
        capacity=self._capacity(market)
        if capacity<=0: return None
        with self.db.transaction():
            if self.db.db.execute('SELECT 1 FROM paper_fills WHERE id=?',(key,)).fetchone(): return None
            pos=self.db.get_position(token)
            if not pos or pos['status']!='OPEN': return None
            qty=min(pos['quantity'],capacity/market.price_usd)
            if qty<=0: return None
            price=self._price(market,qty*market.price_usd,'SELL')
            if price<=0: return None
            fee=qty*price*self.cfg.paper_fee_bps/10000
            basis=pos['notional_usd']*qty/pos['quantity']
            pnl=qty*price-fee-basis
            remaining=max(0,pos['quantity']-qty)
            complete=remaining<=pos['quantity']*1e-12
            total_pnl=(pos['pnl_usd'] or 0)+pnl
            self.db.db.execute("""UPDATE paper_positions SET quantity=?,notional_usd=?,status=?,closed_at=?,exit_price=?,pnl_usd=?,exit_reason=? WHERE lower(token)=lower(?)""",
                               (0 if complete else remaining,0 if complete else pos['notional_usd']-basis,'CLOSED' if complete else 'OPEN',timestamp if complete else None,price,total_pnl,reason,token))
            self.db.db.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?)',(key,token,'SELL',timestamp,qty,price,fee,pnl))
            self.db.enqueue('paper-exit:'+key,f"PAPER SELL {pos['symbol']} | realized PnL {pnl:+.2f}$ | {reason} | remaining {remaining:.8g}")
        return {'action':'PAPER_SELL','token':token,'symbol':pos['symbol'],'price':price,'pnl_usd':pnl,'reason':reason,'qty':qty,'fee':fee,'partial':not complete}

    def maybe_exit_on_cluster(self, signal):
        if signal.side!='SELL' or signal.independent_identities<1 or not signal.risk: return None
        event=max(signal.events,key=lambda e:(e.timestamp,e.tx_hash))
        key=hashlib.sha256(f'paper-sell:{signal.token.lower()}:{event.tx_hash}:{event.wallet.lower()}'.encode()).hexdigest()
        return self._sell(signal.token.lower(),signal.risk.market,signal.last_seen,'watched_wallet_sell',key)

    def evaluate_stops(self, market_client):
        closed=[]
        now=int(time.time())
        for pos in self.db.open_positions():
            m=market_client.token_snapshot(pos['token'])
            capacity=self._capacity(m)
            if capacity<=0 or not finite_positive(pos['notional_usd']): continue
            px=self._price(m,min(pos['quantity']*m.price_usd,capacity),'SELL')
            change=(pos['quantity']*px*(1-self.cfg.paper_fee_bps/10000)/pos['notional_usd']-1)*100
            reason=None
            if change<=-self.cfg.paper_stop_loss_pct: reason='paper_stop_loss'
            elif change>=self.cfg.paper_take_profit_pct: reason='paper_take_profit'
            if reason:
                key=f"stop:{pos['token']}:{pos['opened_at']}:{now//30}"
                fill=self._sell(pos['token'],m,now,reason,key)
                if fill: closed.append(fill)
        return closed
