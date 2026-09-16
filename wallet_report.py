from __future__ import annotations
import json
from pathlib import Path

def pct(x):
    return f"{100*float(x or 0):.1f}%"

def main():
    p = Path("data/wallet_intelligence.json")
    if not p.exists():
        print("No V3 intelligence file yet. Run: python wallet_profiler.py --once")
        return
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = list(data.get("profiles", {}).values())
    rows.sort(key=lambda x: (-x["smart_score"], x["wallet"]))
    print("=== ARC WALLET INTELLIGENCE V3 ===")
    for x in rows:
        flag = "❌" if not x["confirmation_eligible"] else "✅"
        print(
            f"{flag} {x['wallet']} | score={x['smart_score']:>3} "
            f"| {x['profile_class']:<11} | w={x['recommended_weight']} "
            f"| winners={x['winners']}/{x['evaluable_trades']} "
            f"| capture={pct(x['opportunity_capture_rate'])} "
            f"| launches={x['launches_bought']} | active_h={x['active_hours']} "
            f"| avg_early={pct(x['avg_precocity'])} | conviction={x['winner_conviction_ratio']:.2f}x"
        )

if __name__ == "__main__":
    main()
