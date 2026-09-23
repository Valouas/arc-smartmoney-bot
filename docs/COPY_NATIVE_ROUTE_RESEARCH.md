# One native-USDC input reconciled offline

Wallet `0x06b98521ba2cfe389a4ffbd48279ee9f0752bfb8` had one remaining BUY
ambiguity after exact-block pool and log-only-token proofs were collected.
The zero-native-value restriction in the isolated two-hop route researcher
prevented confirmation of its 90-USDC native input.

`copy_native_route_research.py` permits only the reviewed transaction
`0x27e17dd914c5b06231cda320431bed456a574e66a2478931505bb936c041666a`
in block 22083268, with exact sender, router, input, complete receipt logs and
native value. Its receipt shows 90 native USDC sent to the router, 0.9 USDC
router fee and 89.1 USDC reaching the Slipstream pool. The pool payment has
matching native and six-decimal ERC20 USDC logs; they are not summed.
The intermediate EURC and output-token fee remain in the full receipt.

This pinned input proof alone is insufficient: `copy_route_research` still
requires exact historical code, factory/CREATE2 provenance, both swap-event
amounts matching pool Transfer deltas, and a connected wallet path through
both conversions. Gross purchase cost remains 90 USDC, including the fee.
Every other nonzero-native route remains refused. Production classification,
trust registry, scoring and authority are unchanged.

The raw fixture and original 150 source transactions are preserved. Offline
replay now finds 74 buy-to-first-sale observations with median 181.5 seconds;
10 (13.51%) exit below 40 seconds. This satisfies the current research gate,
alongside previously stored ROI/activity and dated funds. It is not proof of
copy profitability and does not activate paper or live trading.

Tests reject altered value, sender, router, transaction hash, block, input,
logs, fee, receipt identity, token, side, pool code and factory evidence.
The analysis uses saved payloads only; no API or RPC call is necessary.
