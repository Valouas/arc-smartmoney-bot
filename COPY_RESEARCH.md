# Shortlist copy research (offline only)

Run from the repository directory:

```powershell
.\.venv\Scripts\python.exe copy_candidate_report.py --top 20
.\.venv\Scripts\python.exe copy_candidate_report.py --json
```

Edit `data/copy_research_shortlist.json` to add/remove addresses. The initial
list is exactly the 15 addresses explicitly supplied by the owner; no bulk
selection is inferred. Duplicate/case variants are normalized in memory only.
Missing details stay in the list as `MISSING_NANSEN_DETAIL`. Invalid latest
details are `INVALID_NANSEN_DETAIL`; no older snapshot or summary substitutes.
The file is never rewritten by the report.

`COPY_SHORTLIST_ONLY=true` is the default, also documented in `.env.example`.
False is explicitly rejected in this version; it cannot enable a bulk report.
Only indexed queries for shortlist wallets against the latest `provider='nansen'`
row in `external_wallet_metrics` are used (retrieved_at, observed_at, snapshot_key
descending for deterministic ties). The SQLite connection is read-only, inside
one read transaction. No HTTP/client import, campaign, refresh, migration or write.
`--top` limits display only. JSON includes every shortlist entry and provenance.

## Metric definitions — descriptive, not engine thresholds

One observation is one distinct token position over the provider's stored date
range, **not one purchase, one closed trade or the token's peak price**. The two
views are deduplicated by token. Realized PnL comes from the realized view when
present; unrealized PnL comes from the open view when present. For an open-only
token, both components come from that row. Bought USD is the total observed buy
cost, counted once. Conflicting overlapping costs/realized PnLs are flagged.

`multiple = 1 + (realized_pnl + unrealized_pnl) / bought_usd`.
Counts x2/x5/x10 mean **at least** that multiple, including current latent losses.
These are provider-accounting outcomes, not achievable copy returns or proof
that a wallet bought before a token's all-time x10. Win rate = positive total
token outcomes / evaluable token positions. Sample size is that denominator.
Concentration = largest positive token PnL / sum of positive token PnLs
(zero when all evaluated positions lose, unavailable for no sample).

Wilson is the 95% lower bound on positive token outcomes. Tokens can be correlated
and this cohort was preselected: Wilson is a descriptive small-sample adjustment,
**not** a calibrated probability of future profitability or independent trials.

New experimental research-only formula, range 0–100:

`100 * (0.5*Wilson(win) + 0.2*Wilson(x2) + 0.2*Wilson(x5) + 0.1*Wilson(x10))
 * (1 - 0.5*concentration)`

Absolute PnL has no weight and no tie-breaking role. Ties use wallet address.
No score is assigned for an empty sample or any omitted/unpriced/conflicting
position, to avoid a favorable score obtained by silently dropping unknown losses.
Diagnostic counts still show their evaluable subset and omitted reasons in JSON.
Such wallets remain visible, observed and in the shortlist; this is not rejection.
Reviewed DEX infrastructure has no score. No EOA/contract heuristic is used.

Pagination completeness is not engine data completeness. Provider flags, date
range, retrieval timestamp and snapshot key remain available in JSON. Gas, prices,
accounting, chronology and repeatability need independent review. No score causes
`PAPER_SELECTED`, promotion, live authority or restart. Paper remains as configured.
No changes to Nansen bulk or any running process are made by this module.

Counters printed by the report refer only to this offline scoring invocation.
They are not account-wide billing counters for any independent running service.
`Wallets scored` counts numeric scores; missing detail counts absent or invalid
stored detailed payloads. Partial position data may be available but unscored.
