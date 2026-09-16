from __future__ import annotations
"""Historical research utility for Arc winner wallets. Read only."""
import json, time, argparse
from collections import defaultdict
from pathlib import Path
from web3 import Web3
from config import SETTINGS
from rpc import ArcRPC, USDC_ERC20, TRANSFER_TOPIC
from decoder import classify_trade
from arcscan import ArcscanClient
from reputation import ReputationBook
from identity_graph import IdentityGraph

ZERO = "0x0000000000000000000000000000000000000000"

def load_winners(path="known_winners.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))["tokens"]

def topic_addr(t):
    h = t.hex() if hasattr(t, "hex") else str(t)
    h = h[2:] if h.startswith("0x") else h
    return Web3.to_checksum_address("0x" + h[-40:])

class HistoryLab:
    def __init__(self):
        self.rpc = ArcRPC(SETTINGS.rpc_url, SETTINGS.chain_id)
        self.arcscan = ArcscanClient(SETTINGS.arcscan_api_url)
        self.rep = ReputationBook()
        self.identity = IdentityGraph(self.arcscan)

    def _creation_block(self, token: str) -> int | None:
        data = self.arcscan.address_overview(token); found = []
        def walk(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    p = f"{path}.{k}".lower()
                    if "creation" in p and "block" in p:
                        try: found.append(int(v))
                        except Exception: pass
                    walk(v, p)
            elif isinstance(obj, list):
                for v in obj: walk(v, path)
        walk(data)
        return min(found) if found else None

    def earliest_transfer_logs(self, token: str, max_logs=400, block_chunk=20_000):
        token = Web3.to_checksum_address(token)
        start = self._creation_block(token)
        if start is None: return [], "creation_block_unknown"
        head = self.rpc.block_number(); logs = []; frm = start
        while frm <= head and len(logs) < max_logs:
            to = min(head, frm + block_chunk - 1)
            try:
                logs.extend(self.rpc.w3.eth.get_logs({"fromBlock":frm,"toBlock":to,"address":token,"topics":[TRANSFER_TOPIC]}))
            except Exception:
                if block_chunk > 1000:
                    block_chunk = max(1000, block_chunk // 4); continue
                return [], "log_range_unavailable"
            frm = to + 1
        logs.sort(key=lambda x: (int(x["blockNumber"]), int(x.get("logIndex", 0))))
        return logs[:max_logs], None

    def analyze_token(self, symbol: str, token: str, max_candidates=150):
        logs, err = self.earliest_transfer_logs(token)
        if err: return {"symbol":symbol,"token":token,"error":err,"buyers":[]}
        candidates=[]; seen_tx_wallet=set(); rank=0
        for log in logs:
            topics=log["topics"]
            if len(topics)<3: continue
            to=topic_addr(topics[2])
            if to.lower()==ZERO: continue
            key=(log["transactionHash"].hex(),to.lower())
            if key in seen_tx_wallet: continue
            seen_tx_wallet.add(key)
            try:
                receipt=self.rpc.get_receipt(log["transactionHash"])
                tx=self.rpc.w3.eth.get_transaction(log["transactionHash"])
                detected=classify_trade(self.rpc,receipt,to,SETTINGS.min_usdc_flow,
                    native_value_wei=int(tx.get("value",0) or 0) if str(tx.get("from","")).lower()==to.lower() else 0)
            except Exception: return {"symbol":symbol,"token":token,"error":"receipt_incomplete","buyers":[]}
            buys=[e for e in detected if e["token"].lower()==token.lower() and e["side"]=="BUY" and e["confirmation_eligible"]]
            if buys:
                observed_spend=buys[0]["usdc_amount"]
                rank+=1
                candidates.append({"rank":rank,"wallet":to.lower(),"tx_hash":log["transactionHash"].hex(),"block":int(log["blockNumber"]),"usdc_spent":observed_spend})
                if len(candidates)>=max_candidates: break
        return {"symbol":symbol,"token":token,"buyers":candidates}

    def run(self, max_candidates=100):
        self.rpc.sanity_check()
        results=[]; overlap=defaultdict(list)
        for item in load_winners():
            print(f"[history] {item['symbol']} ...", flush=True)
            r=self.analyze_token(item["symbol"],item["address"],max_candidates=max_candidates); results.append(r)
            for b in r.get("buyers",[]):
                overlap[b["wallet"]].append({"symbol":item["symbol"],"rank":b["rank"]})
        addresses=list(overlap)
        if addresses: self.identity.refresh(addresses)
        ranked=[]
        for wallet,hits in overlap.items():
            rep=self.rep.get(wallet)
            ranked.append({"wallet":wallet,"winner_count":len({h['symbol'] for h in hits}),"hits":hits,"reputation_score":rep.score,"dynamic_weight":rep.dynamic_weight,"funded_by":getattr(self.identity.facts.get(wallet),"funded_by",None),"identity_cluster":self.identity.identity_id(wallet)})
        ranked.sort(key=lambda x:(-x["winner_count"],-x["reputation_score"],min(h["rank"] for h in x["hits"])))
        out={"generated_at":int(time.time()),"tokens":results,"ranked_wallets":ranked,"identity_clusters":self.identity.cluster_map()}
        Path("data/history_report.json").write_text(json.dumps(out,indent=2),encoding="utf-8")
        return out

if __name__ == "__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--max-candidates",type=int,default=100); args=ap.parse_args()
    HistoryLab().run(max_candidates=args.max_candidates)
