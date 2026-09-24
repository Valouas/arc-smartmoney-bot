# Synthra launchpad: isolated holding research

Five stored buys at Arc block **21189850** use the Synthra LaunchPad rather
than a V3 pool. Its `Trade` event is absent from the engine's generic swap
topics. This patch explains those receipts in offline research only. It does
not change the production decoder, trusted registry, scoring or selection.

## Evidence

- Official deployment/semantics: https://docs.synthra.org/docs/contract-addresses
  and https://docs.synthra.org/docs/launchpad-integration.
- The saved documentation bundle is https://docs.synthra.org/assets/index-jIUJ8fq1.js,
  SHA256 `2627917875e2dd0aa75bdde9af1758a0140ae43b331f13ea573b2936a2ffc9b8`.
- Official app ABI for `buy` and `getCurve`:
  https://app.synthra.org/static/js/main.e5e9b8d8.js,
  SHA256 `1b6f2663e46c6de3e4c9b8dc13ed2d758c8bf5202cb3e793ad9d0cf287b212e3`.
- Exact historical code at `0x18d33de5eefb2f91b09385f35f6a1317659cc1f9`,
  block21189850, keccak
  `0x766cc2f05d1be671f29f3f0eb7c6cb5b1cb47bba123cb47e2583b789b71b9400`.
  Exact getters confirm nativeScale=1e12 and wrappedNative=zero on Arc.

The docs describe the Trade/FeeSplit fields; the app exposes uint128 curve
reserves and buyback pot. The typed signatures inferred from these definitions
match the observed event topic hashes exactly:

```
Trade(address,address,bool,uint256,uint256,uint256,uint128,uint128,uint128,uint128)
FeeSplit(address,uint256,uint256,uint256,uint128)
```

This is not a claim that the app publishes those two complete event ABIs or
that the contract has been independently security-audited. The runtime and
block are pinned; deployment identity alone never confirms a transaction.

## Strict supported form

Only an ERC20-funded `buy(address,uint256,uint256,uint256)` from the researched
wallet to that exact deployment/block is handled. Native-value buys, sells,
graduation, multiple trades, additional events or transfers remain unsupported.

Require exactly three transfers (USDC and its native mirror wallet→launchpad,
token launchpad→wallet), one Trade, one FeeSplit. Input, trader/token topics,
amounts, minimum output, deadline, fee split and complete wallet deltas must
reconcile. Native mirror and USDC must agree at 1e12 scaling and are never
summed. Event booleans, uint128 bounds, log identity/order, receipt status and
raw snapshot consistency are checked. Changed or missing code fails closed.

Research outputs preserve the original error and before/after explanation.
They add the observed BUY at its original timestamp, never an invented profit
or promotion. Unrelated inventory gaps remain unresolved. The five buys each
have a subsequent confirmed sale of that token **29 seconds later**; this is
a per-transaction observation, not a complete wallet median or a copy-latency
guarantee. Their full dossiers still require other evidence.

The bounded original-Top25 probe used four RPC reads, reserving **$0.0004**;
cumulative reservation **$6.7433 of $10**. No Nansen calls. Reports/replay are
offline. All 5,342 original payloads remain equal to source SQLite. Tests cover
all five real receipts plus altered flow, code, block, identity, log, event,
fee and input failures. No paper/live authority is granted.
