# Offline V2 research: one reviewed pair, two blocks

The remaining two decode errors for wallet
`0x007ca0b2215f8dceb441db3406156e861dda22ec` came from a Uniswap V2 pair.
The earlier V3-only provenance probe correctly refused its different factory.

Official anchors, retrieved and preserved on 23 September 2026:

- [Arc deployments](https://raw.githubusercontent.com/Uniswap/contracts/main/deployments/json/5042.json): V2 factory `0x89e5db8b5aa49aa85ac63f691524311aeb649eba`.
- [V2 SDK constants](https://raw.githubusercontent.com/Uniswap/v2-sdk/main/src/constants.ts): pair creation hash `0x96e8ac4277198ff8b6f785478aa9a39f403cb768dd02cbee326c3e7da348845f`.
- [Factory source](https://raw.githubusercontent.com/Uniswap/v2-core/master/contracts/UniswapV2Factory.sol): pair CREATE2 salt is the hash of packed sorted token addresses.

At blocks 21196066 and 21197159, the pair reports the official factory, sorted
USDC/token addresses, and the factory returns that same pair from `getPair`.
Its address independently matches CREATE2. Pair and factory runtime hashes
are pinned in `copy_v2_research.py`; only the two reviewed blocks are accepted.
The source snapshots and all 13 free public RPC responses are retained under
`outputs/v2_research_007ca_20260923/proofs/` with SHA-256 manifest.

`copy_research_replay.py --v2-proof` can load this evidence offline. The existing
swap classifier still requires wallet cash/token flow connected to the trusted
swap emitter. Production `trusted_dex.json` is never modified: registration is
in memory in the research process. The fixture includes both real stored
receipts, and tests deny network access and reject altered provenance or blocks.

Replaying the 32 stored transactions resolves the two remaining errors. The
wallet has 11 observed buy-to-first-sale intervals, median 178 seconds, with
3 below 40 seconds. Together with the previously stored ROI/activity metrics
and dated funds observation, it satisfies the current research criteria.
This neither selects it for paper nor grants live authority. The 25-wallet
validation and wider analysis are still incomplete.
