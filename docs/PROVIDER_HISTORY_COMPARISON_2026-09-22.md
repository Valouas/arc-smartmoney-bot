# Comparison of Nansen DEX trades, Dune and stored Arc reconstruction

## Result

The explicit four-wallet pilot supports using Nansen DEX trades and Dune for
research collection. It does **not** establish that either provider can replace
the complete reconstruction or confer copy/paper authority on a wallet.
No production collector, ranking, checkpoint or promotion rule changed.
The existing collection continues under its existing budgets.

| Wallet | Local confirmed trades | Nansen rows | Matched direction/token/amounts | Exact timestamps |
| --- | ---: | ---: | ---: | ---: |
| `0x1f1f1c9f75fc95caf63587d28fe533bd94d7ea7e` | 4 | 4 | 4 | 4 |
| `0xddd589a33989c8c76f8cd3de1ab5392f592abed4` | 7 | 7 | 7 | 7 |
| `0x14a1a1e1cc6bb1d78c1447677df2083f92140078` | 5 | 5 | 5 | 5 |
| `0x06b98521ba2cfe389a4ffbd48279ee9f0752bfb8` | 119 | 123 | 119 | 119 |
| Total | 135 | 139 | 135 | 135 |

The sample deliberately includes two wallets with verified holding duration,
one excluded for short holding duration, and one active wallet requiring review.
It is small and selected, not a random chain-wide accuracy estimate.

The reference snapshot contains 173 locally decoded transactions. All four
Nansen pages report `is_last_page=true`; there are no identical duplicate rows.
Amounts are compared with tolerance `max(0.000001, abs(reference)*0.000001)`
to accommodate already-stored floating point numbers, not raw integer equality.
All 135 timestamps have explicit UTC and exactly match the local timestamp.
Native USDC mirror `0xffff…fffe` and ERC20 USDC `0x3600…0000` are recognized
as the two already-known Arc cash representations. Arbitrary tokens are not
treated as USD; provider `trade_value_usd` is not substituted for settled USDC.

Four extra Nansen hashes belong to transactions already present locally with
`unsupported_or_ambiguous_reconstruction`. Their errors remain intact; the
provider description is additional evidence, not permission to bypass decoding:

- `0x125184956008cb89519e0a42327c88e8814bac80e7aac9a4c64460843985cd16`
- `0x248e5be9def6268d61d0e60c0abdc198942f790425242d4132dec4f02a4869c0`
- `0x27e17dd914c5b06231cda320431bed456a574e66a2478931505bb936c041666a`
- `0x6e8266c52b1f5282c1a6e9bd93e5b99ea44643d737c74e032825a83ea9d71c3a`

## Dune comparison and measured usage

One query selected the exact sampled hashes, without winner/profit filters,
from `erc20_arc.evt_transfer` and `uniswap_v4_arc.poolmanager_evt_swap` for
the reviewed PoolManager. It returned 2,410 rows. Raw receipts were available
in the isolated RPC cache for 24 of the reference transactions:

- 755/755 local transfer/swap events matched by hash, log index and emitter;
  transfers also matched exact integer amount, sender and recipient.
- No missing or extra events within those cached receipts.
- 1,655 Dune rows have no raw receipt in this selected cache: **not independently
  verified**, even though decoded local transaction summaries exist.
- No block-number, transaction-index or timestamp mismatch against the local
  summaries across returned rows.
- The query only compares the existence/index of reviewed swap events, not all
  swap arguments or the complete contract call graph.

The first Dune `small` submission was rejected HTTP 400 (tier not available).
That refusal was archived. One subsequent `medium` execution succeeded:
`01M34WRZ660C34TPYNWD1F0SMR`, execution time 58.906 seconds,
execution cost **2.578970589 Dune credits**. Account usage rose from
1,139.512 to 1,142.091 of 2,500 included credits. No subscription change or
credit purchase occurred. Dune's API has no documented per-query credit cap;
the pilot checks an included-credit reserve before submission, submits once,
and requests cancellation after bounded polling. This is not a hard billing cap.

Nansen: **4 HTTP requests, 4 credits confirmed by billing headers**. They share
the previously authorized 2,000-attempt supplement budget, not a new campaign.
Policy counters after the pilot: 1,079 attempts reserved, 1,077 confirmed credits
used; two earlier uncertain transport attempts remain reserved. The DEX responses
are kept in a separate `research_attempts` table, not the transactions seed pages.

Alchemy/RPC requests by this pilot: **0**. All RPC reference data was read from
disk. This does not imply the other running collectors stopped using Alchemy.

## What can and cannot be concluded

Nansen retrieves hundreds of useful trades with one call; Dune can retrieve
many timestamped transfer/swap events together. These are credible alternatives
to repeated per-transaction RPC reads for a **research** ledger.

Before changing the authoritative path, a separate adapter still needs to prove
full wallet-flow coverage, pagination, starting inventory, deposit/withdrawal
handling, native-flow/gas handling, ambiguous transactions, and ordering when
timestamps tie. Nansen DEX rows lack raw receipts/log indexes and do not describe
every non-swap transfer. Published Arc coverage begins 2026-07-15; earlier
inventory cannot be assumed absent. Dune's sampled transfer tables include
native-mirror events, but this is not proof of all native calls or complete gas
accounting. Other DEX emitters were not tested.

Therefore keep RPC for unresolved evidence and retain fail-closed status.
Do not claim a chain-wide completeness rate, a guaranteed monetary saving, or
a new completion ETA from this four-wallet experiment. Stored Nansen PnL and
existing reconstruction are also not assumed infallible.

## Reproduce without consuming credits

From the repository directory, PowerShell:

```powershell
.\.venv\Scripts\python.exe provider_history_comparison.py report `
  --directory data/provider_comparison_20260922 `
  --dune-directory data/provider_comparison_20260922/dune_medium
```

`prepare` and `report` are offline. Network actions are separate explicit
`nansen` / `dune` commands. There is no integration into `run_v4.py`, no bulk scan,
no retry of an uncertain paid request, no automatic purchase and no live authority.
Nansen reservations use the existing campaign lock and an atomic shared cap.
Crash/unknown billing leaves a halt until explicit operator review; raw bodies,
including partial bodies, are retained. Re-running a captured request does not
submit it again. Reports append new files rather than overwrite evidence.

Evidence directory: `data/provider_comparison_20260922/`.
Verified second copy:
`data/evidence_archives/provider_comparison_20260922_1790092214923835600/`.
Its SHA-256 manifest covers 98 files, including the refused Dune submission,
successful responses, local snapshot and intermediate comparisons. Early reports
pre-date explicit native-USDC mapping and must not be mistaken for final findings.
Final comparison for this pilot: `comparison_1790092116633629900.json`.

Official references checked on 2026-09-22:
- https://docs.nansen.ai/api/profiler/address-dex-trades
- https://docs.nansen.ai/api/data-coverage
- https://docs.nansen.ai/getting-started/credits
- https://docs.dune.com/api-reference/executions/endpoint/execute-sql

Validation: 276 deterministic tests (pilot, Nansen collection, holding analysis,
candidate/recovery reports and RPC transport); live four-wallet comparison above.
