# Shortlist copy research (offline only)

## Incomplete-position research scenarios (explicit offline report)

`copy_recovery_report.py` leaves the strict score/ROI in `copy_candidate_report.py`
unchanged. It reads the selected wallets' stored Nansen snapshots and produces a
separate report, never consumed by promotion, wallet selection or execution.
The default scope is the existing shortlist. To examine a different previously
saved scope, pass its explicit shortlist-format file; no automatic broad scan or
provider request is added. The owner's global research scope can be reused:

```powershell
.\.venv\Scripts\python.exe copy_recovery_report.py --shortlist outputs/copy_global_check_20260922/global_research_scope.json --output-dir outputs/copy_recovery_next_review
```

The output directory must be new. Existing evidence is never overwritten. The
JSON preserves strict metrics, original snapshot key/date/flags, all omitted
positions, recovery reasons and a token review queue grouped by token AND original
historical window. The HTML prioritizes single-position blockers, then 2–3.
There is no minimum activity requirement or contract-wallet exclusion. Reviewed
DEX infrastructure remains excluded. All recovery rows have false paper/live
authority; the original `copy_research_eligible` remains false for partial data.

For an open position without a usable price/open view, the scenario assumes its
remaining value is zero **only if** costs, sales and quantities reconcile:

* `implied_sold_cost = sold_usd - realized_pnl`
* `remaining_cost = bought_usd - implied_sold_cost`
* `scenario_pnl = realized_pnl - remaining_cost = sold_usd - bought_usd`

Both implied costs must be nonnegative. Positive finite purchase costs and bought
quantities are required. Bought quantity must equal sold plus held quantity within
floating-point tolerance; transfers/rebases/incomplete inventory are not inferred
away. Overlapping views must agree on costs, sales, realized PnL and quantities.
When no quantity was sold, nonzero sales/realized PnL are inconsistent. The sources
are never edited and no artificial market price is inserted.

Known positions retain their observed total PnL, including latent losses. Wallet
scenario metrics exist only if **every** position is either source-evaluable or
supported by this zero-value accounting scenario. Zero-cost receipts, missing
costs and contradictions remain unresolved: no infinite ROI, no omission of an
unprofitable/unknown position, no scenario promotion. Source aggregate summaries
or current market prices never fill historical gaps.

The `prudent_scenario` is explicitly hypothetical, based on provider accounting,
not a verified return or guaranteed lower bound after fees/transfer corrections.
Its experimental score uses the existing formula but is not a mathematical lower
bound on another scenario's score (concentration can change). Original strict
score and ROI stay null where they were null. `research_recovery_candidate`
requires scenario ROI >=20% and mean operations/day <=50 by default, using the
same user criteria. It is an audit priority only, not a funded/validated wallet.

Known-capital coverage is source-evaluable purchase cost / total deduplicated
purchase cost. It is unavailable if any cost is zero/unknown; it does not claim
full portfolio coverage. Additional capital-weighted hit rate/return, profit
factor and median cumulative token purchase cost are **position-level** metrics,
not per-buy sizing or evidence of repeated successful trades. No losses means an
unavailable profit factor, not infinity. Current funding, liquidity, slippage,
delay sensitivity and trade chronology remain explicitly unverified/unavailable.

On the first owner-requested global run, 7,554 stored-scope addresses produced
3,928 unchanged strict scores and 2,083 hypothetical scenarios among 3,239 partial
wallets. 442 scenario wallets passed ROI/frequency, including 247 single-position
cases. 1,156 partial wallets remained unresolved. These counts describe one saved
run, not guaranteed recoverable profits. No Nansen credit was consumed.

Run from the repository directory:

```powershell
.\.venv\Scripts\python.exe copy_candidate_report.py --top 20
.\.venv\Scripts\python.exe copy_candidate_report.py --json
```

Edit `data/copy_research_shortlist.json` to add/remove addresses. At the owner's
request the first expansion contained 50 wallets, preserving all 15 originally supplied.
The 35 additions were selected once from the existing frozen Nansen report,
alternating descending realized PnL, realized ROI, and winning-token count.
Only positive realized and total PnL with stored complete pages were considered
for these additions; reviewed infrastructure was excluded. There is no minimum
trade count. These are research leads, not a claim of robust copy performance.
No new x2/x5/x10 score was computed for the full universe to make this selection.
Selection provenance and the previous list are retained under
`data/evidence_archives/copy_research_shortlist/`. No automatic expansion occurs.
After the subsequent request for 30 additional funded candidates, the research
list contains 80 wallets. The prior 50 are archived and retained in the list;
only the 30 new confirmed candidates are appended. There is no hard size limit.
Duplicate/case variants are normalized in memory only.
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

## Copy frequency ceiling (owner-approved: 50/day)

`COPY_MAX_OPERATIONS_PER_DAY=50` excludes wallets above 50 reported trading
operations per day **on average over the exact stored date window** from copy
research candidates. Exactly 50 passes. Numerator: Nansen summary `traded_times`;
denominator: `(date.to - date.from).total_seconds()/86400`, without rounding.
This does not measure active-day frequency, a daily peak or holding duration.
Missing/invalid count or dates produce `UNKNOWN_FREQUENCY`, never zero activity.

All shortlist entries, positions and historical scores are preserved. The report
shows operation count, days, daily average and frequency status. Only scored
wallets within the ceiling have `copy_research_eligible=true` and appear first;
excluded or unknown-frequency entries remain visible below, with no replacement
or bulk expansion. This flag is local research eligibility, not `PAPER_SELECTED`
or live authority. Existing engine scoring, historical collection and paper
execution are unchanged. No provider calls or SQLite writes are needed.

## Position score

Owner-approved copy research minimum: `COPY_MIN_ROI_PCT=20`. A total ROI below
20% produces `EXCLUDED_LOW_ROI`; exactly 20% passes. Missing/partial ROI remains
`UNKNOWN_ROI`, never an assumed zero or eligibility. Only scored wallets that
pass both frequency and ROI criteria have `copy_research_eligible=true`.
Historical scores and all evidence are retained. These research-only exclusions
do not modify engine thresholds, paper admission or live authority. Exclusion
counters can overlap (a wallet may fail both frequency and ROI).

One observation is one distinct token position over the provider's stored date
range, **not one purchase, one closed trade or the token's peak price**. The two
views are deduplicated by token. Realized PnL comes from the realized view when
present; unrealized PnL comes from the open view when present. For an open-only
token, both components come from that row. Bought USD is the total observed buy
cost, counted once. Conflicting overlapping costs/realized PnLs are flagged.

`multiple = 1 + (realized_pnl + unrealized_pnl) / bought_usd`.
Wallet `roi_pct = 100 * sum(realized_pnl + unrealized_pnl) / sum(bought_usd)`
over the same deduplicated positions; `capital_multiple = 1 + roi_pct / 100`.
Costs are counted once across views. No wallet ROI is displayed for a partial,
empty or invalid-cost sample. This is a return on cumulative purchases, not on
initial wallet equity (reinvested funds may be counted on multiple buys), not
annualized and not a simulated copy return. ROI filters research eligibility only;
it does not affect the score formula or engine authority.
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

## Explicit current-funds check (separate from offline scoring)

`copy_funds_check.py` accepts an already scored, ordered JSON array and checks
at most 120 candidates by default, stopping at 30 wallets with at least $10 in
observed USDC plus valued known tokens (`--minimum-usd 10`). It uses
only the public Arc RPC, at one request/second through the existing retry and
chain-verification transport. It does not use Alchemy or Nansen credentials.
This is a manual action: the offline report never imports or invokes it. For a
wallet with less than 10 USDC, up to 20 known token addresses from its already
stored positions are checked, prioritizing previously valued holdings. Token
quantities and decimals are read on-chain; current public DEX Screener quotes
must match Arc and the exact base-token address and have positive liquidity.
The deepest quoted pair is used. This is mark-to-market evidence, not proof of
sellability or an executable liquidation value. Token checks stop when the
minimum is established; not every asset or chain is inventoried.

Chain ID must be 5042. The USDC predeploy is
`0x3600000000000000000000000000000000000000`; its decimals must be 6. The balance
is obtained with `eth_call balanceOf` at one recent block (maximum age 5 minutes
at start), with the block hash rechecked at the end. Arc's native and ERC-20 USDC
are the same balance and must never be added together. Reference:
https://www.arc.io/blog/supporting-arc-in-wallets-one-balance-usdc-fees-and-complete-history

Raw accepted responses and wallet observations are flushed to a new append-only
JSONL file. A new final JSON file is created only on completion; neither file is
overwritten. Wrong chain, stale blocks, malformed balances or reorgs cannot
confirm funds. A failed read is UNKNOWN, never zero. A zero USDC balance does not
prove a zero portfolio: the wallet may hold tokens or positions. Below $10 in the
observed subset means `BELOW_MINIMUM_OBSERVED_PORTFOLIO_INCOMPLETE`, not an empty
wallet. Positive dust alone cannot pass. Token-only or mixed holdings can pass.
USDC and its native mirror are excluded from the token loop to avoid double counting.
All price payloads and successful raw balance responses are archived. Evidence is dated and
can become stale immediately; it confers no engine/paper/live authority.

Example (explicit RPC network access, no Nansen requests):

```powershell
.\.venv\Scripts\python.exe copy_funds_check.py --candidates outputs/candidates.json --output data/new_funds_evidence.json --target 30 --maximum 120 --minimum-usd 10
```

For the owner's request for 30 additional wallets, a separate persisted research
pool of 240 new addresses was selected from the existing frozen report, excluding
the previous 50 and reviewed infrastructure. Preselection used positive total
and realized PnL, reported realized ROI >=20%, and <=50 operations/day, alternating
winning-token count, win rate and reported realized ROI. Only these 240 stored
detailed payloads were rescored offline: 120 were calculable and passed total ROI
and frequency criteria. This is a bounded shortlist search, not a claim of the
global top 30 across every stored wallet. The pool, its provenance, scores and
balance evidence are retained separately; no Nansen refresh or bulk rescan occurs.
59 of those candidates were checked to establish 30 with observed value >=$10;
5 of the 30 required token valuations to reach the minimum. The previous 9
candidates were also checked: 6 reached the minimum, 3 were not confirmed.
The dated funded-review file/report is separate from the general offline score
report: the latter's eligibility flag refers to ROI/frequency/data quality/holding duration,
not a fresh balance guarantee. No funded-review file is consumed by the engine.

## Minimum holding duration (owner-approved, 22 September 2026)

`COPY_MIN_HOLDING_SECONDS=60` adds a research selection gate. Below 60 seconds
is `EXCLUDED_SHORT_HOLDING`; exactly 60 passes. The owner chose the median time
from each acquisition to its **first partial sale**, not the final dust exit.
`copy_holding_duration.py` matches confirmed swaps FIFO per token. There is one
unweighted sample per buy lot with an observed first sale, regardless of later
sales or capital size. FIFO is an accounting convention for fungible inventory.
This is a descriptive historical statistic, not proof of profitable execution.

Still-unsold buys are censored: counted separately, not assigned zero or the
current age, and excluded from this observed-exit median. Always inspect sample
size, unsold count and dated coverage; one observed exit is weak evidence even
though no minimum activity filter is added. Open losses remain in the existing
ROI calculations. Nansen position aggregates and date windows cannot establish
holding times. We read only complete version-1 local historical generations, or
the retained non-invalidated completed generation during refresh; expose its
cutoff and source. We never merge generations. Collection errors, missing payloads,
inventory gaps, unknown timestamps, unmatched sells, conflicting duplicates and
ambiguous trade order produce `UNKNOWN_HOLDING_DURATION`, which does not pass.
Missing evidence stays in the report/shortlist for later audit, with its score
unchanged. There is no automatic fetch or reconstruction.

`performance_research_eligible` and `performance_recovery_candidate` retain the
ROI/frequency assessment. `copy_research_eligible` and `research_recovery_candidate`
add the mandatory duration gate for both strict and prudent research candidates.
Neither flag provides paper/live authority. No engine threshold, watchlist,
checkpoint, or historical evidence is changed. Previously exported rankings
remain archived, but predate this additional filter.

```powershell
.\.venv\Scripts\python.exe copy_candidate_report.py --top 50 --eligible-only
```

Omit `--eligible-only` to see pending/excluded rows and their timing status; `--json`
always includes the full scope and diagnostics. Recovery exports use the same
default 60-second gate, configurable explicitly with `--min-holding-seconds`.
Both commands remain offline: zero Nansen calls/credits and read-only SQLite.

## Explicit public-RPC timing pilot

`copy_holding_pilot.py` is a separate, manually invoked evidence collector, not
part of the scoring command or supervisor. It requires a scope of exactly three
wallets with existing version-1 histories. It copies their work into a new local
database, resumes pagination there, reuses valid cached reads and decoded records,
and retries ambiguous records through the existing strict decoder. The source
database is read-only. It never invokes engine selection, identity refresh,
collector reset, signing, or broadcasting.

Only `https://rpc.arc-scan.org`, `https://rpc.mainnet.arc.io` and the Arcscan
address-activity route are allowed. Neither Nansen nor Alchemy credentials are
used. All HTTP attempts including retries count toward the limits (at most 300
RPC and 12 Arcscan attempts per invocation, at most 600 seconds before new
requests). Requests are paced at one per second and share the existing public
fallback limiter. Successful response payloads are durably archived, with a
manifest, original wallet snapshot, isolated SQLite and raw RPC cache.
Timing remains UNKNOWN after absent receipts, invalid pages, incomplete decoding,
inventory gaps or unconfirmed trades. A completed collection is not proof that
every trade is understood. No result automatically changes the production ranking.

For an explicit continuation, pass the previous isolated `pilot.sqlite3` via
`--db`, reuse its cache with `--reuse-cache`, choose a **new** output directory,
and subtract all previous attempts from the original authorized budget. Limits
are per invocation; the script does not create a new budget authorization.
Missing/pruned receipts on one public provider must not be interpreted as no
trade; any alternate provider is explicitly selected and chain-checked at 5042.


On 2026-09-23 the owner explicitly lowered this research minimum from 120 to 60
seconds. The definition remains the median BUY-to-first FIFO partial sale.
Older evidence reports retain their original 120-second criterion. Recompute
from stored transactions to also update fast-exit counts; do not relabel unknown
histories as eligible. This reporting change adds no paper/live authority and
does not guarantee copy latency or a one-minute holding time for every trade.
