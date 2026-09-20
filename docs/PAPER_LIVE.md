# Paper live and Alchemy

`PAPER_LIVE_ENABLED=true` activates an independent forward cursor in the existing
paper ledger. This operational change does not change selection, amounts, fees,
sizing, scoring, security, swap confirmation or paper-only execution.

First activation requires no open copied positions and no pending orders. It
records `live_start_block` and `live_started_at`, preserves the old `next_block`
and `last_hash`, and explicitly records the unprocessed old interval in
`live_uncovered_from` / `live_uncovered_to`. That interval is **not** reported as
processed. The historical analysis/scanner continues independently in its own
database. Existing cash, observations, fills and baselines are retained. No
historical trades are retroactively filled at current prices.

On restart the live cursor always resumes, even if the environment flag is later
removed. It never jumps ahead again. A reorganization halts for review. Empty
ranges require successful sender AND recipient Transfer queries for every batch
and subrange, with stable canonical boundary checks. Their coverage is recorded
in `live_empty_ranges`. Blocks with activity still use the original ordered
receipt decoder and atomic per-block checkpoint. Transport failures do not
become empty results. Both paths retain the same observation IDs.

Signed Alchemy notifications only wake this pipeline. RPC fallback checks every
two seconds (plus request/processing time) even without notifications; it tracks
all currently selected wallets automatically. Two confirmations and all existing
freshness limits still apply. This is near-live, not instantaneous execution.
The global RPC budget remains unchanged. Waiting paper requests have priority
over historical readers under that same budget, preventing a costly log query
from starving behind continuous cheaper reads. Priority uses a two-second lease
that expires if the paper worker dies; server cooldowns are still respected.
The dashboard exposes the measured live
head, cursor, lag and measurement time separately from the archived checkpoint.

## Alchemy setup

Create an Address Activity webhook on **Arc Mainnet** with the HTTPS tunnel URL
ending `/webhook/alchemy`. Export addresses using:

```powershell
((.\.venv\Scripts\python.exe alchemy_webhook.py --paper-addresses | ConvertFrom-Json).addresses -join ",") | Set-Clipboard
```

Use that webhook's signing key in `ALCHEMY_WEBHOOK_SIGNING_KEY`, never the RPC API
key. Restart the receiver after changing the environment file. `/health` returning
200 only proves reachability; it does not prove Alchemy configured/active or a
valid signature. Verify a provider delivery returned 200 and appeared in the
inbox. A test payload from another chain must remain rejected.

Remote address synchronization is currently manual. Newly selected addresses
are automatically covered by the live RPC fallback, but must also be added in
Alchemy to receive notifications. No Notify auth token is assumed or extracted
from browser credentials. An unavailable webhook does not stop either scanner.
