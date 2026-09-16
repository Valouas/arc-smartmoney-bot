# V4 audit — issue #1

Audit date: 2026-09-16. Scope: every tracked Python module, configuration/example,
watchlists, README and existing tests on `main`; no V5 features or real-money execution.
Changes are on `fix/issue-1-v4-audit`. Numerical scoring and promotion thresholds are
unchanged. Data-quality gates are deliberately stricter, as described below.

## Critical

No private-key loading, signing, swap broadcasting or real-money execution found.
`ProposalQueue` writes human-review records only; paper positions remain simulations.
Tests now disable dotenv loading. No secrets were printed or committed.

## High — fixed

- **False validation on unknown risk.** Missing market, price, liquidity, valuation,
  pair age or concentration previously supplied warnings only. They now block
  validation. The concentration parser uses explicit units and the live
  `summary.top10_share` / complete `census` shape, not recursive substring guesses
  that could confuse holder counts with percentages.
- **Wrong market identity.** DexScreener fallback could select another chain or a
  pair where the requested token was the quote, while using the base token's price.
  Both response paths now require the configured chain and requested base token;
  non-finite/negative numeric quotes become unknown.
- **USDC reconstruction and false buys.** The native mirror emitter
  `0xfffffffffffffffffffffffffffffffffffffffe` was treated as a separate token;
  gross `msg.value` was added to ERC-20 spending. Native net transfers/refunds and
  native proceeds now use 18 decimals; ERC-20 uses 6; matching mirrors are counted
  once and conflicting representations are rejected. Live receipt fixtures cover
  an actual router/V4-pool purchase. V2/V3/V4 swap events plus token-transfer
  connectivity are required for confirmation eligibility. Plain transfers,
  airdrops, failed receipts and NFT logs cannot provide a verified purchase.
  Basket/LP flows have no invented per-token allocation. Unknown decimals raise
  an explicit error instead of silently assuming 18. Gross-value-only observations
  remain research-only; strict historical reconstruction rejects ambiguity.
- **Incomplete histories promoted as complete.** API failures no longer become
  empty successful responses. Discovery checks the live `page.coverage` schema,
  index gaps, page exhaustion, cursor loops and caps. Early ranking uses block/log
  order, not hash order. Missing receipt data keeps the winner unprocessed and the
  candidate unhydrated. Promotion requires verified history, sufficient evaluable
  trades and verified winners. Unrecognized coverage fails closed.
- **Quarantine and stale authority.** Scanner eligibility checks candidate status,
  verified history and the enabled promotion record, in addition to identity and
  trade evidence. Buffered confirmations are re-evaluated after watchlist changes;
  an ineligible observation cannot trigger `VALIDATED`. Legacy promotions require
  revalidation. Unverified old observations cannot train new profiles.
- **Lost scanner transactions.** Receipt failures previously advanced the scan;
  transactions were marked seen before event handling. Failures now retry the block,
  event writes are replay-safe for the single scanner, and completed events and
  next-block checkpoints persist. Legacy transaction hashes without `0x` remain
  recognizable. This does not claim exactly-once external notification delivery.

## Medium — fixed

- **Arcscan integration:** `standard=ERC-20` returned HTTP 400; normalized `erc20`
  succeeds. Live contract `creation_block` is parsed. NFT standards and mismatched
  token rows are excluded. Live nested address/funder objects have fixture tests.
- **Intelligence distortion:** candidate research/live overlap no longer doubles
  stake; profiles use observed execution ratios instead of later spot quotes;
  equal-timestamp snapshots are not future outcomes. Winner-only HistoryLab runs
  no longer increase live reputation on every rerun or overwrite its file.
- **Identity:** direct wallet-to-wallet funding joins related actors; missing or
  incomplete funding data supplies no eligible confirmation. Explicitly labelled
  exchange/bridge/faucet/custodian funding does not merge unrelated users and stays
  unverified. Refresh rebuilds unions rather than retaining obsolete links.
- **SQLite:** removed import-time class monkey-patching. Additive migrations use
  `BEGIN IMMEDIATE`, WAL and a 10-second busy timeout. Source counts increment only
  when the unique source insert succeeds. A four-process legacy migration/write
  regression checks the race. Existing observations are preserved; verification
  flags default false until reconstructed.
- **Launch observations:** least-recently-checked candidates get priority; snapshot
  scheduling no longer ignores all but the newest 5,000 launches. Missing prices
  or a different pair do not permanently occupy a snapshot slot. Explorer discovery
  outages do not prevent observations of existing launches. Actual observation
  times and delays remain stored, including late observations.
- **File/paper state:** JSON snapshots publish by atomic replacement; watchlist
  weight/label edits are detected even with unchanged addresses. Paper transitions
  report an action only when the database actually changed; invalid prices cannot
  create a synthetic stop exit.

## Low — fixed / operational

- The existing native-USDC test truncated a hash by assuming `.hex()` includes `0x`
  under Web3 7. Topics now accept bytes and prefixed/unprefixed strings.
- Added a development requirements file, isolated opt-in network tests, captured
  public fixtures and deterministic regressions. Tests disable dotenv loading.
- Telegram failures are still best-effort and silent. JSONL proposals may repeat
  for successive validated observations. Neither path executes an order.

## Live validation evidence

Public GETs and `eth_chainId`/transaction reads only, on 2026-09-16. See
`tests/fixtures/README.md` and `tests/test_network.py` for reproduction.

| Surface | Observed result |
| --- | --- |
| RPC `eth_chainId` | `0x13b2` = 5042 |
| `/v1/chain` | mainnet 5042; native 18 decimals; ERC-20 address `0x3600…`, 6 decimals |
| `/address/{addr}` | HTTP 200, nested address/creation fields |
| `/address/{addr}/facts` | HTTP 200; both explicitly unavailable and complete nested funder examples |
| `/address/{addr}/activity?limit=1` | HTTP 200; `items`, `page.coverage`, `has_more`, `next` |
| `/explore/contracts?limit=1&filter=token` | HTTP 200; nested token/creator, `creation_block`, cursor |
| `/explore/token-transfers?limit=1&standard=erc20` | HTTP 200; previous `ERC-20` request reproducibly HTTP 400 |
| `/tokens/{addr}/transfers?limit=1` | HTTP 200; native token example explicitly `capped: true` |
| `/tokens/{addr}/concentration` | ARGUS HTTP 200, top10 share 24.5811%; native USDC HTTP 501 (no separate holder list) |

Intermittent 503s/timeouts occurred. Seven endpoint integration cases passed on
first execution; the chain-identity case timed out, then passed on a targeted retry.
This is evidence of availability instability, not an empty market or empty history.
The live contract and response tests cover every path used by `ArcscanClient`, but
not every optional cursor/kind/token combination or every asset.

Documentation checked: [Arcscan API](https://docs.arc-scan.org/apis),
[Arcscan network](https://arc-scan.org/docs), and
[Arc USDC interfaces](https://docs.arc.io/arc/concepts/stablecoin-native-model).
The documentation's unpublished-API notice and several capability statements lag
behind the successful live responses. Do not treat provider documentation alone
as availability or coverage proof.

## Residual limitations and assumptions requiring more live validation

**Historical first-pass status:** the list below records what remained after the
initial audit commit. The subsequent [follow-up corrections](AUDIT_V4_REMEDIATION.md)
implement cursor persistence, forward scoring, deployment trust checks, stronger
identity handling, transactional recovery and paper costs/fills. That document is
the current implementation status; scientific and external-data limits remain
explicit there.

1. **High: statistical claims remain unproven.** Winner-seeded discovery conditions
   on success. The launch universe is restricted to observed explorer pages and
   discoverable/liquid DexScreener base pairs; deployments during outages/bursts can
   be missed. Active hours mean observed trade hours, not all wallet activity;
   clamping the opportunity denominator to purchases does not remove selection
   bias. Max-future sampled returns are not realized PnL, and missing loser quotes
   are not evidence of success. Scores are research heuristics, not an estimated
   probability or demonstrated trading edge.
2. **High: reconstruction is intentionally incomplete.** A genesis-contiguous
   index does not prove every economic action is exposed by address activity.
   Token-to-token swaps, custom DEX ABIs, account-abstraction/relayer transactions,
   incomplete native mirrors and cross-wallet settlement require more fixtures or
   traces. These can produce false negatives. Swap topics/connectivity cannot
   prove a pool is trustworthy: malicious tokens/contracts can emit plausible
   events. Validate deployments/DEX provenance before treating signals as advice.
3. **Medium: Sybil coverage is partial.** A first funder is only one relation.
   Unlabelled exchanges can over-merge; actors funded through different paths can
   evade grouping. Distinct source tokens do not prove independent deployers or
   outcomes. The promotion gate uses verified winner counts but does not assert
   statistical independence among them.
4. **Medium: observations and persistence.** Late snapshots keep actual delays and
   are not historical quotes. Snapshot attempts rotate so unavailable tokens cannot monopolize a batch. One live scanner is supported, as in `run_v4.py`;
   multiple scanner instances do not have a distributed ownership lock. A crash
   between an alert/proposal and its completion marker can replay the message;
   an outbox would be needed for stronger delivery semantics. In-memory clusters
   are not restored on restart. Identity facts refresh on membership changes; unknown facts are retried at the
   watchlist reload interval. Serial explorer calls can delay scanning for large lists.
5. **Paper realism:** no slippage, fees, price impact or partial fills; one lifetime
   position per token remains the existing policy. Invalid quotes are rejected,
   but this is not an executable backtest. Legacy historical values are retained
   for inspection, not retrospectively corrected or certified.

Suggested future changes, **not implemented as threshold adjustments**: persist
full launch cursors/coverage intervals; assess a matched forward-only opportunity
universe and fixed outcome windows; track non-evaluable losses and maturity;
calibrate thresholds only on held-out forward data; validate pool registries,
funding labels and additional transaction shapes; add alert outbox and identity
refresh/backoff. No numerical threshold was silently retuned.

## Validation / migration

- Baseline: 12 passing, 1 failing test (truncated topic fixture) with Python 3.10 /
  Web3 7.16.0.
- Initial audit deterministic suite: **56 passed**; follow-up: **103 passed**,
  including the four-process SQLite test; 8 network cases skipped by default.
- Follow-up public integration run: **8 passed** in one run, separately from the
  deterministic suite. Compilation of 49 Python files and `git diff --check` pass.
- Run offline: `python -m pip install -r requirements-dev.txt`, then
  `python -m pytest -m "not network" -q`.
- Run public integration separately: set `ARC_RUN_NETWORK=1`, then
  `python -m pytest -m network -v`. No tokens or secrets are required.
- On first start, migration version 1 preserves existing data, marks legacy trade
  evidence unverified, disables old auto-promotions and returns their candidates
  to quarantine. Reconstruction must succeed before they can confirm again.
  Back up the SQLite database before applying to a running research installation;
  stop all four processes for deployment. New-schema concurrent startup is tested.
  Follow-up schema version 2 also resets old promotions for forward evidence;
  consult the [migration and trust requirements](AUDIT_V4_REMEDIATION.md).
