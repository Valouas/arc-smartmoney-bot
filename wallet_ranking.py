"""Read-only ranking of persisted wallet evidence; never grants signal authority."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import textwrap
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from config import SETTINGS
from wallet_policy import InfrastructureRegistryError, infrastructure_addresses
from capital_allocation import allocation_metrics, forward_buys, sizing_policy
from ranking_clusters import attach_clusters, cluster_ranking, cluster_summary, select_view


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def optional_json(path):
    path = Path(path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Invalid report input")
    return data


def profile_payload(row):
    try:
        payload = json.loads(row.get("payload", "{}"))
        return payload if isinstance(payload, dict) else {}
    except (TypeError, ValueError):
        return {}


def rank_wallet(wallet, candidate, stored, promoted, research, observed, epoch,
                generated, history, identity, excluded, cfg, now):
    """Describe stored evidence. No profiling, identity refresh or promotion calls."""
    p = profile_payload(stored)
    forward = p.get("profile_version") == 2 and p.get("cohort") == "forward_after_discovery"
    first = candidate.get("first_discovered_at")
    start = max(epoch, first or 0) if epoch is not None else None
    # A profile from another observation epoch is not current forward evidence.
    forward = forward and start is not None and p.get("observation_start") == start
    metric = lambda key: number(p.get(key)) if forward else None
    buys, evaluable, winners = (metric(k) for k in ("launches_bought", "evaluable_trades", "winners"))
    score = number(stored.get("smart_score", candidate.get("profile_score")))
    current_class = stored.get("profile_class") or candidate.get("profile_class")
    capture = metric("opportunity_capture_rate") if (metric("opportunity_count") or 0) > 0 else None
    independent = metric("independent_winners")
    source_winners = candidate.get("source_winners", p.get("source_winners"))
    generated_at = number(p.get("generated_at"))
    fresh = generated_at is not None and 0 <= now-generated_at <= max(120, cfg.intelligence_reload_seconds*4)
    problems = []
    if not forward:
        problems.append("forward_profile_missing_or_wrong_cohort")
    elif not p.get("data_quality_ok"):
        problems.append("forward_evidence_incomplete")
    if forward and any(metric(k) is None for k in ("launches_bought", "evaluable_trades", "winners", "smart_score", "opportunity_capture_rate", "opportunity_count")):
        problems.append("profile_metrics_missing")
    if not fresh:
        problems.append("profile_stale_or_missing")
    if candidate and (not candidate.get("history_complete") or candidate.get("hydrated_at") is None):
        problems.append("research_history_incomplete")
    for key in ("unmatched_buys", "reconstruction_gaps", "missing_outcomes"):
        if (metric(key) or 0) > 0:
            problems.append(key)
    complete = not problems
    blockers = list(problems)
    if excluded:
        blockers.append("dex_infrastructure_excluded")
    if first is not None and now-first < cfg.discovery_quarantine_hours*3600:
        blockers.append("quarantine_not_elapsed")
    if source_winners is None or source_winners < cfg.promotion_min_winners:
        blockers.append("not_enough_winner_sources")
    # The exact numeric promotion gates are reused, not weakened for PROMISING.
    performance_checks = (
        (buys is not None and buys >= cfg.promotion_min_launch_buys, "not_enough_launch_history"),
        (metric("smart_score") is not None and metric("smart_score") >= cfg.promotion_min_score, "score_too_low_or_unknown"),
        (capture is not None and capture <= cfg.promotion_max_capture_rate, "capture_rate_too_high_or_unknown"),
        (evaluable is not None and evaluable >= cfg.profile_min_evaluable_trades, "insufficient_evaluable_history"),
        (winners is not None and winners >= cfg.promotion_min_winners, "insufficient_verified_winners"),
    )
    blockers.extend(reason for passed, reason in performance_checks if not passed)
    if independent is None:
        blockers.append("independent_sources_unknown")
    elif independent < cfg.promotion_min_winners:
        blockers.append("related_or_unknown_winner_deployers")
    if not forward or not p.get("confirmation_eligible"):
        blockers.append("not_confirmation_eligible")
    rejected = excluded or candidate.get("status") == "REJECTED" or (forward and current_class in {"SCANNER_BOT", "IGNORE"})
    if rejected and not excluded:
        blockers.append("rejected_status_or_profile_class")
    internally_promoted = bool(candidate.get("status") == "PROMOTED" and candidate.get("history_complete") and promoted.get("enabled"))
    validated = internally_promoted and complete and bool(p.get("confirmation_eligible"))
    classification = ("REJECTED" if rejected else "VALIDATED" if validated else
                      "PROMISING" if forward and all(passed for passed, _ in performance_checks) else "WATCH")
    last_reason = candidate.get("promotion_reason")
    # Funding facts are in memory in the engine. Persisted reasons/history are
    # explicitly labelled as past observations, never invented current facts.
    flags = [s for s in (last_reason or "").split(",") if s in {
        "funding_unknown_or_related", "related_or_unknown_winner_deployers"}]
    if not internally_promoted:
        blockers.extend(flags)
        blockers.append("engine_promotion_pending" if candidate else "profile_only_no_discovery_promotion")
        if not flags:
            blockers.append("funding_check_not_persisted")
    return {
        "wallet": wallet, "report_class": classification,
        "discovery_status": candidate.get("status", "PROFILE_ONLY"),
        "first_discovered_at": utc(first), "winner_sources": source_winners,
        "independent_sources": independent, "research_trades": research,
        "forward_buys_observed": observed if start is not None else None,
        "forward_launches_bought": buys, "forward_evaluable_trades": evaluable,
        "forward_winners": winners,
        "forward_pending_outcomes": metric("pending_outcomes"),
        "forward_missing_outcomes": metric("missing_outcomes"),
        "forward_hit_rate_pct": 100*winners/evaluable if evaluable and winners is not None else None,
        "forward_adjusted_hit_rate_pct": 100*metric("adjusted_hit_rate") if evaluable and metric("adjusted_hit_rate") is not None else None,
        "capture_pct": 100*capture if capture is not None else None,
        "score": score, "current_class": current_class, "profile_generated_at": utc(generated_at),
        "data_status": "COMPLETE" if complete else "INCOMPLETE",
        "data_reasons": ";".join(problems),
        "promotion_blockers": ";".join(dict.fromkeys(blockers)),
        "last_engine_promotion_reason": last_reason,
        "internal_promotion_enabled": internally_promoted,
        "in_generated_watchlist": wallet in generated if generated is not None else None,
        "last_runtime_identity_cluster": identity.get("identity_id"),
        "runtime_identity_known": identity.get("identity_known", False),
        "last_runtime_identity_at": utc(identity.get("timestamp")),
        "historical_identity_cluster": history.get("identity_cluster"),
        "historical_funder": history.get("funded_by"),
        "historical_identity_at": utc(history.get("generated_at")),
        "relation_funder_flags": ";".join(flags) or "UNKNOWN",
        "infrastructure_excluded": excluded,
    }


def build_ranking(db_path="data/arc_bot.sqlite3", *, cfg=SETTINGS, now=None,
                  watchlist_path="generated_watchlist.json", history_path="data/history_report.json", policy=None):
    now = int(time.time()) if now is None else now
    policy = policy or sizing_policy()
    excluded = infrastructure_addresses(cfg)
    watchlist = optional_json(watchlist_path)
    generated = {r["address"].lower() for r in watchlist["wallets"]} if watchlist is not None else None
    history_report = optional_json(history_path) or {}
    history = {r["wallet"].lower(): dict(r, generated_at=history_report.get("generated_at"))
               for r in history_report.get("ranked_wallets", [])}
    # mode=ro cannot create a missing DB, migrate it, or write profiling outcomes.
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")  # One coherent WAL snapshot, even while the scanner runs.
        def indexed(table):
            return {r["wallet"].lower(): dict(r) for r in db.execute("SELECT * FROM "+table)}
        candidates, profiles, promoted = (indexed(t) for t in ("discovery_candidates", "wallet_profiles", "promoted_wallets"))
        epoch_row = db.execute("SELECT started_at FROM observation_epochs WHERE name='launch_universe'").fetchone()
        epoch = epoch_row[0] if epoch_row else None
        research = {r[0]: r[1] for r in db.execute("SELECT lower(wallet),COUNT(*) FROM research_trades GROUP BY lower(wallet)")}
        observed = {r[0]: r[1] for r in db.execute("""
            SELECT wallet,COUNT(*) FROM (
                SELECT DISTINCT lower(e.wallet) AS wallet,lower(e.tx_hash),lower(e.token)
                FROM events e LEFT JOIN discovery_candidates c ON lower(c.wallet)=lower(e.wallet)
                WHERE e.side='BUY' AND e.trade_verified=1 AND e.token_amount>0
                AND e.timestamp>=max(?,coalesce(c.first_discovered_at,0))
            ) GROUP BY wallet""", (epoch,))} if epoch is not None else {}
        identities = {}
        for r in db.execute("""SELECT wallet,timestamp,payload FROM (
                SELECT wallet,timestamp,payload,
                       row_number() OVER (PARTITION BY lower(wallet) ORDER BY timestamp DESC,rowid DESC) AS n
                FROM runtime_events) WHERE n=1"""):
            identities[r["wallet"].lower()] = dict(profile_payload(dict(r)), timestamp=r["timestamp"])
        rows = [rank_wallet(w, candidates.get(w, {}), profiles.get(w, {}), promoted.get(w, {}),
                            research.get(w, 0), observed.get(w, 0), epoch, generated,
                            history.get(w, {}), identities.get(w, {}), w in excluded, cfg, now)
                for w in candidates.keys() | profiles.keys()]
        buys = forward_buys(db, now)
        sources = {}
        for r in db.execute('SELECT lower(wallet),lower(token) FROM candidate_sources'):
            sources.setdefault(r[0], set()).add(r[1])
        for row in rows:
            row.update(allocation_metrics([] if row['infrastructure_excluded'] else buys.get(row['wallet'], []), policy, cfg.winner_return_pct))
            row['winner_source_tokens'] = sorted(sources.get(row['wallet'], set()))
            row['reason_not_promoted'] = row['promotion_blockers']
    attach_clusters(rows)
    priority = {"VALIDATED": 0, "PROMISING": 1, "WATCH": 2, "REJECTED": 3}
    return sorted(rows, key=lambda r: (priority[r["report_class"]],
        # Positive allocation evidence can raise display rank, never demote a
        # wallet's internal status or give it confirmation power.
        -max(0, r['capital_weighted_return'] or 0),
        -(r["forward_winners"] or 0), -(r["forward_evaluable_trades"] or 0),
        -(r["forward_buys_observed"] or 0),
        -(r["score"] or 0) if r["forward_launches_bought"] is not None else 0,
        r["data_status"] != "COMPLETE", -(r["independent_sources"] or 0),
        -(r["winner_sources"] or 0), r["wallet"]))


def summarize(rows):
    classes = Counter(r["report_class"] for r in rows)
    return {"total": len(rows), "incomplete": sum(r["data_status"] == "INCOMPLETE" for r in rows),
            **{c.lower(): classes[c] for c in ("REJECTED", "WATCH", "PROMISING", "VALIDATED")},
            "forward_buys_ge_1": sum((r["forward_buys_observed"] or 0) >= 1 for r in rows),
            "forward_buys_ge_3": sum((r["forward_buys_observed"] or 0) >= 3 for r in rows),
            "complete": sum(r["data_status"] == "COMPLETE" for r in rows),
            **cluster_summary(rows)}


def display(value):
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def display_field(key, value):
    if value is not None and key in {'capital_weighted_hit_rate', 'capital_weighted_return',
            'outcome_capital_coverage', 'largest_trade_contribution', 'conviction_accuracy',
            'internal_capital_concentration', 'best_wallet_return'}:
        return f'{value:.2%}'
    if value is not None and key == 'profit_factor':
        return f'{value:.2f}'
    return display(value)


def export_csv(rows, csv_path):
    path = Path(csv_path)
    if path.suffix.lower() != ".csv":
        raise ValueError("Report output must be a CSV file")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["wallet", "report_class"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=True, allow_nan=False) if isinstance(v, (list, dict)) else v
                         for k, v in row.items()} for row in rows)


def write_report(rows, csv_path, top=None, diverse=False, per_cluster=2):
    full, selected = select_view(rows, top, diverse, per_cluster)
    export_csv(full, csv_path)
    print("Classement descriptif uniquement. Aucune autorite de signal accordee.")
    print("Hit = winners/evaluables forward; N/A = inconnu, pas zero. Dates UTC.")
    print('Capital: rendements bruts a horizon fixe; pas des profits realises. Couverture des outcomes indiquee.')
    print(f"{'#':>3} {'Categorie':<10} {'Wallet':<42} {'Discovery':<12} {'Score':>5} {'Classe moteur':<13} {'Buys':>5} {'Hit%':>6} {'Capt%':>6} Donnees")
    print(f"Vue: {'diversifiee' if diverse else 'brute'} | {len(selected)} wallets affiches")
    for i, r in enumerate(selected, 1):
        print(f"{i:3} {r['report_class']:<10} {r['wallet']:<42} {r['discovery_status']:<12} "
              f"{display(r['score']):>5} {display(r['current_class']):<13} "
              f"{display(r['forward_buys_observed']):>5} {display(r['forward_hit_rate_pct']):>6} "
              f"{display(r['capture_pct']):>6} {r['data_status']}")
        for label, keys in (
            ("Sources", ("first_discovered_at", "winner_sources", "independent_sources", "research_trades")),
            ("Forward", ("forward_launches_bought", "forward_evaluable_trades", "forward_winners", "profile_generated_at")),
            ("Capital", ("capital_weighted_hit_rate", "capital_weighted_return", "profit_factor", "outcome_capital_coverage")),
            ("Sizing", ("median_buy_usdc", "largest_buy_usdc", "largest_trade_contribution", "conviction_accuracy", "sizing_confidence", "allocation_evidence_confidence", "concentration_risk")),
            ("Etat", ("in_generated_watchlist", "infrastructure_excluded", "last_engine_promotion_reason")),
            ("Relations", ("last_runtime_identity_cluster", "last_runtime_identity_at", "historical_identity_cluster", "historical_funder", "historical_identity_at", "relation_funder_flags")),
            ("Cluster", ("identity_cluster_id", "identity_cluster_size", "related_wallets_count", "independent_cluster_count", "same_cluster_wallet_count_in_top")),
        ):
            print(textwrap.fill(label+": "+" | ".join(k+"="+display_field(k,r[k]) for k in keys),
                                width=140, initial_indent="    ", subsequent_indent="      ",
                                break_long_words=False, break_on_hyphens=False))
        blockers = r['promotion_blockers'].split(';') if r['promotion_blockers'] else []
        print("    Blocages principaux: "+("; ".join(blockers[:3]) or "aucun critere stocke bloquant")+
              (f" (+{len(blockers)-3} dans le CSV)" if len(blockers) > 3 else ""))
    print("Resume (tous les wallets): "+" | ".join(f"{k}={v}" for k, v in summarize(rows).items()))
    print(f"CSV complet: {csv_path}")


def write_cluster_report(rows, path, top=None, policy=None):
    clusters = cluster_ranking(rows, policy or sizing_policy(), SETTINGS.winner_return_pct)
    export_csv(clusters, path)
    print('Clusters descriptifs; identites inconnues != confirmations independantes.')
    print('Cluster | Wallets | Actifs | Sources distinctes | Buys | Winners | Capital USDC | Return pondere | Confiance')
    for r in clusters[:top]:
        print(' | '.join(display_field(k,r[k]) for k in ('cluster_id','wallet_count','observed_wallet_count',
            'winner_sources_distinct','forward_buys','forward_winners','capital_total_observed',
            'capital_weighted_return','cluster_confidence')))
        print('    '+ ' | '.join(k+'='+display_field(k,r[k]) for k in ('best_wallet_return',
            'internal_capital_concentration','concentration_risk','relation_funder_flags')))
    print('Resume global: '+ ' | '.join(f'{k}={v}' for k,v in summarize(rows).items()))
    print(f'CSV clusters complet: {path}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, help="Limit console rows only; CSV and summary include everyone")
    parser.add_argument("--db", default="data/arc_bot.sqlite3")
    parser.add_argument("--csv", default="data/wallet_ranking.csv")
    parser.add_argument("--watchlist", default="generated_watchlist.json")
    parser.add_argument("--history", default="data/history_report.json")
    parser.add_argument("--diverse", action="store_true", help="Display at most --per-cluster wallets per identity")
    parser.add_argument("--per-cluster", type=int, default=2)
    parser.add_argument("--clusters", action="store_true", help="Display cluster ranking instead of wallet rows")
    parser.add_argument("--cluster-csv", default="data/cluster_ranking.csv")
    args = parser.parse_args(argv)
    if args.top is not None and args.top < 1:
        parser.error("--top must be positive")
    if args.per_cluster < 1:
        parser.error('--per-cluster must be positive')
    if args.diverse and args.clusters:
        parser.error('--diverse applies to wallet rows, not --clusters')
    try:
        rows = build_ranking(args.db, watchlist_path=args.watchlist, history_path=args.history)
        if args.clusters:
            full, _ = select_view(rows, args.top)
            export_csv(full, args.csv)
            write_cluster_report(rows, args.cluster_csv, args.top)
        else:
            write_report(rows, args.csv, args.top, args.diverse, args.per_cluster)
            export_csv(cluster_ranking(rows, sizing_policy(), SETTINGS.winner_return_pct), args.cluster_csv)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, InfrastructureRegistryError):
        parser.exit(1, "Rapport impossible: verifier la base, le registre DEX et les fichiers JSON/CSV. Aucun etat moteur modifie.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
