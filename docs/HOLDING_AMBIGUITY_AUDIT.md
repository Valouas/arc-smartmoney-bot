# Offline holding ambiguity audit

`copy_ambiguity_audit.py` diagnoses an explicit list of at most 25 wallets using
stored campaign payloads, digest-checked raw RPC caches and saved response
ledgers. It opens every SQLite input read-only, performs no HTTP, preserves
existing reports and never updates holding eligibility, scoring, the trusted
registry or paper/live selection. Missing evidence remains explicit.

```powershell
.\.venv\Scripts\python.exe copy_ambiguity_audit.py --campaign data/copy_holding_campaign_20260922 --wallets data/copy_holding_campaign_20260922/priority_top25_20260922.json --cache data/rpc_cache.sqlite3 --cache outputs/holding_pilot_mainnet_20260922/rpc_cache.sqlite3 --cache outputs/holding_pilot_live_20260922/rpc_cache.sqlite3 --ledger outputs/holding_pilot_mainnet_20260922/requests.jsonl --ledger outputs/holding_pilot_live_20260922/requests.jsonl --ledger outputs/holding_pilot_public_diagnostics_20260922/requests.jsonl --output-dir outputs/new_ambiguity_audit
```

Use a new output directory. JSON contains per-transaction causes, token deltas,
emitter addresses, provenance for saved metadata/code responses and source
coverage limitations. HTML groups these causes per wallet. A SHA-256 manifest
accompanies the report. Inspecting an error is not the same as resolving it.

## Diagnostic boundaries

- A Swap event topic alone never establishes DEX trust. An unlisted emitter
  remains untrusted; no blanket pool/contract approval is introduced.
- Reviewed emitter code is replayed only from a saved response at the exact
  numeric block. Latest-state code, missing code and conflicting code cannot
  substitute for this evidence.
- Saved decimals must be present and consistent. Even consistent observations
  do not establish their historical value at every trade: replay is diagnostic,
  not a replacement for a production reconstruction.
- Incoming-only credits with no observed disposal are identified only when the
  stored generation is complete and raw transaction/receipt coverage is complete.
  They are not called spam, discarded or used to manufacture BUY events.
- A replay that differs from the stored error is flagged for review. The
  original transaction is never overwritten and no duration is inferred.

## Initial 2026-09-22 findings

All 5,342 transactions were collected; 2,342 have existing reconstruction flags:
668 decoder errors and 1,674 inventory gaps. The first complete offline pass
classified these flags as follows (counts are wallet-transaction observations):

| Cause | Count |
| --- | ---: |
| No reviewed swap emitter | 588 |
| Swap fund flow not confirmed | 66 |
| Swap without observable USDC basis | 9 |
| Exact historical code absent from available ledgers | 5 |
| Incoming token requiring inventory reconciliation | 1,077 |
| Incoming-only, no observed disposal in this history | 569 |
| Outgoing token without a confirmed sale | 28 |

The two heaviest dossiers (`0x0a2b…44ef`, `0x0fdf…bad8`) account for
1,520 of the 1,674 inventory gaps. Raw coverage is complete for 19 wallets;
the other six still have stored decoded payloads but some raw records are absent
from the inspected caches. None of the flagged transactions lacks both required
raw transaction/receipt inputs in this pass.

The 569 incoming-only credits can be separated descriptively from confirmed
trades. Automatically ignoring them for wallet eligibility would change the
current whole-history gate and requires a separate reviewed accounting change.
The current audit does not make that change.

## Smallest dossiers to review first

| Wallet | Flags | Work remaining |
| --- | ---: | --- |
| `0x0e50d5f5c0ca47871d073cf4385b60ebb955de08` | 1 | Exact historical emitter code missing; then recheck swap connectivity |
| `0xad52d654964de37a08c6056ae12481168b9e12c6` | 4 | Two unconfirmed fund paths, one incoming credit and one unexplained token outflow |
| `0xee2c4517735f3ea7a31fdaf1c197bcf1380158c2` | 7 | Four unreviewed-emitter swaps, two incoming credits and one missing cash basis |
| `0x7af4253a4704e6d2aeeaf3455c22b7742473c241` | 9 | Eight incoming-only credits and one token outflow; 669 transactions already contain confirmed trades |

This is an audit-effort order, not a profitability or copy ranking. There is no
need for another 7,554-wallet scan. First obtain/recover the five exact missing
code proofs, review the most frequent unlisted emitters against deployment and
code evidence, and reconcile transfer inventory with confirmed trade inventory.
These are proposed next actions; this offline audit executes no provider requests.

Tests cover offline operation with sockets/HTTP forbidden, unchanged source
databases, corrupt caches, unreviewed emitters, missing/conflicting decimals,
exact-block code checks, incoming credits and output preservation.
