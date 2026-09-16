# Public fixtures

Captured 2026-09-16 from `https://api.arc-scan.org/v1` and
`https://rpc.arc-scan.org`, without credentials. Files contain public blockchain
addresses and transactions, not account secrets. They are offline regression
fixtures, not assertions about current market values. The test suite never
fetches them implicitly.

- `arcscan_live_0.json`: `/address/0x3600000000000000000000000000000000000000/facts`, explicitly unavailable.
- `arcscan_live_1.json`: same address `/activity?limit=1`, complete index coverage.
- `arcscan_live_2.json`: `/tokens/0x3600000000000000000000000000000000000000/transfers?limit=1`, capped history.
- `arcscan_live_3.json`: `/explore/contracts?limit=1&filter=token`.
- `arcscan_extra_0.json`: `/tokens/0xece5ca8bf9220718e5727754026757512212cb3c/concentration`.
- `arcscan_extra_1.json`: `/explore/token-transfers?limit=1&standard=erc20`.
- `arcscan_transfers_supplemental.json`: `/explore/token-transfers?limit=3&standard=erc20`,
  captured for PR #2 comment 5700673258. Valid rows and a next-page cursor coexist
  with `ranking.complete=false`; this supplemental stream does not certify the
  deployment universe.
- `arcscan_extra_2.json`: `/address/0xa6d09bfa73dcc201b6e9b30ca9cabfb59abbf26c/facts`.
- `arc_rpc_swap.json`: `eth_getTransactionReceipt` and `eth_getTransactionByHash`
  for `0x906048c7168a6ffcc1188f6776b034b1775aa0f90d17e37a2a2664d01b258208`.

No captured response is interpreted as an instruction. Synthetic variants in the
tests exercise caps, missing fields, errors, cycles, malformed units and failures.
