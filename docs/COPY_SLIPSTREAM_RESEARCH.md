# Exact-block Aero Slipstream research

An original Top25 wallet had a BUY routed through Aero Slipstream EURC/USDC,
then the reviewed Uniswap V4 PoolManager. Its receipt used the V3 Swap ABI but
the pool was not deployed by the canonical Uniswap V3 factory. Treating every
V3-topic emitter as canonical would be incorrect.

The isolated replay accepts an optional `--slipstream-proof result.json`.
It verifies the adjacent SHA-256 manifest and pinned historical provenance:

- Arc factory `0xb89df768af2cfe637ceb352c587fe8edaf491d03`, identified in the
  [official app bundle](https://app.aero.xyz/assets/index-DyVaaWzS.js), SHA-256
  `6f6e82c370728aa4d695cf2fcccf4723b958b630263d54f4e36e8e743414adac`.
  The bundle defines `TD` as chain 5042 and uses `TD.id` for this factory.
- Pool `0xbe080ac37ad1305dfcc9521f5e6f68cfdc41b7fa` at block 21170072:
  factory, ordered tokens, tick spacing and reciprocal `getPool` agree.
- Its runtime is an exact EIP-1167 clone. CREATE2 using the factory, its
  implementation and the ABI-encoded token0/token1/int24 spacing salt reproduces
  its address. This follows the official
  [CLFactory creation algorithm](https://github.com/dromos-labs/metadex-slipstream-public/blob/contest/contracts/core/CLFactory.sol).
- Factory and implementation runtime hashes are pinned in
  `copy_slipstream_research.py`. The implementation is
  `0x0df9fc8730eec048e9832b31b4bff4351158e19b`. Clone bytes alone are insufficient:
  the actual implementation code must also have been saved at that block.

This is deployment and historical runtime evidence, not a claim to have
independently audited or reproduced a compiler build of the whole protocol.
The route must also reconcile exact pool Transfer deltas with both Swap events,
recover the V4 PoolKey by hash, and connect wallet cash to wallet token output.
Changed implementations, unknown factories, wrong blocks, malformed evidence
and unreconciled amounts remain fail-closed.

The research registry exists in memory only. Production `trusted_dex.json`,
historical source records, scoring, paper selection and live authority are not
modified. All source transactions and before/after research annotations remain
preserved in separate reports.

The resolved wallet `0x0e50d5f5c0ca47871d073cf4385b60ebb955de08` does **not** pass
the owner's 60-second holding criterion: its 19 observed first-sale durations
have median 21 seconds, with 16 under 60 seconds. Resolving a reconstruction
ambiguity therefore does not automatically qualify a wallet.

The bounded historical probe used nine HTTP attempts, reserving 900 CU
($0.0009 under the campaign's conservative reservation), inside the existing
Top25 scope and cumulative $10 ceiling. A missing V4 runtime response was
recovered from a previously saved ledger with no additional request.
