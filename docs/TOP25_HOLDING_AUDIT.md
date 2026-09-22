# Targeted holding-time audit

This audit changes neither promotion thresholds nor paper/live selection. A
research ranking is not proof that a wallet is copyable.

`copy_holding_duration.measure` reports the share of acquisition lots whose first
FIFO partial sale occurs strictly before the configured minimum (120 seconds in
this audit). The denominator includes only lots with an observed first sale.
Unsold lots are separately counted. At exactly 120 seconds an exit is not fast.
A passing median can coexist with fast exits; the share is descriptive, not an
additional admission threshold. Incomplete or ambiguous histories publish null
statistics rather than partial results or a fabricated zero-percent risk.

The campaign accepts an optional explicit priority file:

```powershell
.\.venv\Scripts\python.exe copy_holding_campaign.py run --directory data/copy_holding_campaign_20260922 --priority-file data/copy_holding_campaign_20260922/priority_top25_20260922.json
```

The JSON contains `wallets`, a list of at most 25 unique existing campaign
addresses. It does not add wallets, reset progress, reopen blocked/review-required
dossiers, bypass backoff, or increase paid/public budgets. Pending prioritized
wallets run ahead of other ready wallets; provider-cache shortcuts cannot
displace this group. Other pending wallets remain available when the prioritized
group has no eligible work. Only one worker owns the campaign database.

To read current results offline:

```powershell
.\.venv\Scripts\python.exe copy_holding_review.py --campaign data/copy_holding_campaign_20260922 --wallets data/copy_holding_campaign_20260922/priority_top25_20260922.json
```

Add `--output-dir` with a new directory to export a dated JSON and HTML report.
The reader uses SQLite read-only mode and performs no provider calls. It reports
decoded/missing transactions and reconstruction issues alongside the strict
holding result. A complete transport collection with ambiguous transactions is
still unverified. Existing evidence, run snapshots and prior reports are retained.
