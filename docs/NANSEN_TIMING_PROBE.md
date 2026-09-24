# Limited transaction-history comparison

`nansen_transactions_probe.py` compares archived Nansen address transactions with
an existing, read-only holding-campaign SQLite database. It is an explicit paid
research command, never invoked by scoring or the campaign worker. No trade,
holding eligibility, promotion or paper-selection authority is produced.

## Boundaries

- At most three distinct Arc wallets and three pages of 100 records each.
- Hard cap of ten HTTP attempts; no retries, redirects or automatic recharge.
- Five-minute request-start deadline and bounded HTTP timeouts.
- Require billing headers with cost and usage at most one credit per request.
  Stop on missing/changed billing or transport uncertainty; an already submitted
  request can still have been charged by the provider.
- Persist request intent before HTTP, raw responses before parsing, selected
  billing headers, final summary and SHA-256 manifest. Never write API keys.
- Exclusive output-directory creation prevents accidental overwrites.
- Validate chain, hash, pagination and explicit-zone timestamps. Naive dates
  remain unverified observations with `timestamp=null`; matching their clock
  against known UTC values is diagnostic only.
- `pages_complete` means the provider returned its last page in the requested
  date interval, not that complete chain history or confirmed swaps are proven.
- No production database writes, cache reset or automatic mass follow-up.

The scope is a JSON array of up to three objects:

```json
[{"wallet":"0x0000000000000000000000000000000000000001",
  "date":{"from":"2026-07-15T00:00:00Z","to":"2026-09-20T00:00:00Z"}}]
```

Manual invocation (uses `NANSEN_API_KEY` via existing configuration):

```powershell
.\.venv\Scripts\python.exe nansen_transactions_probe.py --scope data/probe_scope.json --source data/copy_holding_campaign_20260922/campaign.sqlite3 --output-dir outputs/new_unique_probe
```

`--prior-evidence outputs/previous_probe` can reuse a first pilot's archived
responses after a parser interruption. It verifies hashes and identical scope,
carries the previous attempts/credits into the same cap, and requires a new
output directory. Uncertain billing or incomplete evidence refuses continuation.
Keep both directories: the new manifest references the original evidence.

## Observed pilot, 22 September 2026

Six total requests/credits returned 460 transactions across three wallets
(137, 45, 278). Each reached the last provider page. Forty-eight hashes were
absent from the campaign copy at comparison time. All rows had `source_type`
`transfer` and timezone-less dates. Ten dates coincided with existing decoded
UTC timestamps; that does not establish correctness for all other rows.

The initial parser stopped after one request because dates lacked a timezone.
That response was retained and reused, not fetched or paid for again. The
comparison then continued with dates explicitly marked unverified.

The result supports investigating faster transaction enumeration, not replacing
receipt/trusted-DEX reconstruction or dropping local hashes not returned by the
provider. No further Nansen campaign is enabled by this probe.

Separately, the existing 1,512-wallet holding campaign was enabled for Alchemy
with a $5 conservative reserved budget, 100 reserved CU per HTTP attempt and
$1 per million reserved CU. This provision is not an invoice or an account-wide
spending cap. Existing retry, canonical-anchor, cache and checkpoint rules apply;
the worker returns to public RPC after its paid budget is exhausted.

References: [Address transactions](https://docs.nansen.ai/api/profiler/address-transactions),
[credits](https://docs.nansen.ai/getting-started/credits).
