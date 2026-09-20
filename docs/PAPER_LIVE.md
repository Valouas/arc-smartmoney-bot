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

## Automatic subscriptions

Set `ALCHEMY_SYNC_ENABLED=true`, `ALCHEMY_NOTIFY_AUTH_TOKEN`, `ALCHEMY_WEBHOOK_ID`
and `ALCHEMY_WEBHOOK_EXPECTED_URL` in the local `.env`. The Notify token differs
from the RPC API key and the webhook signing key. Never commit these secrets.

The launcher runs `alchemy_sync.py` every `ALCHEMY_SYNC_SECONDS` (default 300).
It verifies the exact webhook ID, Arc Mainnet, Address Activity, active state,
destination and signing key before updating it. It reads every address page,
adds missing paper-admitted wallets and existing exit-tracking wallets, and
verifies the result with a fresh read. Already registered addresses are preserved.
Membership in Alchemy never authorizes trades: the receiver and paper engine
still enforce current local eligibility and infrastructure exclusions.

Run `python alchemy_sync.py --once` for an immediate sync. Safe status counts are
written to `data/alchemy_sync_status.json`; errors retry on the next cycle and
never stop the independent live RPC scanner. No history or checkpoints change.
Existing remote addresses are intentionally not removed, including manual ones;
this may retain subscriptions to wallets that later leave paper selection.

API documentation:
- https://www.alchemy.com/docs/data/webhooks/webhooks-api-endpoints/notify-api-endpoints/team-webhooks
- https://www.alchemy.com/docs/data/webhooks/webhooks-api-endpoints/notify-api-endpoints/webhook-addresses
- https://www.alchemy.com/docs/data/webhooks/webhooks-api-endpoints/notify-api-endpoints/update-webhook-addresses
