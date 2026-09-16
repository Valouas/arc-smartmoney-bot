from __future__ import annotations
import argparse, json, logging, time
from pathlib import Path

from config import SETTINGS
from storage import Storage
from arcscan import ArcscanClient, ArcscanError, require_complete_history
from rpc import ArcRPC
from decoder import classify_trade
from models import TradeEvent
from discovery_parser import extract_transfer_rows, extract_tx_hashes
from wallet_metrics import profile_from_rows
from identity_graph import IdentityGraph
from atomic_json import write_json
from forward_metrics import build_forward_profile
from wallet_policy import is_dex_infrastructure, require_smart_money_wallet

logging.basicConfig(level=getattr(logging, SETTINGS.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("auto-discovery")

class AutoDiscovery:
    def __init__(self):
        self.cfg=SETTINGS
        self.db=Storage()
        self.arc=ArcscanClient(self.cfg.arcscan_api_url)
        self.rpc=ArcRPC(self.cfg.rpc_url,self.cfg.chain_id)
        self.identities=IdentityGraph(self.arc)

    def detect_winners(self):
        rows=self.db.db.execute(
            """SELECT l.token,l.first_price_usd,MAX(s.price_usd) max_price
               FROM launches l JOIN launch_snapshots s ON lower(s.token)=lower(l.token)
               WHERE l.first_price_usd IS NOT NULL AND l.first_price_usd>0 AND s.price_usd IS NOT NULL
               GROUP BY l.token,l.first_price_usd"""
        ).fetchall()
        found=0; now=int(time.time())
        for r in rows:
            ret=(float(r["max_price"])/float(r["first_price_usd"])-1)*100
            if ret < self.cfg.winner_return_pct: continue
            self.db.db.execute("INSERT OR IGNORE INTO discovery_winners(token,detected_at,max_return_pct) VALUES(?,?,?)", (r["token"].lower(),now,ret))
            found+=1
        self.db.db.commit(); return found

    def _all_transfer_rows(self, token):
        cursor=None; rows=[]; cursors=set()
        for _ in range(self.cfg.discovery_transfer_pages):
            data=self.arc.token_transfers(token,limit=self.cfg.discovery_page_size,cursor=cursor)
            require_complete_history(data)
            rows.extend(extract_transfer_rows(data,expected_token=token))
            nxt=self.arc.next_cursor(data)
            if not nxt: break
            if nxt in cursors: raise ArcscanError("pagination cursor cycle")
            cursors.add(nxt)
            cursor=nxt
        else:
            raise ArcscanError("history page limit reached")
        if any(r["block"] is None or r["log_index"] is None for r in rows):
            raise ArcscanError("early buyer order unavailable")
        rows.sort(key=lambda x: (x["block"], x["log_index"]))
        return rows

    def early_buyers(self, token):
        rows=self._all_transfer_rows(token); buyers=[]; seen=set()
        for row in rows:
            wallet=row["to"].lower()
            if is_dex_infrastructure(wallet,self.cfg): continue
            if wallet in seen or wallet=="0x0000000000000000000000000000000000000000": continue
            try:
                tx=self.rpc.w3.eth.get_transaction(row["tx_hash"])
                receipt=self.rpc.get_receipt(row["tx_hash"])
                detected=classify_trade(self.rpc,receipt,wallet,self.cfg.min_usdc_flow,native_value_wei=(int(tx.get("value",0) or 0) if str(tx.get("from","")).lower()==wallet else 0))
            except Exception as exc:
                raise ArcscanError("early buyer receipt incomplete") from exc
            matched=[x for x in detected if x["side"]=="BUY" and x["token"].lower()==token.lower() and x["confirmation_eligible"]]
            if not matched: continue
            seen.add(wallet); buyers.append((wallet,len(buyers)+1,row["tx_hash"]))
            if len(buyers)>=self.cfg.discovery_early_buyers: break
        return buyers

    def discover_from_winners(self):
        winners=self.db.db.execute("SELECT * FROM discovery_winners WHERE processed_at IS NULL ORDER BY detected_at").fetchall()
        total=0
        for w in winners:
            token=w["token"]
            try: buyers=self.early_buyers(token)
            except ArcscanError:
                log.warning("winner history incomplete: %s", token)
                continue
            for wallet,rank,txh in buyers:
                if not is_dex_infrastructure(wallet,self.cfg):
                    if self.db.upsert_discovery_candidate(wallet,token,rank): total+=1
            self.db.db.execute("UPDATE discovery_winners SET processed_at=? WHERE token=?", (int(time.time()),token)); self.db.db.commit()
            log.info("winner %s -> %s verified early buyers",token,len(buyers))
        return total

    def activity_hashes(self,wallet):
        cursor=None; hashes=[]; seen=set(); cursors=set()
        for _ in range(self.cfg.discovery_activity_pages):
            data=self.arc.address_activity(wallet,limit=self.cfg.discovery_page_size,cursor=cursor)
            require_complete_history(data)
            for h in extract_tx_hashes(data):
                if h not in seen: seen.add(h); hashes.append(h)
            nxt=self.arc.next_cursor(data)
            if not nxt: break
            if nxt in cursors: raise ArcscanError("pagination cursor cycle")
            cursors.add(nxt)
            cursor=nxt
        else:
            raise ArcscanError("history page limit reached")
        return hashes

    def hydrate_wallet(self,wallet):
        if is_dex_infrastructure(wallet,self.cfg): return 0
        self.db.db.execute("UPDATE discovery_candidates SET history_complete=0,hydrated_at=NULL WHERE wallet=?", (wallet.lower(),))
        self.db.db.commit()
        hashes=self.activity_hashes(wallet); inserted=0
        for h in hashes:
            try:
                tx=self.rpc.w3.eth.get_transaction(h)
                receipt=self.rpc.get_receipt(h)
                if int(receipt.get("status",1))!=1: continue
                block=self.rpc.get_block(int(tx["blockNumber"]),full_transactions=False)
                ts=int(block["timestamp"])
                detected=classify_trade(self.rpc,receipt,wallet,self.cfg.min_usdc_flow,native_value_wei=int(tx.get("value",0) or 0) if str(tx.get("from","")).lower()==wallet.lower() else 0,strict=True)
            except Exception as exc:
                raise ArcscanError("wallet reconstruction incomplete") from exc
            for d in detected:
                if not d["confirmation_eligible"]:
                    raise ArcscanError("ambiguous trade requires trace/DEX validation")
                e=TradeEvent(tx_hash=h,block_number=int(tx["blockNumber"]),timestamp=ts,wallet=wallet,wallet_label="V4 candidate",wallet_weight=0,side=d["side"],token=d["token"],token_symbol=d["symbol"],token_amount=d["token_amount"],usdc_amount=d["usdc_amount"],confirmation_eligible=False)
                self.db.add_research_trade(e); inserted+=1
        self.db.db.execute("UPDATE discovery_candidates SET hydrated_at=?,history_complete=1 WHERE wallet=?", (int(time.time()),wallet.lower())); self.db.db.commit()
        return inserted

    def hydrate_candidates(self):
        rows=self.db.db.execute("SELECT * FROM discovery_candidates WHERE status='QUARANTINE' AND (hydrated_at IS NULL OR history_complete=0) ORDER BY source_winners DESC,best_early_rank ASC").fetchall()
        rows=[r for r in rows if not is_dex_infrastructure(r['wallet'],self.cfg)][:self.cfg.discovery_max_wallets_per_cycle]
        n=0
        for r in rows:
            try: count=self.hydrate_wallet(r["wallet"])
            except ArcscanError:
                log.warning("candidate remains incomplete: %s", r["wallet"])
                continue
            n+=1
            log.info("hydrated %s with %s research trades",r["wallet"],count)
        return n

    def _active_hours(self,wallet):
        rows=self.db.db.execute("SELECT timestamp FROM research_trades WHERE wallet=? UNION SELECT timestamp FROM events WHERE lower(wallet)=?", (wallet.lower(),wallet.lower())).fetchall()
        return {int(r["timestamp"])//3600 for r in rows if r["timestamp"]}

    def _opportunity_count(self,active):
        if not active:return 0
        rows=self.db.db.execute("SELECT pair_created_at FROM launches").fetchall()
        return sum(1 for r in rows if int(r["pair_created_at"])//3600 in active)

    def _candidate_entries(self,wallet):
        rows=self.db.db.execute(
            """SELECT tx_hash,token,timestamp,usdc_amount,token_amount,execution_price_usd FROM research_trades WHERE wallet=? AND side='BUY' AND trade_verified=1
               UNION
               SELECT e.tx_hash,e.token,e.timestamp,e.usdc_amount,e.token_amount,CASE WHEN e.token_amount>0 THEN e.usdc_amount/e.token_amount ELSE NULL END
               FROM events e WHERE lower(e.wallet)=? AND e.side='BUY' AND e.trade_verified=1 ORDER BY timestamp""", (wallet.lower(),wallet.lower())
        ).fetchall()
        launches={r["token"].lower():dict(r) for r in self.db.db.execute("SELECT * FROM launches").fetchall()}
        by_token={}; seen=set()
        for r in rows:
            token=r["token"].lower()
            key=(r["tx_hash"].lower().removeprefix("0x"), token)
            if key in seen: continue
            seen.add(key)
            if token not in launches: continue
            x=by_token.get(token)
            if x is None:
                x={"token":token,"first_ts":int(r["timestamp"]),"usdc":0.0,"entry_price":r["execution_price_usd"]}; by_token[token]=x
            x["usdc"]+=float(r["usdc_amount"] or 0)
            if not x["entry_price"] and r["execution_price_usd"]: x["entry_price"]=r["execution_price_usd"]
        out=[]
        for token,x in by_token.items():
            launch=launches[token]
            future=self.db.db.execute("SELECT price_usd,observed_at FROM launch_snapshots WHERE token=? AND observed_at>? AND price_usd IS NOT NULL ORDER BY observed_at", (token,x["first_ts"])).fetchall()
            future_prices=[float(s["price_usd"]) for s in future]
            sell=self.db.db.execute("SELECT execution_price_usd FROM research_trades WHERE wallet=? AND token=? AND side='SELL' AND trade_verified=1 AND timestamp>=? AND execution_price_usd IS NOT NULL ORDER BY timestamp LIMIT 1", (wallet.lower(),token,x["first_ts"])).fetchone()
            out.append({"token":token,"usdc":x["usdc"],"age_seconds":max(0,x["first_ts"]-int(launch["pair_created_at"])),"entry_price":float(x["entry_price"]) if x["entry_price"] else None,"max_future_price":max(future_prices) if future_prices else None,"exit_price":float(sell["execution_price_usd"]) if sell else None})
        return out

    def profile_candidate(self,row):
        require_smart_money_wallet(row['wallet'],self.cfg)
        p=build_forward_profile(self.db,row["wallet"],self.cfg)
        p["source_winners"]=int(row["source_winners"])
        p["best_early_rank"]=row["best_early_rank"]
        p["independent_winners"]=self._independent_winners(p.get("winner_tokens",[]))
        return p

    def _independent_winners(self,tokens):
        deployers=[]
        for token in tokens:
            row=self.db.db.execute("SELECT deployer FROM launch_candidates WHERE token=?",(token.lower(),)).fetchone()
            if not row or not row[0]: return 0
            deployers.append(row[0].lower())
        graph=IdentityGraph(self.arc)
        graph.refresh(list(set(deployers)))
        # Different token addresses from the same creator/funding component
        # supply only one confirmation. Unknown/shared service funding is unknown.
        return len({graph.identity_id(w) for w in deployers if graph.eligible(w)})

    def _funding_collision(self,wallet):
        promoted=[r["wallet"] for r in self.db.db.execute("SELECT wallet FROM promoted_wallets WHERE enabled=1").fetchall()
                  if not is_dex_infrastructure(r['wallet'],self.cfg)]
        self.identities=IdentityGraph(self.arc); self.identities.refresh(promoted+[wallet])
        return not self.identities.eligible(wallet) or any(self.identities.related(wallet,p) for p in promoted)

    def promote_candidates(self):
        cutoff=int(time.time())-self.cfg.discovery_quarantine_hours*3600
        rows=self.db.db.execute("SELECT * FROM discovery_candidates WHERE status='QUARANTINE' AND hydrated_at IS NOT NULL AND history_complete=1 AND first_discovered_at<=? ORDER BY source_winners DESC,best_early_rank ASC", (cutoff,)).fetchall()
        current=sum(not is_dex_infrastructure(r['wallet'],self.cfg)
                    for r in self.db.db.execute("SELECT wallet FROM promoted_wallets WHERE enabled=1")); promoted=0
        for row in rows:
            if is_dex_infrastructure(row['wallet'],self.cfg): continue
            if current+promoted>=self.cfg.promotion_max_wallets: break
            p=self.profile_candidate(row); self.db.save_wallet_profile(p)
            self.db.db.execute("UPDATE discovery_candidates SET profile_score=?,profile_class=? WHERE wallet=?", (p["smart_score"],p["profile_class"],row["wallet"])); self.db.db.commit()
            reasons=[]
            if row["source_winners"] < self.cfg.promotion_min_winners: reasons.append("not_enough_independent_winners")
            if p["launches_bought"] < self.cfg.promotion_min_launch_buys: reasons.append("not_enough_launch_history")
            if p["smart_score"] < self.cfg.promotion_min_score: reasons.append("score_too_low")
            if p["opportunity_capture_rate"] > self.cfg.promotion_max_capture_rate: reasons.append("capture_rate_too_high")
            if p["profile_class"] in {"SCANNER_BOT","IGNORE"}: reasons.append("bad_profile_class")
            if not p["confirmation_eligible"]: reasons.append("not_confirmation_eligible")
            if p.get("profile_version") != 2 or not p.get("data_quality_ok"): reasons.append("forward_evidence_incomplete")
            if p.get("independent_winners",0) < self.cfg.promotion_min_winners: reasons.append("related_or_unknown_winner_deployers")
            if p["evaluable_trades"] < self.cfg.profile_min_evaluable_trades: reasons.append("insufficient_evaluable_history")
            if p["winners"] < self.cfg.promotion_min_winners: reasons.append("insufficient_verified_winners")
            if self._funding_collision(row["wallet"]): reasons.append("funding_unknown_or_related")
            if reasons:
                self.db.db.execute("UPDATE discovery_candidates SET promotion_reason=? WHERE wallet=?", (",".join(reasons),row["wallet"])); self.db.db.commit(); continue
            weight=max(1,min(4,int(p["recommended_weight"]))); label=f"auto {p['profile_class']} {p['smart_score']}"
            if not self.db.save_promoted_wallet(row["wallet"],p["smart_score"],p["profile_class"],weight,label): continue
            promoted+=1
            log.info("PROMOTED %s score=%s class=%s",row["wallet"],p["smart_score"],p["profile_class"])
        self.write_generated_watchlist(); return promoted

    def write_generated_watchlist(self):
        rows=self.db.db.execute("SELECT * FROM promoted_wallets WHERE enabled=1 ORDER BY smart_score DESC,promoted_at ASC").fetchall()
        rows=[r for r in rows if not is_dex_infrastructure(r['wallet'],self.cfg)]
        payload={"wallets":[{"address":r["wallet"],"label":r["label"],"weight":int(r["weight"]),"group":"auto_discovered","smart_score":int(r["smart_score"]),"profile_class":r["profile_class"]} for r in rows]}
        write_json("generated_watchlist.json", payload)

    def cycle(self):
        w=self.detect_winners(); c=self.discover_from_winners(); h=self.hydrate_candidates(); p=self.promote_candidates()
        log.info("cycle winners=%s sources=%s hydrated=%s promoted=%s",w,c,h,p)

    def run(self):
        self.rpc.sanity_check()
        while True:
            try:self.cycle()
            except KeyboardInterrupt:return
            except Exception:log.exception("auto-discovery cycle failed")
            time.sleep(self.cfg.discovery_poll_seconds)

if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--once",action="store_true"); args=ap.parse_args()
    d=AutoDiscovery()
    if args.once:
        d.rpc.sanity_check()
        d.cycle()
    else: d.run()
