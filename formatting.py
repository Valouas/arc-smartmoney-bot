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


def buy_opportunity_text(signal, proposal):
    market = signal.risk.market if signal.risk else None
    lines = [
        '🟢 Opportunité d’achat validée par le scanner',
        f"Token : {proposal['symbol']}",
        proposal['token'],
        f'Score : {signal.score}/100 | Identités indépendantes : {signal.independent_identities}',
    ]
    if market:
        price = f'${market.price_usd:.8g}' if market.price_usd is not None else '?'
        lines += [f'Prix de référence : {price}',
                  f'Liquidité : {money(market.liquidity_usd)} | Capitalisation : {money(market.market_cap_usd or market.fdv_usd)}']
        if market.url: lines.append(market.url)
    eligible = [e for e in signal.events if e.confirmation_eligible and e.trade_verified and e.sizing]
    # Keep a single concise Telegram notification; full metadata is persisted
    # in runtime_events/signals for every event, including those not listed here.
    for e in eligible[-3:]:
        sizing = e.sizing
        ratio = sizing.get('size_multiplier')
        share = sizing.get('capital_share')
        lines += [
            f'Wallet autorisé : {e.wallet}',
            f'Classe moteur : {e.intelligence_class or "?"} | signal VALIDATED',
            f'Buy: {money(sizing.get("buy_usdc"))} | Usual buy: {money(sizing.get("usual_buy_usdc"))}',
            f'Size: {ratio:.2f}x' if ratio is not None else 'Size: N/A (baseline insuffisante)',
            f'Capital share observed: {share:.1%}' if share is not None else 'Capital share observed: N/A',
            f'Sizing confidence: {sizing.get("sizing_confidence", "LOW")} | Conviction: {sizing.get("conviction_label") or "N/A"}',
            f'Identity cluster: {e.identity_id or "?"} | Cluster size: {e.identity_cluster_size or "?"}',
            f'Independent identities on token: {signal.independent_identities}',
        ]
    if len(eligible) > 3:
        lines.append(f'+ {len(eligible)-3} wallets autorisés : détails dans le rapport local.')
    lines += [f"Référence : {proposal['id']}", 'À examiner manuellement — aucun ordre réel envoyé.']
    return '\n'.join(lines)


def promoted_wallet_text(wallet, profile):
    return '\n'.join([
        '🧠 Nouveau wallet qualifié à suivre',
        wallet.lower(),
        f"Score : {profile['smart_score']}/100 | Classe : {profile['profile_class']}",
        f"Gagnants observés : {profile['winners']}/{profile['evaluable_trades']} trades évaluables",
        f"Déploiements achetés : {profile['launches_bought']} | Capture : {profile['opportunity_capture_rate']:.1%}",
        'Critères de promotion validés. Suivi activé ; aucune copie automatique.',
    ])
