from __future__ import annotations
import math, statistics

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def beta_rate(successes: int, trials: int, alpha=1.5, beta=3.0) -> float:
    return (successes + alpha) / (trials + alpha + beta)

def precocity_value(age_seconds):
    if age_seconds is None: return 0.25
    m = age_seconds / 60
    if m <= 2: return 1.0
    if m <= 5: return 0.9
    if m <= 15: return 0.75
    if m <= 60: return 0.5
    if m <= 360: return 0.25
    return 0.05

def recommended_weight(score):
    if score >= 82: return 4
    if score >= 68: return 3
    if score >= 52: return 2
    if score >= 35: return 1
    return 0

def classify_profile(metrics, cfg):
    n = metrics["launches_bought"]
    capture = metrics["opportunity_capture_rate"]
    rate = metrics["launch_buys_per_active_hour"]
    if n >= cfg.scanner_min_launch_buys and (capture >= cfg.scanner_capture_rate or rate >= cfg.scanner_buys_per_active_hour):
        return "SCANNER_BOT"
    if metrics["evaluable_trades"] < cfg.profile_min_evaluable_trades: return "WATCH"
    s = metrics["smart_score"]
    if s >= 82: return "ELITE"
    if s >= 68: return "SMART"
    if s >= 50: return "SELECTIVE"
    if s < 30 and n >= 10: return "IGNORE"
    return "WATCH"

def build_score(metrics, cfg):
    n = metrics["evaluable_trades"]
    winners = metrics["winners"]
    hit = beta_rate(winners, metrics.get("outcome_trials", n))
    capture = metrics["opportunity_capture_rate"]
    selectivity = 1.0 - clamp(capture)
    selectivity_quality = 0.55 * hit + 0.45 * selectivity
    precocity = clamp(metrics["avg_precocity"])
    perf = clamp(metrics["avg_performance_score"])
    conviction = clamp(metrics["conviction_quality"])
    exits = clamp(metrics["exit_quality"])
    history = clamp(math.log1p(n) / math.log(21))
    raw = 100 * (0.30 * selectivity_quality + 0.20 * precocity + 0.15 * perf + 0.15 * conviction + 0.10 * exits + 0.10 * history)
    confidence = clamp(math.sqrt(n / max(1, cfg.profile_min_evaluable_trades * 2)))
    score = 40 * (1-confidence) + raw * confidence
    if metrics["launches_bought"] >= cfg.scanner_min_launch_buys:
        if capture >= cfg.scanner_capture_rate:
            score -= min(40, 20 + 40*(capture-cfg.scanner_capture_rate))
        if metrics["launch_buys_per_active_hour"] >= cfg.scanner_buys_per_active_hour:
            score -= 15
    return int(round(clamp(score/100)*100))

def profile_from_rows(wallet, entries, opportunity_count, active_hours, cfg):
    launches_bought = len(entries)
    evaluable = winners = missing = pending = 0
    precocities=[]; perf_scores=[]; stakes=[]; winner_stakes=[]; exits=[]
    for e in entries:
        stake = float(e.get("usdc") or 0)
        if stake > 0: stakes.append(stake)
        precocities.append(precocity_value(e.get("age_seconds")))
        entry = e.get("entry_price"); future = e.get("outcome_price",e.get("max_future_price"))
        missing += int(e.get("outcome_state") == "MISSING")
        pending += int(e.get("outcome_state") == "PENDING")
        if entry and future is not None and math.isfinite(future) and future >= 0 and entry > 0:
            evaluable += 1
            ret = (future/entry - 1) * 100
            is_winner = ret >= cfg.winner_return_pct
            winners += int(is_winner)
            if is_winner and stake > 0: winner_stakes.append(stake)
            perf_scores.append(clamp((ret + 50) / 350))
        exit_price = e.get("exit_price")
        if entry and exit_price and entry > 0:
            exits.append(clamp(((exit_price/entry - 1)*100 + 50)/250))
    median_stake = statistics.median(stakes) if stakes else 0
    winner_median = statistics.median(winner_stakes) if winner_stakes else 0
    ratio = winner_median / median_stake if median_stake > 0 else 0
    conviction_quality = clamp(ratio / 2.0) if winners else 0.25
    capture = launches_bought / max(1, opportunity_count)
    launch_rate = launches_bought / max(1, active_hours)
    metrics = {
        "wallet": wallet.lower(), "launches_bought": launches_bought,
        "evaluable_trades": evaluable, "winners": winners,
        "adjusted_hit_rate": beta_rate(winners, evaluable + missing),
        "outcome_trials": evaluable + missing, "missing_outcomes": missing, "pending_outcomes": pending,
        "opportunity_count": opportunity_count, "opportunity_capture_rate": clamp(capture),
        "active_hours": active_hours, "launch_buys_per_active_hour": launch_rate,
        "avg_precocity": sum(precocities)/len(precocities) if precocities else 0,
        "avg_performance_score": sum(perf_scores)/len(perf_scores) if perf_scores else 0.25,
        "median_launch_buy_usdc": median_stake, "winner_median_buy_usdc": winner_median,
        "winner_conviction_ratio": ratio, "conviction_quality": conviction_quality,
        "exit_quality": sum(exits)/len(exits) if exits else 0.4,
    }
    metrics["smart_score"] = build_score(metrics, cfg)
    metrics["profile_class"] = classify_profile(metrics, cfg)
    metrics["recommended_weight"] = recommended_weight(metrics["smart_score"])
    metrics["confirmation_eligible"] = metrics["profile_class"] not in {"SCANNER_BOT","IGNORE"}
    return metrics
