# Targeted Nansen support for holding-duration research

The owner authorized **2,000 additional Nansen credits** on 22 September 2026.
This opt-in campaign targets only `PENDING/FETCHING` wallets in the existing
1,512-wallet holding campaign. It does not rescan the 7,554-wallet universe,
refresh PnL, or grant paper/live authority.

## Budget and preservation

- Persistently reserve one attempt in SQLite before each HTTP submission.
- Maximum 2,000 additional attempts at the currently verified price of one
  credit per address-transactions request. Prior pilot pages are free cache hits.
- Read actual billing headers; stop on missing or changed pricing, transport
  uncertainty, HTTP errors or an account balance of 100 credits or less.
  Provider pricing is external: an already-submitted request cannot be undone.
- Never recharge or purchase credits. The cap applies to this campaign, not to
  unrelated account traffic. A process lock prevents duplicate workers.
- Fixed two-hour campaign deadline and one request start per second maximum.
- Archive full/partial response bytes, billing metadata and SHA-256 digests.
  Durable SQLite contains each request and response. Keep all generations;
  snapshot at preparation, every five minutes while progressing, and shutdown.
- On restart, reuse a fully persisted response even if page indexing had not
  finished. An uncertain in-flight request blocks further spending.

## Integration

Pages are validated and their hashes staged in a separate SQLite database.
The producer rechecks local progress before each page and skips wallets that
have already left the fetching phase. Only exact scope/date matches can reuse
pilot pages, and archive digests must validate.

The reconstruction worker imports hashes only into a matching wallet/cutoff/hash/
decoder context while its historical run is still fetching. It never replaces
existing payloads. Up to ten advisory transactions are decoded per turn using
the existing RPC receipt/block/trusted-DEX checks. Successful payloads commit
individually; failures leave remaining work pending.

At most every other worker turn prioritizes a fetching wallet with staged hashes
that are not yet decoded. Other turns preserve the normal round-robin queue, so
the Nansen supplement cannot monopolize all reconstruction work. Already decoded
hashes and backoff-delayed wallets do not receive this priority.

**Nansen's last page does not complete a local history.** Arcscan remains
responsible for the original coverage/pagination proof, and canonical block
checks remain required. Dates without a timezone remain unverified; provider
transfers never directly become BUY/SELL. Late evidence stays archived even if
the local run has already advanced and no longer imports new advisory hashes.

This makes transaction hashes available for earlier reconstruction. It does not
remove the RPC reconstruction cost or guarantee that Nansen will improve the
overall completion time. The consumer reports `nansen_seed_hashes`,
`nansen_new_hashes` and `nansen_early_decoded` to measure actual contribution.
Its existing `nansen_http_calls=0` refers to that worker only; the separate Nansen
campaign status contains the actual shared attempt and credit totals.

## Operate the existing campaign

Run from the repository root in PowerShell. These status commands make no API calls:

```powershell
.\.venv\Scripts\python.exe nansen_holding_campaign.py status --directory data/nansen_holding_campaign_20260922_2000
.\.venv\Scripts\python.exe copy_holding_campaign.py status --directory data/copy_holding_campaign_20260922
```

The existing workers were started with hidden windows and separate logs:

- `data/nansen_holding_campaign_20260922_2000/worker.out.log`
- `data/nansen_holding_campaign_20260922_2000/worker.err.log`
- `data/copy_holding_campaign_20260922/worker_alchemy_nansen_priority.out.log`
- `data/copy_holding_campaign_20260922/worker_alchemy_nansen_priority.err.log`

The earlier worker logs are retained. At the first integration check, 78 new
requests/credits had produced 7,882 wallet transactions. The consumer had imported
148 previously absent hashes and decoded 40 advisory transactions early. These
are progress measurements, not newly qualified wallets or a completion estimate.

Gracefully stop just the Nansen supplement (no data deletion):

```powershell
New-Item -ItemType File -Path data/nansen_holding_campaign_20260922_2000/STOP
```

Do not prepare another directory to restart: that would create a different
budget. Resume the same directory only after reviewing any recorded halt.
Alchemy retains its previously authorized $5 conservative reserved cap, without
resetting its usage. Production scoring, promotion and paper configuration are
unchanged.
