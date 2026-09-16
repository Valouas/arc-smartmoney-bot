from __future__ import annotations
from models import ClusterSignal, RiskReport
from config import Settings

def score_buy_cluster(cluster: ClusterSignal, risk: RiskReport, cfg: Settings) -> int:
    score = 0
    score += min(32, cluster.cluster_weight * 6)
    score += min(18, cluster.independent_identities * 6)

    reps = [e.reputation_score for e in cluster.events if e.confirmation_eligible and e.reputation_score is not None]
    if reps:
        avg = sum(reps) / len(reps)
        if avg >= 80: score += 12
        elif avg >= 65: score += 8
        elif avg >= 50: score += 4

    m = risk.market
    if m:
        if m.liquidity_usd is not None and m.liquidity_usd >= cfg.min_liquidity_usd:
            score += 10
        mc = m.market_cap_usd or m.fdv_usd
        if mc is not None and cfg.min_market_cap_usd <= mc <= cfg.max_market_cap_usd:
            score += 10
        if m.pair_age_hours is not None and m.pair_age_hours <= cfg.max_pair_age_hours:
            score += 5
        accel = m.volume_acceleration
        if accel is not None:
            if accel >= 3: score += 15
            elif accel >= 1.5: score += 8

    if risk.top10_concentration_pct is not None:
        if risk.top10_concentration_pct <= cfg.max_top10_pct:
            score += 10
        elif risk.top10_concentration_pct > 60:
            score -= 20

    sybil_excess = cluster.distinct_wallets - cluster.independent_identities - cluster.ignored_wallets
    score -= min(30, max(0, sybil_excess) * 10)
    score -= min(15, cluster.ignored_wallets * 5)

    if risk.hard_fail_reasons:
        score = min(score, 49)
    return max(0, min(100, score))

def classify_status(cluster: ClusterSignal, risk: RiskReport, score: int, cfg: Settings) -> str:
    if risk.hard_fail_reasons:
        return "BLOCKED"
    if cluster.independent_identities >= cfg.min_distinct_wallets and cluster.cluster_weight >= cfg.min_cluster_weight and score >= 70:
        return "VALIDATED"
    return "WATCH"
