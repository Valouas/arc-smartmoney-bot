from __future__ import annotations
import json, time, logging, hashlib
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
from formatting import signal_text
from identity_graph import IdentityRefresher
from intelligence_book import IntelligenceBook
from watchlist_manager import WatchlistManager
from alert_outbox import AlertOutbox
from process_lock import ProcessLock

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
                self.watch_manager.reload()
                self.watch = dict(self.watch_manager.items)
                # Candidates are observed prospectively with zero authority. They
                # cannot need promotion in order to collect promotion evidence.
                for row in self.db.db.execute("SELECT wallet FROM discovery_candidates WHERE status='QUARANTINE' ORDER BY first_discovered_at LIMIT ?",(self.cfg.promotion_max_wallets,)):
                    addr=Web3.to_checksum_address(row["wallet"])
                    self.watch.setdefault(addr.lower(),WalletInfo(addr,"quarantine",0,"quarantine"))
                self.identities = self.identity_refresher.poll(list(self.watch),self.cfg.watchlist_reload_seconds)
                head = self.rpc.block_number()
                while next_block <= head:
                    self.process_block(next_block)
                    next_block += 1
                    self.outbox.flush(limit=2)
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
        # Persist work to retry, not completion. This also protects the first
        # START_BLOCK=latest block when the process dies before any checkpoint.
        self.db.begin_block(n)
        block = self.rpc.get_block(n, full_transactions=True)
        ts = int(block["timestamp"])
        if not getattr(self, "_clusters_restored", False):
            if hasattr(self, "clusters"):
                for prior in self.db.recent_runtime_events(ts-max(self.cfg.cluster_window_seconds,self.cfg.exit_cluster_window_seconds)):
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
                    intel = self.intelligence.effective(info.address,0,info.weight)
                    e = TradeEvent(
                        tx_hash=tx_hash,block_number=n,timestamp=ts,wallet=info.address,
                        wallet_label=info.label,wallet_weight=intel["weight"],trade_verified=d["confirmation_eligible"],
                        side=d["side"],token=d["token"],token_symbol=d["symbol"],
                        token_amount=d["token_amount"],usdc_amount=d["usdc_amount"],
                        identity_id=self.identities.identity_id(info.address),reputation_score=intel["score"],
                        intelligence_class=intel["class"],confirmation_eligible=(intel["eligible"] and d["confirmation_eligible"]
                            and self.identities.eligible(info.address) and self.db.wallet_eligible(info.address)))
                    if self.db.event_processed(e): continue
                    self.handle_event(e)
            self.db.mark_tx(tx_hash,n,ts)
        with self.db.transaction():
            for wallet in self.watch: self.db.mark_wallet_coverage(wallet,n,ts)
            self.db.checkpoint(n+1)

    def handle_event(self, event: TradeEvent):
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
                        prior.identity_id = self.identities.identity_id(prior.wallet)
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
        key = hashlib.sha256(f"{event.tx_hash.lower()}:{event.wallet.lower()}:{event.token.lower()}:{event.side}".encode()).hexdigest()
        with self.db.transaction():
            if self.db.event_processed(event): return
            self.db.add_event(event)
            self.db.save_runtime_event(event)
            if cluster.risk and cluster.risk.market:
                self.db.add_trade_context(event,cluster.risk.market)
            signal_id=self.db.add_signal(cluster)
            if cluster.status=="VALIDATED": self.db.register_signal_outcome(signal_id,event.token,self.cfg)
            self.db.enqueue(key,signal_text(cluster))
            if event.side == "BUY":
                paper = self.paper.maybe_open(cluster)
                if paper:
                    self.db.enqueue(key+":paper",f"PAPER BUY {paper['symbol']} | ${paper['notional']:.2f} @ {paper['price']:.10g}")
                if cluster.status == "VALIDATED":
                    proposal, created = self.proposals.add(cluster)
                    if created:
                        self.db.enqueue("proposal:"+proposal['id'],f"HUMAN REVIEW #{proposal['id']}\n{proposal['symbol']} {proposal['token']}\nNo real-money order was sent.")
            elif event.confirmation_eligible:
                self.paper.maybe_exit_on_cluster(cluster)
            self.db.mark_event_processed(event)


if __name__ == "__main__":
    ArcSmartMoneyBot().run()
