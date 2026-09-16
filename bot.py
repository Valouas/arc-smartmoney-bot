from __future__ import annotations
import json, time, logging
from pathlib import Path
from web3 import Web3

from config import SETTINGS
from models import WalletInfo, TradeEvent
from rpc import ArcRPC
from decoder import classify_trade
from market_data import DexScreenerClient
from arcscan import ArcscanClient
from risk_engine import RiskEngine
from cluster import ClusterBook
from scoring import score_buy_cluster, classify_status
from storage import Storage
from paper_trader import PaperTrader
from telegram_alerts import TelegramAlerts
from proposal_queue import ProposalQueue
from formatting import signal_text
from reputation import ReputationBook
from identity_graph import IdentityGraph
from intelligence_book import IntelligenceBook
from watchlist_manager import WatchlistManager

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("arc-bot")

class ArcSmartMoneyBot:
    def __init__(self):
        self.cfg = SETTINGS
        self.rpc = ArcRPC(self.cfg.rpc_url, self.cfg.chain_id)
        self.market = DexScreenerClient(self.cfg.dexscreener_chain_id)
        self.arcscan = ArcscanClient(self.cfg.arcscan_api_url)
        self.risk = RiskEngine(self.rpc, self.market, self.arcscan, self.cfg)
        self.db = Storage()
        self.paper = PaperTrader(self.db, self.cfg)
        self.tg = TelegramAlerts(self.cfg.telegram_bot_token, self.cfg.telegram_chat_id)
        self.proposals = ProposalQueue()
        self.watch_manager = WatchlistManager(reload_seconds=self.cfg.watchlist_reload_seconds)
        self.watch_manager.reload(force=True)
        self.watch = self.watch_manager.items
        self.reputation = ReputationBook()
        self.intelligence = IntelligenceBook(reload_seconds=self.cfg.intelligence_reload_seconds)
        self.identities = IdentityGraph(self.arcscan)
        self.identities.refresh([w.address for w in self.watch.values()])
        self.clusters = ClusterBook(self.cfg.cluster_window_seconds, self.cfg.exit_cluster_window_seconds)

    def run(self):
        self.rpc.sanity_check()
        head = self.rpc.block_number()
        next_block = head if self.cfg.start_block.lower() == "latest" else int(self.cfg.start_block)
        log.info("Arc scanner V3 online | chain=%s | head=%s | wallets=%s", self.cfg.chain_id, head, len(self.watch))
        log.info("READ-ONLY + PAPER MODE. No private key, signer or broadcaster is present.")
        stop_check_at = 0.0
        while True:
            try:
                if self.watch_manager.reload():
                    old = set(self.watch)
                    self.watch = self.watch_manager.items
                    new_addresses = [w.address for k,w in self.watch.items() if k not in old]
                    if new_addresses:
                        self.identities = IdentityGraph(self.arcscan)
                        self.identities.refresh([w.address for w in self.watch.values()])
                        log.info("watchlist expanded by %s wallets", len(new_addresses))
                head = self.rpc.block_number()
                while next_block <= head:
                    self.process_block(next_block)
                    next_block += 1
                if time.time() >= stop_check_at:
                    for x in self.paper.evaluate_stops(self.market):
                        msg = f"🧪 {x['action']} {x['symbol']} | PnL {x['pnl_usd']:+.2f}$ | {x['reason']}"
                        log.info(msg); self.tg.send(msg)
                    stop_check_at = time.time() + 30
                time.sleep(self.cfg.poll_interval)
            except KeyboardInterrupt:
                log.info("Stopped."); return
            except Exception as e:
                log.exception("loop error: %s", e); time.sleep(2)

    def process_block(self, n: int):
        block = self.rpc.get_block(n, full_transactions=True)
        ts = int(block["timestamp"])
        for tx in block["transactions"]:
            frm = str(tx.get("from", "")).lower()
            info = self.watch.get(frm)
            if not info: continue
            tx_hash = tx["hash"].hex()
            if self.db.tx_seen(tx_hash): continue
            try:
                receipt = self.rpc.get_receipt(tx["hash"])
                if int(receipt["status"]) != 1:
                    self.db.mark_tx(tx_hash, n, ts); continue
                detected = classify_trade(self.rpc, receipt, info.address, self.cfg.min_usdc_flow, native_value_wei=int(tx.get("value", 0) or 0))
            except Exception as e:
                log.warning("cannot decode %s: %s", tx_hash, e); continue
            self.db.mark_tx(tx_hash, n, ts)
            for d in detected:
                old_rep = self.reputation.get(info.address)
                intel = self.intelligence.effective(info.address, old_rep.score, info.weight)
                e = TradeEvent(
                    tx_hash=tx_hash, block_number=n, timestamp=ts,
                    wallet=info.address, wallet_label=info.label, wallet_weight=intel["weight"],
                    side=d["side"], token=d["token"], token_symbol=d["symbol"],
                    token_amount=d["token_amount"], usdc_amount=d["usdc_amount"],
                    identity_id=self.identities.identity_id(info.address),
                    reputation_score=intel["score"], intelligence_class=intel["class"],
                    confirmation_eligible=intel["eligible"],
                )
                self.reputation.mark_signal(info.address); self.reputation.save(); self.db.add_event(e); self.handle_event(e)

    def handle_event(self, event: TradeEvent):
        cluster = self.clusters.add(event)
        cluster.risk = self.risk.inspect(event.token)
        if cluster.risk and cluster.risk.market:
            self.db.add_trade_context(event, cluster.risk.market)
        if event.side == "BUY":
            cluster.score = score_buy_cluster(cluster, cluster.risk, self.cfg)
            cluster.status = classify_status(cluster, cluster.risk, cluster.score, self.cfg)
        else:
            cluster.score = min(100, cluster.cluster_weight * 15 + cluster.independent_identities * 10)
            cluster.status = "EXIT_CLUSTER" if cluster.independent_identities >= 2 else "EXIT_EARLY"
        self.db.add_signal(cluster)
        msg = signal_text(cluster)
        print("\n" + msg + "\n", flush=True); self.tg.send(msg)
        if event.side == "BUY":
            paper = self.paper.maybe_open(cluster)
            if paper:
                pmsg = f"🧪 PAPER BUY {paper['symbol']} | ${paper['notional']:.2f} @ {paper['price']:.10g}"
                log.info(pmsg); self.tg.send(pmsg)
            if cluster.status == "VALIDATED":
                p = self.proposals.add(cluster)
                self.tg.send(f"📝 HUMAN REVIEW #{p['id']}\n{p['symbol']} {p['token']}\nReference price: {p['reference_price_usd']}\nNo real-money order was sent.")
        else:
            paper = self.paper.maybe_exit_on_cluster(cluster)
            if paper:
                pmsg = f"🧪 PAPER SELL {paper['symbol']} | PnL {paper['pnl_usd']:+.2f}$ | {paper['reason']}"
                log.info(pmsg); self.tg.send(pmsg)

if __name__ == "__main__":
    ArcSmartMoneyBot().run()
