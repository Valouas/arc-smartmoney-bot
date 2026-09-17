from __future__ import annotations
import json, time, logging, sqlite3
from web3 import Web3

from config import SETTINGS
from models import WalletInfo, TradeEvent
from rpc import ArcRPC, RPCError
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
from formatting import buy_opportunity_text
from identity_graph import IdentityRefresher
from intelligence_book import IntelligenceBook
from watchlist_manager import WatchlistManager, effective_watchlist
from alert_outbox import AlertOutbox
from process_lock import ProcessLock
from wallet_policy import is_dex_infrastructure
from capital_allocation import event_sizing, sizing_policy

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
        self.proposals = ProposalQueue(self.db)
        self.outbox = AlertOutbox(self.db, self.tg)
        self.watch_manager = WatchlistManager(reload_seconds=self.cfg.watchlist_reload_seconds)
        self.watch_manager.reload(force=True)
        self.watch = self.watch_manager.items
        self.intelligence = IntelligenceBook(reload_seconds=self.cfg.intelligence_reload_seconds)
        self.identity_refresher = IdentityRefresher(self.arcscan,self.cfg.identity_cache_seconds,self.cfg.identity_workers)
        self.identities = self.identity_refresher.current
        self.clusters = ClusterBook(self.cfg.cluster_window_seconds, self.cfg.exit_cluster_window_seconds)
        self.webhooks = None
        if self.cfg.alchemy_webhook_enabled:
            from webhook_consumer import WebhookConsumer
            self.webhooks = WebhookConsumer(self)

    def refresh_watch(self):
        self.watch = effective_watchlist(self.watch_manager,self.db.db,self.cfg)
        self.identities = self.identity_refresher.poll(list(self.watch),self.cfg.watchlist_reload_seconds)

    def run(self):
        with ProcessLock(self.db.path + ".scanner.lock"):
            self.outbox.recover()
            try: self._run()
            finally: self.identity_refresher.close()

    def _run(self):
        self.rpc.sanity_check()
        head = self.rpc.block_number()
        saved_block = self.db.next_block()
        next_block = saved_block if saved_block is not None else (head if self.cfg.start_block.lower() == "latest" else int(self.cfg.start_block))
        log.info("Arc scanner V3 online | chain=%s | head=%s | wallets=%s", self.cfg.chain_id, head, len(self.watch))
        log.info("READ-ONLY + PAPER MODE. No private key, signer or broadcaster is present.")
        stop_check_at = 0.0
        while True:
            try:
                self.refresh_watch()
                if self.webhooks: self.webhooks.drain()
                head = self.rpc.block_number()
                while next_block <= head:
                    self.process_block(next_block)
                    next_block += 1
                    self.outbox.flush(limit=2)
                    if self.webhooks:
                        self.refresh_watch()
                        self.webhooks.drain()
                if time.time() >= stop_check_at:
                    for x in self.paper.evaluate_stops(self.market):
                        msg = f"🧪 {x['action']} {x['symbol']} | PnL {x['pnl_usd']:+.2f}$ | {x['reason']}"
                        log.info(msg)
                    self.proposals.export()
                    stop_check_at = time.time() + 30
                self.outbox.flush()
                time.sleep(self.cfg.poll_interval)
            except KeyboardInterrupt:
                log.info("Stopped."); return
            except RPCError as e:
                log.warning("%s", e); time.sleep(2)
            except Exception as e:
                log.exception("loop error: %s", e); time.sleep(2)

    def process_block(self, n: int):
        self.watch = {wallet:info for wallet,info in self.watch.items()
                      if not is_dex_infrastructure(wallet,self.cfg)}
        # Persist work to retry, not completion. This also protects the first
        # START_BLOCK=latest block when the process dies before any checkpoint.
        self.db.begin_block(n)
        block = self.rpc.get_block(n, full_transactions=True)
        ts = int(block["timestamp"])
        if not getattr(self, "_clusters_restored", False):
            if hasattr(self, "clusters"):
                for prior in self.db.recent_runtime_events(ts-max(self.cfg.cluster_window_seconds,self.cfg.exit_cluster_window_seconds),until=ts):
                    if is_dex_infrastructure(prior.wallet,self.cfg): continue
                    self.clusters.add(prior)
            self._clusters_restored = True
        participants = (self.rpc.watched_participants(n, list(self.watch), block['hash'])
                        if block.get('hash') is not None else self.rpc.watched_participants(n, list(self.watch)))
        for tx in block["transactions"]:
            tx_hash = Web3.to_hex(tx["hash"])
            wallets = set(participants.get(tx_hash.lower(), []))
            for key in ("from", "to"):
                addr = str(tx.get(key, "")).lower()
                if addr in self.watch: wallets.add(addr)
            if not wallets: continue
            if self.db.tx_seen(tx_hash): continue
            receipt = self.rpc.get_receipt(tx["hash"])
            if block.get('hash') is not None and receipt.get('blockHash') != block['hash']:
                provider = self.rpc.w3.provider
                raise RPCError('eth_getTransactionReceipt', 'block_mismatch', 1, provider.label(provider.active))
            self.process_transaction(tx,receipt,wallets,n,ts)
            self.db.mark_tx(tx_hash,n,ts)
        with self.db.transaction():
            for wallet in self.watch: self.db.mark_wallet_coverage(wallet,n,ts)
            self.db.checkpoint(n+1)

    def process_transaction(self, tx, receipt, wallets, n, ts, live=False):
        """Common decoder/engine path; webhook calls never mark a scanner transaction complete."""
        tx_hash = Web3.to_hex(tx["hash"])
        for wallet in sorted(wallets):
            self.db.observe_wallet(wallet,tx_hash,n,ts)
            if int(receipt["status"]) != 1: continue
            info = self.watch[wallet]
            native_value = int(tx.get("value",0) or 0) if str(tx.get("from","")).lower()==wallet else 0
            try:
                detected = classify_trade(self.rpc,receipt,info.address,self.cfg.min_usdc_flow,native_value_wei=native_value)
            except RPCError:
                raise
            except Exception as exc:
                raise RuntimeError(f"cannot decode {tx_hash}; block will retry") from exc
            from decoder import SWAP_TOPICS, hex_value
            has_swap=any(log.get("topics") and hex_value(log["topics"][0]) in SWAP_TOPICS for log in receipt.get("logs",[]))
            ambiguous=any(not d["confirmation_eligible"] for d in detected) or (has_swap and not detected)
            if ambiguous:
                self.db.db.execute("INSERT OR REPLACE INTO reconstruction_gaps VALUES(?,?,?,?)",(wallet,tx_hash,"unsupported_or_untrusted_flow",ts))
                self.db.commit()
            for d in detected:
                if live and not d["confirmation_eligible"]: continue
                intel = self.intelligence.effective(info.address,0,info.weight)
                e = TradeEvent(
                    tx_hash=tx_hash,block_number=n,timestamp=ts,wallet=info.address,
                    transaction_index=receipt.get('transactionIndex'),
                    wallet_label=info.label,wallet_weight=intel["weight"],trade_verified=d["confirmation_eligible"],
                    side=d["side"],token=d["token"],token_symbol=d["symbol"],
                    token_amount=d["token_amount"],usdc_amount=d["usdc_amount"],
                    identity_id=self.identities.identity_id(info.address),reputation_score=intel["score"],
                    identity_known=self.identities.eligible(info.address),
                    intelligence_class=intel["class"],confirmation_eligible=(intel["eligible"] and d["confirmation_eligible"]
                        and self.identities.eligible(info.address) and self.db.wallet_eligible(info.address)))
                if self.db.event_processed(e): continue
                self.handle_event(e)

    def handle_event(self, event: TradeEvent):
        if is_dex_infrastructure(event.wallet,self.cfg): return
        if self.db.event_processed(event): return
        if getattr(self.cfg,'alchemy_webhook_enabled',False):
            # Live and backfill interleave: rebuild only evidence at or before
            # this event. Never give an old signal knowledge of future buys.
            self.clusters = ClusterBook(self.cfg.cluster_window_seconds,self.cfg.exit_cluster_window_seconds)
            cutoff=event.timestamp-max(self.cfg.cluster_window_seconds,self.cfg.exit_cluster_window_seconds)
            priors=sorted(self.db.recent_runtime_events(cutoff,until=event.timestamp),
                          key=lambda e:(e.timestamp,e.block_number,e.transaction_index if e.transaction_index is not None else -1))
            for prior in priors:
                earlier = prior.block_number < event.block_number or (
                    prior.block_number == event.block_number and prior.transaction_index is not None
                    and event.transaction_index is not None and prior.transaction_index < event.transaction_index)
                if earlier and prior.timestamp <= event.timestamp and not is_dex_infrastructure(prior.wallet,self.cfg):
                    self.clusters.add(prior)
        event.sizing = {}
        if event.side == 'BUY' and event.confirmation_eligible and event.trade_verified:
            # Observational metadata cannot affect safety gates or interrupt
            # observation when an optional sizing input is unavailable.
            try:
                event.sizing = event_sizing(self.db.db, event, sizing_policy())
                event.identity_cluster_size = sum(
                    self.identities.identity_id(w) == event.identity_id for w in self.watch
                    if self.identities.eligible(w)) if event.identity_id else None
            except (ValueError, TypeError, sqlite3.Error):
                log.warning('Optional sizing metadata unavailable')
        # Re-evaluate buffered confirmations after removals, demotions or changes
        # to identity membership. Old events must not retain stale authority.
        for queues in self.clusters.events.values():
            for queue in queues.values():
                for prior in queue:
                    info = self.watch.get(prior.wallet.lower())
                    prior.confirmation_eligible = bool(info and prior.trade_verified
                        and self.identities.eligible(prior.wallet) and self.db.wallet_eligible(prior.wallet))
                    if info:
                        intel = self.intelligence.effective(prior.wallet, prior.reputation_score, info.weight)
                        prior.confirmation_eligible &= intel["eligible"]
                        prior.wallet_weight = intel["weight"]
                        current_identity = self.identities.identity_id(prior.wallet)
                        if current_identity != prior.identity_id:
                            prior.identity_cluster_size = None
                        prior.identity_id = current_identity
        cluster = self.clusters.add(event)
        cluster.risk = self.risk.inspect(event.token)
        if event.side == "BUY":
            cluster.score = score_buy_cluster(cluster, cluster.risk, self.cfg)
            cluster.status = classify_status(cluster, cluster.risk, cluster.score, self.cfg)
            if not event.confirmation_eligible and cluster.status == "VALIDATED":
                cluster.status = "WATCH"
        else:
            cluster.score = min(100, cluster.cluster_weight * 15 + cluster.independent_identities * 10)
            cluster.status = "EXIT_CLUSTER" if cluster.independent_identities >= 2 else "EXIT_EARLY"
        with self.db.transaction():
            if self.db.event_processed(event): return
            self.db.add_event(event)
            self.db.save_runtime_event(event)
            if cluster.risk and cluster.risk.market:
                self.db.add_trade_context(event,cluster.risk.market)
            signal_id=self.db.add_signal(cluster)
            if cluster.status=="VALIDATED": self.db.register_signal_outcome(signal_id,event.token,self.cfg)
            if event.side == "BUY":
                self.paper.maybe_open(cluster)
                if cluster.status == "VALIDATED":
                    proposal, created = self.proposals.add(cluster)
                    if created:
                        self.db.enqueue('buy:'+proposal['id'],buy_opportunity_text(cluster,proposal),
                                        kind='BUY_OPPORTUNITY')
            elif event.confirmation_eligible:
                self.paper.maybe_exit_on_cluster(cluster)
            self.db.mark_event_processed(event)


if __name__ == "__main__":
    ArcSmartMoneyBot().run()
