# Issue #1 — follow-up corrections

Date: 2026-09-16. This document supersedes the implementation status in the
original audit's residual-limitations section. It distinguishes corrected code
from claims that still require deployment evidence or future observations.

## Findings and resulting behavior

| Audit finding | Correction | Remaining evidence needed |
| --- | --- | --- |
| Winner-selected histories contaminated scoring | Historical discovery remains a source of candidates only. Version 2 profiles use observed purchases after both candidate discovery and the launch-universe observation epoch. | Forward observations cannot establish an edge in advance. |
| Opportunity denominator depended on liquid survivors and trade-only hours | All observed ERC-20 deployments are considered, including those without a market quote. Deployments must fall in the wallet's recorded block coverage and activity hours. Incoming/outgoing transfers and failed transactions also establish activity. Purchases do not inflate the denominator. | Explorer omissions and economic actions without supported logs remain outside coverage. |
| Retrospective maxima and later position additions inflated outcomes | One first purchase per token, its own execution price and initial stake; one quote in a fixed future window. Pending, missing and observed outcomes are separate. Missing mature outcomes remain in the conservative hit-rate denominator and block confirmation eligibility. | Quotes are observations, not executable or realized returns. |
| Repeated signals looked like independent backtest trials | First forward VALIDATED signal per token only; report missing outcomes, bounds on win rate and simulated realized PnL separately. Signal outcome targets are registered with the signal. | No statistical certification or threshold calibration is claimed. |
| Launch discovery could lose pages after a burst or restart | Persist stream cursors, previous head, seen cursors and completion/error state. Resume interrupted batches; reject malformed, capped and cyclic pages. NFT pages preserve contract cursor boundaries. | Provider ordering and completeness are still external assumptions. |
| Relayed transactions were skipped by `tx.from` filtering | Scanner additionally discovers watched senders/recipients through indexed Transfer logs, including the native mirror. Hydration no longer rejects a receipt solely because another account submitted it. | Unsupported token-to-token swaps, custom event formats and missing native traces stay unverified. |
| Swap topics could be spoofed | Require a configured deployment address and historical runtime-code hash, plus directed cash and token paths to the same trusted emitter. Unknown emitters cannot confirm. | No production DEX is implicitly trusted. Provenance review is required before populating the registry. |
| Distinct winner tokens were treated as independent | Collapse winners by deployer/funding component; unknown deployers do not count. Extend wallet funding graph through one additional ancestor. | Funding components are a heuristic, not proof of distinct people or statistically independent outcomes. |
| Identity HTTP calls delayed scanning and stale data persisted | Bounded background refresh, per-worker HTTP sessions, cache TTL and failed-request backoff. Apply completed snapshots for the matching watch set only. | Unlabelled services and deeper/different funding paths still limit identity inference. |
| Event completion was separate from alerts, proposals and paper state | SQLite transactions/savepoints atomically commit the event, context, signal, alert intents, proposal, paper fills and completion marker. Stable keys prevent replay duplication. | Telegram cannot provide atomic commit with SQLite; ambiguous delivery requires review. |
| Restart lost clusters and competing scanners could conflict | Restore recent persisted cluster events, prune expired tokens, and acquire an OS process lock for the database's scanner. | One host/local database is supported; this is not a distributed lock. |
| Profiles retained authority despite missing or stale evidence | Require a fresh version 2 profile with explicit data quality; no authority fallback to seed/reputation weights. Previously promoted candidates return to quarantine on migration. Untrusted sell events cannot retrigger an older eligible paper exit. | Expect a period without validated signals while evidence matures. |
| Paper results omitted costs and fills | Apply configurable fees, slippage, liquidity-dependent impact and per-fill capacity; support partial fills, proportional cost basis, durable fill history and later re-entry. Missing price/depth/volume means no fill. | Simulation assumptions need calibration; repeated fills do not model a complete AMM/order book or shared liquidity consumption. |

## Observation and scoring policy

The numerical score weights, classification cutoffs, promotion gates, winner
threshold and stop/take-profit thresholds are unchanged. The **evidence feeding
them has changed explicitly**: the production scoring path uses forward,
fixed-horizon results. Old retrospective helper calculations are retained only
for inspecting historical data and existing regression fixtures.

Defaults added in `.env.example`:

- `OUTCOME_HORIZON_SECONDS=86400`, `OUTCOME_GRACE_SECONDS=300`: first valid quote
  from 24 hours through 24 hours + 5 minutes after entry. Late responses are not
  substituted for the target. Changing settings does not rewrite stored targets.
- `LAUNCH_DISCOVERY_PAGES=10`: per-cycle pagination budget, with persisted resume.
  Initial contract discovery backfills to the end before creating the observation
  epoch. The supplemental transfer stream starts within `MAX_PAIR_AGE_HOURS`.
- `IDENTITY_CACHE_SECONDS=3600`, `IDENTITY_WORKERS=4`: bounded refresh;
  failed facts retry with exponential delay from 30 seconds to one hour.
- `PAPER_FEE_BPS=30`, `PAPER_SLIPPAGE_BPS=50`: illustrative 0.30% fee and 0.50%
  slippage per fill, plus `notional / liquidity` impact.
- `PAPER_LIQUIDITY_FRACTION=0.001`, `PAPER_VOLUME_FRACTION=0.01`: each fill is
  capped by the smaller of 0.1% of quoted liquidity and 1% of five-minute volume.
  These are modeling assumptions, not measurements of Arc execution costs.

Data quality also requires a completed contract discovery pass no older than
`max(4 * LAUNCH_POLL_SECONDS, 60)` seconds. Exported profiles expire after
`max(4 * INTELLIGENCE_RELOAD_SECONDS, 120)` seconds. Recorded reconstruction
gaps, unmatched buys or missing mature quotes prevent confirmation. Old gaps are
retained as evidence, not erased to make a wallet promotable. Coverage and outcome
tables grow with observation history; disk use should be monitored.

## Deployment trust and migration

1. Stop all four processes and back up the research SQLite database before
   deploying. Schema version 2 is additive; concurrent startup migration is tested.
   It preserves old observations, disables legacy automatic promotions and returns
   promoted candidates to quarantine for forward validation.
2. Copy `trusted_dex.example.json` to the path in `TRUSTED_DEX_REGISTRY`.
   For each **reviewed immutable** pool or PoolManager, supply `address` and
   `runtime_code_hash` (the 0x-prefixed Keccak-256 of deployed runtime bytecode),
   with the correct top-level `chain_id`. A hash match alone does not establish
   provenance or safety; upgradeable proxies need separate implementation review
   and are not covered by this immutable-code trust model. Restart after changes.
3. The supplied registry is deliberately empty: the audit observed a V4 emitter,
   but observation is not deployment certification. Until reviewed deployments
   are configured, swaps remain research-only and cannot validate signals or
   promote wallets. Configure trust before collecting the forward scoring cohort.
4. Restart the four processes. Candidates are observed with zero authority during
   quarantine (up to the configured candidate observation limit). Both manually
   listed and automatic wallets need fresh forward evidence. Historical winners
   cannot shortcut the fixed-horizon observation period or sample requirements.

This change does not start the live bot, send Telegram messages, sign transactions
or modify the user's existing runtime database. Tests use isolated databases and
mock notification transports. No `.env` contents are copied or committed.

## Notification and proposal recovery

`alert_outbox` is authoritative. Successful delivery becomes `SENT`; explicit
Telegram rate-limit rejection remains `PENDING` with backoff; permanent rejection
becomes `FAILED`. Timeout, ambiguous server response, or restart during `SENDING`
becomes `UNKNOWN`, with no automatic resend. Check Telegram before deciding on
manual recovery. Exactly-once delivery across SQLite and Telegram is not promised.
`python status.py` shows delivery counts, cursor errors, gaps and pending proposals.

Proposals have stable identifiers and one pending record per token/side in SQLite.
`data/proposals.jsonl` is an atomic export of that table, not an execution queue.
Editing that export does not approve a trade. No real-money action is implemented.

## Verification

The deterministic suite covers transactional crash/replay, nested savepoints,
four-process migration, scanner ownership, persisted pagination and malformed
pages, missing/stale evidence, spoofed emitters and cash paths, relay discovery,
fixed outcome windows, missing-return bounds, related deployers, background
identity refresh, partial paper fills and re-entry. The final test count is
recorded in the PR and original audit's validation section.

Results: **103 deterministic tests passed**, including four-process SQLite;
**8 opt-in public network tests passed** in the follow-up run. All 49 Python
files compile and `git diff --check` passes. A dependency emits the existing
`websockets.legacy` deprecation warning.

The public network cases and captured live fixtures verify endpoint/receipt
assumptions. They do not certify deployment trust,
economic history completeness, anti-Sybil completeness, or future profitability.
