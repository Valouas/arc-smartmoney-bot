def money(x):
    if x is None:
        return "?"
    if abs(x) >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:.2f}"

def signal_text(s):
    risk = s.risk
    m = risk.market if risk else None
    icon = "🔥" if s.status == "VALIDATED" else ("⛔" if s.status == "BLOCKED" else "👀")
    labels = []
    for e in s.events:
        eligibility = "" if e.confirmation_eligible else " IGNORED"
        labels.append(f"{e.wallet_label}[{e.intelligence_class or '?'} {e.reputation_score if e.reputation_score is not None else '?'}{eligibility}]")
    lines = [
        f"{icon} ARC {s.side} {s.status} — score {s.score}/100",
        f"Token: {(m.symbol if m else '?')} {s.token}",
        f"Wallets observed: {s.distinct_wallets}",
        f"Independent eligible identities: {s.independent_identities}",
        f"Ignored scanner/low-quality wallets: {s.ignored_wallets}",
        f"Cluster weight: {s.cluster_weight} | flow {money(s.total_usdc)}",
        f"Seen: {', '.join(labels)}",
    ]
    if m:
        lines += [
            f"Price: {money(m.price_usd)} | MC/FDV: {money(m.market_cap_usd or m.fdv_usd)}",
            f"Liquidity: {money(m.liquidity_usd)} | 5m vol: {money(m.volume_m5)}",
            f"5m acceleration: {m.volume_acceleration:.2f}x" if m.volume_acceleration is not None else "5m acceleration: ?",
            f"Pair age: {m.pair_age_hours:.2f}h" if m.pair_age_hours is not None else "Pair age: ?",
        ]
    if risk:
        lines += [
            f"Top10: {risk.top10_concentration_pct:.1f}%" if risk.top10_concentration_pct is not None else "Top10: unknown",
            f"Fails: {', '.join(risk.hard_fail_reasons) or 'none'}",
            f"Warnings: {', '.join(risk.warnings) or 'none'}",
        ]
    if m and m.url:
        lines.append(m.url)
    return "\n".join(lines)
