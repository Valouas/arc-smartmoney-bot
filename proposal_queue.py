from __future__ import annotations
import json, time, uuid
from pathlib import Path

class ProposalQueue:
    """Human-approval queue only; no signer or broadcaster is included."""
    def __init__(self, path="data/proposals.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, signal):
        m = signal.risk.market if signal.risk else None
        proposal = {
            "id": uuid.uuid4().hex[:10],
            "created_at": int(time.time()),
            "status": "PENDING_HUMAN_REVIEW",
            "side": signal.side,
            "token": signal.token,
            "symbol": getattr(m, "symbol", "?") if m else "?",
            "reference_price_usd": getattr(m, "price_usd", None) if m else None,
            "signal_score": signal.score,
            "cluster_wallets": [e.wallet for e in signal.events],
            "total_observed_usdc": signal.total_usdc,
            "market_url": getattr(m, "url", None) if m else None,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(proposal) + "\n")
        return proposal
