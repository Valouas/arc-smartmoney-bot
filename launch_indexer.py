from __future__ import annotations
import argparse, time, logging
from config import SETTINGS
from storage import Storage
from arcscan import ArcscanClient
from market_data import DexScreenerClient
from launch_parser import extract_contract_candidates, extract_tokens_from_transfers

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("launch-indexer")

class LaunchIndexer:
    def __init__(self):
        self.cfg = SETTINGS
        self.db = Storage()
        self.arcscan = ArcscanClient(self.cfg.arcscan_api_url)
        self.market = DexScreenerClient(self.cfg.dexscreener_chain_id)

    def discover(self):
        contracts = self.arcscan.explore_contracts(limit=self.cfg.launch_contract_page_size, filter_name="token")
        rows = extract_contract_candidates(contracts)
        for x in rows:
            self.db.upsert_candidate(x["address"], "arcscan_contracts", x["symbol"], x["name"], x["deployer"], x["deployment_block"], x["deployed_at"])
        transfers = self.arcscan.explore_token_transfers(limit=self.cfg.launch_transfer_page_size, standard="ERC-20")
        rows2 = extract_tokens_from_transfers(transfers)
        for x in rows2:
            self.db.upsert_candidate(x["address"], "arcscan_transfers", x["symbol"], x["name"])
        return len(rows), len(rows2)

    def promote_candidates(self):
        due = self.db.due_candidates(self.cfg.launch_candidate_recheck_seconds, self.cfg.launch_candidate_expire_hours, self.cfg.launch_max_market_checks_per_cycle)
        promoted = 0
        for c in due:
            self.db.mark_candidate_checked(c["token"])
            m = self.market.token_snapshot(c["token"])
            if not m or not m.pair_address or not m.pair_created_at_ms: continue
            if m.liquidity_usd is None or m.liquidity_usd < self.cfg.launch_min_liquidity_usd: continue
            self.db.add_launch(c, m, c["source"] or "candidate")
            promoted += 1
            log.info("NEW LAUNCH %s %s | liq=$%.0f | pair_age=%.2fh", m.symbol, c["token"], m.liquidity_usd or 0, m.pair_age_hours or 0)
        return promoted

    def snapshots(self):
        due = self.db.launches_due_snapshot()
        done = 0
        market_cache = {}
        for launch, horizon, target in due:
            token = launch["token"]
            if token not in market_cache: market_cache[token] = self.market.token_snapshot(token)
            m = market_cache[token]
            if not m: continue
            self.db.save_launch_snapshot(token, horizon, target, m)
            done += 1
        return done

    def cycle(self):
        a,b = self.discover(); p = self.promote_candidates(); s = self.snapshots()
        log.info("cycle contracts=%s transfer_tokens=%s promoted=%s snapshots=%s", a,b,p,s)

    def run(self):
        while True:
            try: self.cycle()
            except KeyboardInterrupt: return
            except Exception: log.exception("launch indexer cycle failed")
            time.sleep(self.cfg.launch_poll_seconds)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--once", action="store_true"); args = ap.parse_args()
    x = LaunchIndexer(); x.cycle() if args.once else x.run()
