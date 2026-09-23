# Targeted Top25 validation follow-up

The owner requests 25 paper-copy candidates, keeping ROI >=20%, average
operations/day <=50, dated portfolio evidence >=10 USD and median buy-to-first
FIFO-sale >=120 seconds. Richer histories remain a reporting preference (at least
10 evaluated token positions in the current Top25), not a new engine threshold.
No candidate is selected automatically. No threshold or trusted registry changed.

## 2026-09-22 progress

- The five missing exact-block PoolManager code responses were recovered. All
  five match the existing reviewed runtime hash. Six RPC HTTP attempts reserved
  600 CU ($0.0006 conservative local estimate), within the existing cumulative
  campaign budget; no Nansen request.
- Offline replay still returns `SWAP_FLOW_NOT_CONFIRMED` for all five. Obtaining
  code is not sufficient to resolve route attribution.
- One concrete route in wallet `0x0e50…de08` goes from USDC through an unreviewed
  V3 emitter, then an intermediate token, then the reviewed V4 PoolManager, then
  the purchased token. The existing cash/token graph intentionally cannot certify
  that whole route. Do not simply set confirmation_eligible to true.
- The unexplained outflows for `0xad52…12c6` and `0x7af4…c241` come from the same
  token contract and selector, in transactions sent by another address with 200
  logs each. This suggests a separate token-log investigation, not an established
  sale, malicious contract verdict, or permission to discard logs.
- Local comparison against the existing universe found 281 strict performance
  candidates with >=10 evaluated token positions; none currently has a complete
  qualifying holding proof in the inspected campaign/production histories.
  The campaign's other 50 `VERIFIED` wallets have smaller Nansen samples; they
  must not be presented as substitutes with equally rich historical evidence.

## Bounded proof tools

`copy_holding_code_proofs.py` allows up to five exact-block code proofs for failed
stored transactions of wallets already authorized for paid Top25 research. It
reserves at most 20 attempts, uses the existing cumulative campaign counters,
saves an initial SQLite backup, and writes immutable proof files and a manifest.

`copy_pool_provenance.py` derives at most 100 unique emitter probes from existing
Top25 audit issues and digest-checked cached receipts. The current plan has 85.
The worker permits up to 700 attempts / 1,200 seconds / $0.07 conservative local
reservation, always below the existing cumulative budget. It checks chain 5042,
pool factory/token0/token1/fee, the official factory's reciprocal getPool lookup,
and code at the receipt's exact block. Results are evidence only, not automatic
approval. Every request/response is persisted before subsequent work.

The official factory anchor is from the
[Uniswap deployment record for chain 5042](https://raw.githubusercontent.com/Uniswap/contracts/main/deployments/json/5042.json),
read on 2026-09-22: `0xf0db7b58379503491d857db50ac9ece64c653918`.
Factory membership does not by itself establish a complete trading path, token
semantics, or matching code at every other transaction block.

## Continuation evidence

- `outputs/holding_code_proofs_20260922/`: five code proofs, request ledger,
  pre-write backup, budget deltas and manifest.
- `outputs/copy_validation_progress_20260922/five_code_replays.json`: unchanged
  diagnostic outcomes plus stored metadata provenance.
- `outputs/copy_validation_progress_20260922/replacement_queue.json`: local
  replacement queue with holding/funds evidence and unresolved reasons.
- `outputs/pool_provenance_20260922/`: per-pool immutable observations, live
  request ledger and final summary/manifest when complete.

The first pool probe launch stopped while checking local inputs, before network
activity, because it only searched the campaign cache. The revised loader also
reads production and pilot caches, all query-only. Both original failure logs
and resumed worker logs are retained.

Next: examine the pool evidence, isolate deterministic multi-hop fixtures, and
review route attribution and unrelated-token inventory accounting with explicit
proofs. Preserve the current fail-closed result until a reviewed reconstruction
passes the unchanged holding calculation. Do not restart a complete campaign,
expand the paid wallet scope, or activate paper/live as a side effect.

## First hourly continuation

The 85 pool probes completed: 74 reciprocal memberships in the official V3
factory, 9 different factories and 2 invalid responses. Cost reservation: 45,600
CU ($0.0456 conservative), 456 attempts. All 74 also match the canonical CREATE2
address derived from their factory, ordered currencies, fee and the init-code
hash in [Uniswap PoolAddress.sol](https://raw.githubusercontent.com/Uniswap/v3-periphery/main/contracts/libraries/PoolAddress.sol).
This strengthens deployment evidence; it does not certify every historical code
response or an arbitrary multi-hop route. The 9 different factories remain
unreviewed, including the first hop of wallet `0x0e50…de08`.

Two targeted code reads for `0xc6203d86f5efbed7f9512755f3e6132ee50d03d1`
(3 attempts including chain identity) returned the same runtime hash:
`0x2825969fa042ed3875c136534af0516ab3226cbe2e1809629dae4642d5070f42`.
Its 1,667-byte runtime contains no storage reads/writes, external calls, creation
or self-destruct instructions when PUSH data is skipped. It emits logs and has
metadata selectors; Transfer topics alone do not establish ERC20 inventory
changes. The observation is diagnostic, not an automatic event deletion.

`copy_exact_code_research.py` now prepares a fixed plan from digest-checked stored
receipts of the authorized 25 wallets. There are 2,110 address/block pairs:
471 canonical pool pairs and 1,639 token pairs attached to existing inventory
flags (87 distinct token addresses). It reuses 76 prior code observations, and
collects missing exact-block evidence with a maximum of 2,500 attempts / 3,600
seconds / $0.25 conservative reservation per run, still under the unchanged
cumulative $10 campaign cap. It never enumerates new wallets or transactions.
Successful and failed observations are saved individually; a resume skips them.
No Nansen request, scoring write, trusted registry change or paper/live authority.

Results are under `outputs/exact_code_research_20260922`; completed runs include
budget deltas and SHA-256 manifests. Opcode inventories deliberately inspect all
bytecode, including metadata (which may cause false negatives), and never use
absence of instructions as an automatic token exclusion policy. Remaining work
is semantic inventory review and reviewed route reconstruction on isolated copies,
then the unchanged holding calculation and dated funds/performance checks.

## Second continuation: isolated replay (2026-09-23 local)

All 2,110 exact-block code observations are now saved. All 471 canonical pool
observations match their reviewed anchor. The collection reused 76 observations
and made 2,035 HTTP attempts (203,500 CU / $0.2035 conservative reservation).

`copy_research_replay.py` reads source histories and caches query-only and writes
a new research directory. It never writes the engine's registry or statuses.
Canonical V3 proofs are checked against their manifests, reciprocal factory
membership, CREATE2 derivation and exact-block runtime code. The existing direct
swap classifier and cash/basket guards remain in use. Quantities for FIFO timing
come from exact raw integers; a local 120-digit Decimal context prevents artificial
dust from rounding large raw balances. No historical token-decimal assumption is
needed for this timing metric. Raw units are NOT dollar values or quantities for
execution.

Unsolicited isolated credits are explained only when the complete raw snapshot
shows no disposal or trade of those tokens, no cash touch, no Swap event, zero
transaction native value and an external sender. A specific log-only emitter is
explained only with its previously reviewed exact runtime hash at the transaction
block and no storage/call instructions, plus no cash/swap and an external sender.
These are explicit research annotations with before/after records; original
events are retained. Missing raw records, unresolved routes or inventory flows
still block the whole wallet metric. Nothing is promoted from partial statistics.

A local search of request ledgers and 77 backup caches did not recover the 144
missing raw transaction/receipt responses. `copy_raw_proofs.py` filled only those
explicit gaps from transactions already in the authorized Top25 snapshot. It
checks paid scope, refuses already cached responses, permits only transaction,
receipt and chain-id reads, and caps a run at 450 attempts / 900 seconds under the
existing cumulative $10 reservation. A pre-write campaign backup, every HTTP
response and an isolated cache are retained. Actual completion: 144/144 proofs,
145 attempts including chain identity, 14,500 CU / $0.0145 reservation. Cumulative
campaign reservation: 6,740,100 CU / $6.7401. These are conservative local
reservations, not a statement of the provider invoice. No Nansen calls.

The latest isolated replay explains 1,033 flagged observations and yields:

| Wallet | Holding result | Median | First-sale samples |
|---|---|---:|---:|
| `0x58816ef4f2b24b89e2eef1951a7cdc16380f44d6` | Within limit | 292 s | 80 |
| `0xdb1f232180be0a148e53caae32182150aacc2026` | Within limit | 219 s | 81 |
| `0x821f94ea4f2655b95b5740466ea56d7f94134c14` | Too short | 31 s | 34 |
| `0x7c2c9796f89190a34ab36e712837914d5fbdf2b1` | Too short | 34 s | 32 |
| `0x7af4253a4704e6d2aeeaf3455c22b7742473c241` | Too short | 108 s | 291 |

The other 20 remain unknown. The two passing holding dossiers have stored ROI
42.60% / 55.56%, average 2.47 / 2.46 operations/day and observed funds $214.30 /
$521.24 dated 2026-09-22 00:29 / 00:31 UTC. Both are outside the infrastructure
registry. They satisfy the requested research criteria on these dated snapshots,
not a guarantee of profitability, present balance or engine qualification. Their
fast-exit proportions are still 32.5% / 37.0%; a median above two minutes does not
mean every trade is copyable. No paper/live activation; do not infer independent
identities from two similar trading histories.

Artifacts (all retained separately):
- `outputs/research_replay_20260923/`: initial replay, before filling raw gaps.
- `outputs/raw_evidence_recovery_20260923/`: missing-key list and local searches.
- `outputs/raw_proofs_top25_20260923/`: completed bounded raw proofs and backup.
- `outputs/research_replay_complete_raw_20260923/`: latest replay and manifests.
- `outputs/copy_validation_progress_20260922/new_research_qualifiers_20260923.json`:
  dated performance/funds joined with the two passing holding results.

The preservation comparison confirms all 5,342 original transaction payloads
unchanged. 129 targeted deterministic regression tests pass, including offline
network denial, source immutability, narrow inventory exceptions, exact code
requirements and bounded paid scope.

Next: investigate remaining multi-hop routes and inventory ambiguities locally.
A stored aggregator calldata sample yields a V4 PoolKey whose hash exactly matches
the emitted pool id; this may avoid a new initialization-log scan. It is only key
evidence, not yet a reviewed route decoder. V4 event deltas use negative inputs
and positive outputs, as shown by the implementation of
[Pool.swap](https://raw.githubusercontent.com/Uniswap/v4-core/main/src/libraries/Pool.sol)
and [PoolManager](https://raw.githubusercontent.com/Uniswap/v4-core/main/src/PoolManager.sol).
Hooks can change caller accounting; never replace actual wallet cash flow with
event amounts. Keep production decoding unchanged and prove full currency paths
with deterministic fixtures before accepting any additional research trades.

## Third continuation: reconciled two-hop research (2026-09-23 local)

`copy_route_research.py` now supports a deliberately narrow offline route: exactly
one canonical V3 Swap and one reviewed V4 PoolManager Swap. Both require matching
exact-block runtime evidence. V3 currencies come from the canonical factory proof;
V4 currencies require a cryptographic PoolKey hash match to the emitted pool id,
using bounded candidates from stored calldata. Actual pool Transfer deltas must
exactly equal the V3 event amounts and the negated V4 event amounts. Native/USDC
mirror representations must agree exactly and are never added together.

A directed currency graph must connect wallet input to wallet output through
both proven conversions. Unrelated swaps, unknown factories, changed code,
malformed events, missing keys, partial amounts, extra pool currencies and routes
requiring unreconciled hook/flash accounting remain fail-closed. Wallet cash/basket
classification is still the existing classifier; the new proof annotates only the
isolated research copy. No production decoder, registry, threshold or authority
changes. Real preserved Arc BUY/SELL fixtures supplement synthetic failure tests.

Latest Top25 replay: `outputs/research_replay_routes_20260923/`.
55 additional swap observations now reconcile. Two more wallets have a complete
but too-short holding result: `0xad52d654964de37a08c6056ae12481168b9e12c6`
(44 seconds, 81 first-sale samples) and
`0x75b445c7a9f8275aaeabaffbdf5abb2b588c5430` (111 seconds, 25 samples).
The batch therefore has **2 within the holding limit, 5 too short, 18 unknown**.
The two previous passing research dossiers remain unchanged. This is not a list
of 25 qualified wallets and does not activate copying.

Nine local replacement histories marked COMPLETE were replayed separately in
`outputs/research_replay_replacements_20260923/`. All nine have raw evidence but
retain inventory/route ambiguities. The other local replacement candidates have
196 DECODING and 51 FETCHING campaign headers. They are not complete dossiers.
This replacement inspection is offline only and grants no paid-wallet scope.

152 targeted deterministic tests pass. This continuation made zero RPC/Nansen
calls and consumed zero new provider credits. Original histories and earlier
reports are retained. Continue semantic inventory review, other route cases and
local replacements; never weaken the two-minute threshold merely to reach 25.

## Owner-approved threshold change (2026-09-23)

The owner subsequently explicitly requested **60 seconds instead of 120** for
the median BUY-to-first partial-sale gate. This supersedes the two-minute policy
above for future research and the hourly follow-up. ROI, operation frequency,
dated funds, infrastructure exclusions and fail-closed evidence requirements
remain unchanged. No paper/live activation is authorized by this change.

Shared research defaults, candidate reporting, recovery reporting and the example
environment now use 60 seconds. The hourly automation was updated to the same
policy. Historic reports retain their original criterion. The updated report in
`outputs/copy_validation_60s_20260923/` recomputes medians and fast-exit statistics
from preserved, digest-verified reconstructed records without any network call.
The previous 120-second outcomes were reproduced first as a consistency check.

Of the current 25, **4 satisfy the dated research criteria, 3 are too short and
18 remain ambiguous**. The newly passing medians are 108 and 111 seconds. These
are research results, not engine promotions. A median above 60 seconds is not a
minimum duration for every trade or proof that webhook processing and execution
will take less than a minute. 180 relevant deterministic tests pass, including
the inclusive 60-second boundary, explicit legacy 120-second boundary, unchanged
scores/shortlist and unresolved-history rejection. No API credits consumed.
