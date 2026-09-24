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

## PR #2 live transfer pagination regression

The live `/v1/explore/token-transfers?limit=3&standard=erc20` response includes
valid row identifiers and pagination but reports `ranking.complete=false`.
Requiring a complete ranking for this supplemental stream incorrectly persisted
`transfers: incomplete_page_or_transport` on every cycle.

Only `contracts` now requires an explicit boolean `ranking.complete=true`.
Both streams still reject capped/truncated responses, malformed pagination,
inconsistent `has_more`/`next`, cyclic cursors and missing row identifiers.
Transfers provide token candidates without deployment metadata; they cannot
establish the deployment observation epoch or replace healthy contracts coverage
for profile eligibility and automatic promotion. An accepted transfer page clears
its previous error through the existing cursor update; no database reset is needed.

Validation: the captured three-row response failed before the fix and passes
afterward. **124 deterministic tests pass**. The eight existing public network
tests pass. A new public test executes the actual `launch_indexer.py --once`
entry point in a temporary working directory with dotenv disabled, two pages per
stream, three rows per page, and market lookups disabled. Its first run accepted
transfers but failed on contracts. A diagnostic confirmed an Arcscan transport
failure while paging contracts; the targeted retry passed with
`contracts=6 transfer_tokens=5`. Both stored stream errors were `NULL` on that
successful run. No transport/completeness guard was relaxed for the retry.

To apply to an existing installation, stop the running processes, update
`fix/issue-1-v4-audit`, run `python launch_indexer.py --once`, then
`python status.py`. Under `DELIVERY / DISCOVERY HEALTH`, the transfers row should
show `error: None` after an accepted page. A nonempty cursor is normal when more
pages remain. A genuine later outage or malformed response still produces an
error; this fix does not mask it.

## PR #2 saved-cursor deployment-standard regression

The subsequent live error was reproduced **before** changing parser behavior,
using the contracts cursor read from the user's database in read-only mode. The
public response contained 100 rows, `ranking.complete=true`, and two rows with
the exact value `token.standard="unknown"` (no top-level `standard` field).
The first was `0x7ed6c2f2df8971bc5f648d117c6cb931a1cd9aa8`, at block 21173099;
the other was `0xac167b3d0636914785b56c19f1af83cea9ca66bf`. The pre-fix parser
raised `ArcscanError: unknown deployment standard` on that response.

Arcscan's [token documentation](https://docs.arc-scan.org/docs/tokens) describes
an unclassified category. `unknown` is its observed JSON encoding on this page;
it is not an absent standard. A reduced two-row fixture and the original request
cursor are documented in [the fixture notes](../tests/fixtures/README.md).

The contracts parser now validates each row's address, token object and explicit
standard declarations. Only an exact ERC-20 declaration becomes a deployment
candidate. ERC-721, ERC-1155 and the observed `unknown` value are skipped as
candidates, while all valid row addresses remain pagination keys, including the
head and previous-head stop boundary. Missing/null standards, conflicting
declarations or addresses, malformed metadata and unrecognized new enum values
still fail closed. Token names/symbols and recursively nested objects cannot
turn an unclassified row into an ERC-20 candidate.

Contracts still require `ranking.complete=true`; caps/truncation, malformed
pages, inconsistent pagination and cyclic cursors are rejected. Transfers have
no new authority. Discovery warnings now include the controlled ArcscanError
reason. Raw HTTP exception text, configured URLs/credentials and untrusted row
values are not logged; unexpected parsing exceptions log only their type.

Validation: **158 deterministic tests and 10 public network cases passed**.
The live `--once` entry point passed both from the current head and from the
previously failing saved cursor in isolated temporary databases. The saved-cursor
cycle reported `contracts=48 transfer_tokens=1`, advanced the contracts cursor,
cleared its seeded error, and assigned neither unclassified address deployment
authority. Market lookups were disabled in these discovery regression runs.
No user's runtime database, `.env` or DEX registry was modified. Existing
installations can simply update and run `launch_indexer.py --once` again; there
is no database reset or new migration.

## PR #2 bounded retries for transient Arcscan failures

`ArcscanClient._json()` now makes at most three attempts for a request, including
the first attempt. Retries apply only to timeout/connection exceptions and HTTP
429, 500, 502, 503 or 504. Other HTTP errors (including 400 and 408), malformed
JSON and error/truncated payloads fail immediately. A capped response is still
rejected by discovery/history validation; it is not reclassified as complete by
the transport layer. Exhaustion raises `ArcscanError`, never a synthetic empty
response.

The two local backoffs are 0.5 seconds and 1 second, each with uniform jitter of
0–0.25 seconds. A valid `Retry-After`, in seconds or HTTP-date form, is a minimum
delay: it can lengthen the wait beyond the local backoff. Invalid/expired values
retain the local backoff. Responses are closed before waiting. The per-attempt
request timeout remains 10 seconds. There is no new configuration or migration.

Only controlled failure categories, attempt counters, discovery page numbers and
the computed delay are logged. URLs, cursors, raw exceptions, response bodies and
raw header values are omitted. Discovery page indices start at 1 for each stream
in each cycle. For example, failure of the second page after all retries gives:

```text
Arcscan request failed category=502 page=2 attempt=3/3
contracts discovery incomplete: Arcscan request failed: category=502 attempts=3; page=2/10; saved cursor retained
```

Retries use the identical request parameters. Cursor updates remain inside the
existing transaction after full row, ranking and pagination validation. Successful
earlier pages remain committed; a failed page leaves its cursor available for the
next cycle. A successful HTTP retry followed by an invalid schema or capped page
also leaves the cursor unchanged. Neither transport retries nor successful
transfer pages grant contracts coverage or promotion authority.

Final validation: **190 deterministic tests passed; 11 public network cases
passed**. Tests cover every permitted retry category, Retry-After seconds/dates,
bounded jitter/backoff, non-retryable errors, secret-free logs and page-2 failure
after a committed page-1. Public checks run the actual `--once` entry point in
temporary databases from the current head and both saved cursors. At the latest
84-character cursor, the three-page run reported
`contracts=243 transfer_tokens=7`, with both stream errors cleared. An earlier
public run also recovered from a real timeout via the new retry path.

The first deterministic run exposed an intermittent SQLite startup lock at
`PRAGMA journal_mode=WAL` in the existing four-process test. The final complete
run, including that test, passed. SQLite startup was not changed by this focused
HTTP fix; the observed contention is recorded here rather than claimed fixed.
All runtime DB reads were read-only and all test writes used temporary databases.
The user's `.env` and DEX registry were not modified.
