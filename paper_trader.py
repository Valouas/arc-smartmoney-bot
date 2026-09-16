from __future__ import annotations
import time
import math

class PaperTrader:
    """Simulation only. It never signs or broadcasts transactions."""
    def __init__(self, storage, settings):
        self.db = storage
        self.cfg = settings

    def maybe_open(self, signal):
        if signal.status != "VALIDATED" or not signal.risk or not signal.risk.market:
            return None
        m = signal.risk.market
        if not m.price_usd or not math.isfinite(m.price_usd) or m.price_usd <= 0:
            return None
        if self.db.get_position(signal.token):
            return None
        notional = self.cfg.paper_notional_usd
        qty = notional / m.price_usd
        if not self.db.open_position(signal.token, m.symbol, signal.last_seen, m.price_usd, notional, qty): return None
        return {"action":"PAPER_BUY","token":signal.token,"symbol":m.symbol,"price":m.price_usd,"notional":notional,"qty":qty}

    def maybe_exit_on_cluster(self, signal):
        if signal.side != "SELL": return None
        pos = self.db.get_position(signal.token)
        if not pos or pos["status"] != "OPEN": return None
        if signal.independent_identities < 1: return None
        if not signal.risk or not signal.risk.market or not signal.risk.market.price_usd: return None
        px = signal.risk.market.price_usd
        if not math.isfinite(px) or px <= 0: return None
        pnl = pos["quantity"] * px - pos["notional_usd"]
        if not self.db.close_position(signal.token, signal.last_seen, px, pnl, "watched_wallet_sell"): return None
        return {"action":"PAPER_SELL","token":signal.token,"symbol":pos["symbol"],"price":px,"pnl_usd":pnl,"reason":"watched_wallet_sell"}

    def evaluate_stops(self, market_client):
        closed = []
        for pos in self.db.open_positions():
            m = market_client.token_snapshot(pos["token"])
            if not m or not m.price_usd or not math.isfinite(m.price_usd) or m.price_usd <= 0: continue
            change_pct = (m.price_usd / pos["entry_price"] - 1) * 100
            reason = None
            if change_pct <= -self.cfg.paper_stop_loss_pct: reason = "paper_stop_loss"
            elif change_pct >= self.cfg.paper_take_profit_pct: reason = "paper_take_profit"
            if reason:
                pnl = pos["quantity"] * m.price_usd - pos["notional_usd"]
                if not self.db.close_position(pos["token"], int(time.time()), m.price_usd, pnl, reason): continue
                closed.append({"action":"PAPER_SELL","token":pos["token"],"symbol":pos["symbol"],"price":m.price_usd,"pnl_usd":pnl,"reason":reason})
        return closed
