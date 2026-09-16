from __future__ import annotations
import argparse, time, logging, json
from config import SETTINGS
from storage import Storage
from arcscan import ArcscanClient, ArcscanError
from market_data import DexScreenerClient
from launch_parser import extract_contract_candidates, extract_tokens_from_transfers, _addr

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("launch-indexer")

class LaunchIndexer:
    def __init__(self):
        self.cfg = SETTINGS
        self.db = Storage()
        self.arcscan = ArcscanClient(self.cfg.arcscan_api_url)
        self.market = DexScreenerClient(self.cfg.dexscreener_chain_id)

    def _discover_stream(self, stream):
        state = self.db.db.execute("SELECT * FROM discovery_cursors WHERE stream=?", (stream,)).fetchone()
        cursor = state["cursor"] if state else None
        stop = state["stop_key"] if state else None
        head = state["head_key"] if state else None
        seen = set(json.loads(state["seen_cursors"])) if state and cursor else set()
        if not cursor: head = None
        count = 0
        for _ in range(self.cfg.launch_discovery_pages):
            if stream == "contracts":
                data = self.arcscan.explore_contracts(limit=self.cfg.launch_contract_page_size,cursor=cursor,filter_name="token")
                if not isinstance(data,dict) or not isinstance(data.get("items"),list):
                    raise ArcscanError("unrecognized launch discovery page")
                for row in data["items"]:
                    if not isinstance(row,dict): raise ArcscanError("unrecognized deployment")
                    standard=row.get("standard") or (row.get("token") or {}).get("standard")
                    if str(standard).lower().replace("-","") not in {"erc20","erc721","erc1155"}:
                        raise ArcscanError("unknown deployment standard")
                parsed = extract_contract_candidates(data)
                # Cursor boundaries include NFTs too: a page without ERC-20s
                # must not look empty or lose the previous scan's boundary.
                keys = [_addr(r.get("address")) for r in data.get("items",[]) if isinstance(r,dict)]
                if any(not key for key in keys) or any(
                    not r["is_erc20"] or r["deployment_block"] is None or r["deployed_at"] is None
                    for r in parsed
                ):
                    raise ArcscanError("incomplete deployment metadata")
            else:
                data = self.arcscan.explore_token_transfers(limit=self.cfg.launch_transfer_page_size,cursor=cursor,standard="erc20")
                if not isinstance(data,dict) or not isinstance(data.get("items"),list):
                    raise ArcscanError("unrecognized launch discovery page")
                parsed = extract_tokens_from_transfers(data)
                keys = [f"{r['tx_hash']}:{r['log_index']}" for r in data.get("items",[]) if "tx_hash" in r and "log_index" in r]
            if not isinstance(data.get("items"),list) or len(keys)!=len(data["items"]):
                raise ArcscanError("unrecognized launch discovery page")
            page = data.get("page") or {}
            if not isinstance(page,dict): raise ArcscanError("unrecognized launch pagination")
            nxt = self.arcscan.next_cursor(data)
            if type(page.get("has_more")) is not bool or bool(nxt)!=page["has_more"]:
                raise ArcscanError("launch pagination incomplete")
            if data.get("capped") or data.get("truncated"):
                raise ArcscanError("launch index incomplete")
            # Only contracts certify the deployment universe. Transfer rankings
            # describe a supplemental window and can legitimately be incomplete.
            if stream == "contracts":
                ranking = data.get("ranking")
                if not isinstance(ranking, dict) or ranking.get("complete") is not True:
                    raise ArcscanError("launch index incomplete")
            if nxt and nxt in seen: raise ArcscanError("launch cursor cycle")
            if head is None and keys: head = keys[0]
            done = not nxt or (stop is not None and stop in keys)
            if stream=="transfers" and stop is None and data["items"]:
                timestamps=[r.get("timestamp") for r in data["items"]]
                if all(isinstance(t,int) for t in timestamps) and min(timestamps)<int(time.time())-int(self.cfg.max_pair_age_hours*3600):
                    done=True  # Supplemental initial window; contracts are the deployment universe.
            if nxt: seen.add(nxt)
            with self.db.transaction():
                for x in parsed:
                    self.db.upsert_candidate(x["address"], "arcscan_"+stream,x["symbol"],x["name"],x["deployer"],x["deployment_block"],x["deployed_at"])
                self.db.db.execute("""INSERT INTO discovery_cursors(stream,cursor,stop_key,head_key,seen_cursors,completed_at,error)
                    VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(stream) DO UPDATE SET cursor=excluded.cursor,
                    stop_key=excluded.stop_key,head_key=excluded.head_key,seen_cursors=excluded.seen_cursors,
                    completed_at=COALESCE(excluded.completed_at,discovery_cursors.completed_at),error=NULL""",
                    (stream,None if done else nxt,head if done else stop,None if done else head,
                     "[]" if done else json.dumps(sorted(seen)),int(time.time()) if done else None))
            count += len(parsed)
            if done: break
            cursor = nxt
        return count

    def discover(self):
        counts = []
        for stream in ("contracts","transfers"):
            try: counts.append(self._discover_stream(stream))
            except (ArcscanError,KeyError,TypeError,ValueError):
                self.db.db.execute("INSERT INTO discovery_cursors(stream,error) VALUES(?,?) ON CONFLICT(stream) DO UPDATE SET error=excluded.error",
                                   (stream,"incomplete_page_or_transport"))
                self.db.commit()
                log.warning("%s discovery incomplete; saved cursor retained",stream)
                counts.append(0)
        completed = self.db.db.execute("SELECT COUNT(*) FROM discovery_cursors WHERE stream='contracts' AND completed_at IS NOT NULL AND error IS NULL").fetchone()[0]
        if completed == 1:
            self.db.db.execute("INSERT OR IGNORE INTO observation_epochs VALUES('launch_universe',?)", (int(time.time()),))
            self.db.commit()
        return tuple(counts)

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
            if not m or not m.price_usd or m.price_usd <= 0: continue
            if m.pair_address != launch["pair_address"]: continue
            self.db.save_launch_snapshot(token, horizon, target, m)
            done += 1
        return done

    def cycle(self):
        try: a,b = self.discover()
        except ArcscanError:
            log.warning("discovery unavailable; continuing existing launch observations")
            a,b = 0,0
        p = self.promote_candidates(); s = self.snapshots()
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
