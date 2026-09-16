from types import SimpleNamespace
from wallet_metrics import profile_from_rows

CFG = SimpleNamespace(winner_return_pct=100,scanner_capture_rate=0.35,scanner_min_launch_buys=15,scanner_buys_per_active_hour=12,profile_min_evaluable_trades=5)

def make_entries(n, winners, stake=100, early=120):
    out=[]
    for i in range(n):
        win = i < winners
        out.append({"usdc":stake*(3 if win else 1),"age_seconds":early,"entry_price":1.0,"max_future_price":3.0 if win else 0.8,"exit_price":2.0 if win else 0.7})
    return out

def test_selective_wallet_beats_spray_wallet():
    selective = profile_from_rows("0x1", make_entries(10,5), opportunity_count=100, active_hours=10, cfg=CFG)
    spray = profile_from_rows("0x2", make_entries(40,5), opportunity_count=50, active_hours=10, cfg=CFG)
    assert selective["smart_score"] > spray["smart_score"]
    assert spray["profile_class"] == "SCANNER_BOT"
    assert spray["confirmation_eligible"] is False

def test_small_sample_not_elite():
    p = profile_from_rows("0x3", make_entries(1,1), opportunity_count=20, active_hours=2, cfg=CFG)
    assert p["profile_class"] == "WATCH"
    assert p["smart_score"] < 82

def test_high_conviction_winners_help():
    p = profile_from_rows("0x4", make_entries(10,4,stake=100), 100, 10, CFG)
    assert p["winner_conviction_ratio"] >= 2.5

if __name__ == "__main__":
    test_selective_wallet_beats_spray_wallet(); test_small_sample_not_elite(); test_high_conviction_winners_help(); print("ok")
