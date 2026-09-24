# Exact DYOR V2 evidence for offline holding research

Two unresolved swaps for `0x01346f1bcf18a8f8797af3465275a1e2d1d62274`
used pool `0xcf924acee7eb1f169a922bf19b0a732810971985` at blocks
21148353 and 21173493. Applying the Uniswap V2 init hash does **not** derive
this pool; that unsuccessful diagnostic remains archived.

The public DYORSwap frontend supplied the missing deployment provenance:

- https://dyorswap.finance/
- https://dyorswap.finance/_next/static/chunks/pages/_app-ce00f340c523f9fe.js
- Archived bundle SHA-256:
  `629250bc9bc93c53ca3da3384cbf29ddd2347f34a976040b567abec270e32110`.

The bundle maps `ARC=5042` to factory
`0x942bd5bfdc5317c5507e326f8eb4bb6058ab5c10` and V2 init hash
`0x92ddb604a61717111df354b619e8fb1a1440b9f3dbfcbdeb95792375de4c4257`.
CREATE2 with the sorted token pair derives the observed pool exactly.
Historical public RPC reads independently verify reciprocal factory membership,
token0/token1, and both runtime hashes at each block:

- Pool: `0x9d4b521d72caceb24f2ceb056b66f89b5bebc70bf8f765ec830b099872bbc873`
- Factory: `0x5e4d0bba019e88c06f4d3b09c66febd6226321cad381ae2670da5febefeb1e09`

`copy_v2_research.py` pins these two exact blocks, addresses, hashes and
CREATE2 evidence. It does not infer trust from arbitrary factory getters,
contract names or identical addresses on other chains. Production trust,
scoring, selection and live execution are unchanged. Existing strict receipt
classification still checks the swap and wallet cash flows. No network access
is needed to replay the deterministic fixtures.

Evidence is append-only under `outputs/v2_unknown_0157_20260924/`, including
13 free public RPC responses, three public website downloads, original and
reviewed proofs, and the offline replay. Source SQLite payloads remain intact.
The replay resolves the last two errors: median 863 seconds on seven first-sale
observations, including one exit below 40 seconds and three unsold purchases.
This supports the requested historical research criterion only; it does not
establish execution latency or future copy profitability. Dated observed funds
include token valuations and are not guaranteed liquidation proceeds.
