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
