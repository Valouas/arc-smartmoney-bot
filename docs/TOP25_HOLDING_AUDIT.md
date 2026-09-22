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

## Bounded transport acceleration

The campaign defaults to two independent RPC read clients (`--workers 1` keeps
serial reads; the upper bound is four). Transaction/receipt/block reads overlap,
but classification and transaction persistence remain on the owner thread in
input order. A failed read prevents acceptance of later prefetched results;
successfully collected raw responses remain in the cache/evidence ledger.
Each reader owns its HTTP session and provider state. All readers share the
existing public rate limiter (one request per second) or paid limiter, plus
durable atomic attempt reservations. Retry attempts still count against budgets.
Increasing workers does not increase the rate or credit allowance.

The campaign-specific RPC class reads only token symbol and decimals. Name and
total supply are not consumed by historical trade classification and are no
longer requested here. General bot metadata and historical decoder identity
remain unchanged. An unavailable decimal or transport error still fails closed.

To measure a bounded lot while retaining every result and the original deadline:

```powershell
.\.venv\Scripts\python.exe copy_holding_campaign.py run --directory data/copy_holding_campaign_20260922 --priority-file data/copy_holding_campaign_20260922/priority_top25_20260922.json --workers 2 --max-transactions 100 --max-run-seconds 1200
```

Stop the existing campaign cooperatively before this command; its process lock
prevents duplicate collectors. The transaction limit counts new persisted
wallet-transaction payloads, including explicit ambiguity/outside-snapshot
evidence. It is not a count of validated trades or qualified wallets. Failure
and retry reads can consume additional requests within the unchanged budget.
The immutable `run_*.json` records elapsed time, new payloads, HTTP attempts and
the stopping reason. Compare similar lots/cache states; one noisy network lot
does not establish a durable speedup.

Parallelism does not resolve unreviewed DEX emitters, transfers without an
observable acquisition cost, or missing historical code. Those remain research
issues; no holding-time eligibility is inferred from incomplete evidence.

### Measurement on 2026-09-22

The bounded public-RPC run persisted exactly 100 new wallet-transaction payloads
in 532.5 seconds (about 676/hour), using 528 RPC attempts and no Arcscan calls.
There were zero new Alchemy or Nansen calls. The existing paid reservation stayed
at 50,000 attempts / 5,000,000 reserved CU. Settings and all prior payloads were
compared with the pre-run SQLite backup and remained unchanged. Before/after
backups and request evidence were retained.

The run made 63 symbol and 63 decimal calls, with no name/totalSupply calls.
These unnecessary metadata reads are removed, but this heterogeneous lot does
not demonstrate a large end-to-end speedup. The immediately preceding serial
sample covered 80 different transactions in 348.2 seconds with 345 attempts;
cache state, wallet mix and invalid secondary-provider responses prevent a
controlled comparison. Public request rate still dominates elapsed time.

Offline inspection of three problematic transactions found one unreviewed swap
emitter, one incoming token transfer without a classified acquisition, and one
case lacking stored historical emitter-code evidence for a complete replay.
No new trust was granted and no payload was overwritten. The post-run report
still has 25 unknown holding results (24 wallets have reconstruction issues),
with 4,037 payloads left to collect. Collection completion is not audit approval.

Validation: 1,336 deterministic tests passed across the tracked suite plus the
new acceleration tests. Three Windows multiprocessing tests required an
unsandboxed rerun to create named pipes. The stale-backup failure test now passes
an explicit temporary root to preservation helpers, avoiding real user archives.
