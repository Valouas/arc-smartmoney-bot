# Synthra historical pool evidence — 23 September 2026

This change only extends the isolated, offline holding-duration replay. It does
not edit the production trusted-emitter registry, promote wallets, create paper
selection or authorize live signals. All campaign histories remain unchanged.

The official Synthra documentation's contract-addresses section identifies the
Arc mainnet (5042) factory `0x6307fc239c7964942c1bffe51930e55606619c74`
and pool init-code hash
`0xd57f04bba45e2c271c16c370a90dc3bbfa3e4f5c9ffff0981b979a7bcdeac7c3`.
The rendered text fetch incorrectly returns the overview for this client-side
page; the actual public documentation bundle contains the chain-specific table.

- Page: https://docs.synthra.org/docs/contract-addresses
- Bundle: https://docs.synthra.org/assets/index-jIUJ8fq1.js
- Bundle SHA256: `2627917875e2dd0aa75bdde9af1758a0140ae43b331f13ea573b2936a2ffc9b8`
- Independent integrator reference: https://docs.achswap.app/technical/contract-addresses/

Only these exact historical pool/block pairs are admitted in research:

| Pool | Block | Evidence wallet |
|---|---:|---|
| `0xcee68e8e409b182388ef7a24b968c098f9642ad8` | 21319266 | `0x1074f62b6f8bc659b7dc4d34c0cffdfc7ac6c9a5` |
| `0xcee68e8e409b182388ef7a24b968c098f9642ad8` | 21319289 | same |
| `0xee765f55b122d7ae01cdc3dab164512245405120` | 21189907 | `0x2f08781f9a864e90787f1b915dfce89eb151e28d` |

Each proof includes exact-block pool factory/token/fee getters, reciprocal
factory `getPool`, CREATE2 derivation with the official init hash, and nonempty
runtime bytecode pinned to its reviewed hash. Identical code at an unproved
block does not suffice. These are provenance checks, not a security audit of
Synthra. Incomplete, conflicting, malformed or changed evidence fails closed.

The existing strict classifier still determines whether a receipt confirms a
swap. The new proofs resolve direct Synthra V3 swaps; they do not pretend that
every transfer is a buy, approve unreviewed launchpad events, or relax unrelated
inventory ambiguities. In particular, the 1074 wallet also received a large
third-party PCH token credit before selling it: proving its two Synthra swaps
does not resolve that separate inventory/cost-basis issue.

The bounded collector used **19 HTTP reads / 1,900 CU / $0.0019 reservation**,
inside the previously authorized Top25 and cumulative $10 cap. The cumulative
reservation is now **$6.7429**. These are conservative reservations, not an
Alchemy invoice. No new Nansen calls or credits; no broad scan or refresh.

Public raw proofs, transaction fixtures, source documents, pre-read SQLite
snapshot and append-only response ledger are retained. The offline replay
verifies the proof artifact digest before using it and writes a separate new
report with original records and explanations; it never overwrites history.

Tests cover real stored BUY/SELL receipts without network, unchanged source
registry/evidence, wrong block/factory/membership/CREATE2/code rejection, and
unreviewed pools. Research scope never adds engine authority.
