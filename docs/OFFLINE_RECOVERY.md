# Offline SQLite evidence recovery

`recover_sqlite_evidence.py` reads raw database pages and WAL frames, including
stale WAL generations. It never opens the sources through SQLite, calls an RPC,
changes the operational database, or feeds scoring/paper trading.

Stop the launcher and preserve byte-for-byte copies of the database, WAL and SHM
before investigating. Do not open the preserved evidence through a normal SQLite
connection: work on a separate copy if a database connection is needed. A copied
database/WAL is not automatically a consistent historical snapshot.

PowerShell example, from the project directory (substitute the actual preserved
directory and choose a **new** output filename for each run):

```powershell
.\.venv\Scripts\python.exe recover_sqlite_evidence.py `
  --source data/recovery_forensics/PRESERVED/arc_bot.sqlite3 `
  --source data/recovery_forensics/PRESERVED/arc_bot.sqlite3-wal `
  --output data/recovery_forensics/leaf_evidence.sqlite3
```

Repeat with `--orphans` to find intact SQLite records whose cell pointers have
been removed. `--bodies` also saves intact adjacent wallet/hash/JSON fields when
their record header has been destroyed. The latter has weaker provenance.
Only UTF-8 database records with 4096-byte pages and no reserved bytes are
supported by the default CLI. Overflow records are skipped, never guessed.

The output retains source SHA-256 hashes, source paths, the first page/frame and
cell offset, WAL salts, payload hashes and occurrence counts. Repeated records
are deduplicated by wallet + transaction hash + payload hash. Distinct payload
variants are preserved; no variant is silently chosen as authoritative.

All outputs are **QUARANTINED_NOT_TRADING_INPUT**. Neither a parseable record nor
a stale WAL frame proves that a transaction was committed, canonical, correctly
decoded, or part of a completed collection. The tool does not validate WAL
commit/checksum chains. Do not import its output into completed snapshots or
promotion inputs without independently establishing those properties.

For recovery accounting, compare keys against the union of operational rows,
retained snapshots and earlier recovery archives. Do not count already-preserved
records as newly recovered. Separate decoder errors and incomplete fragments
from successful decodings, and report records salvaged separately from completed
wallet histories restored.

The incident's local evidence and detailed recovery inventory are retained in
`data/recovery_forensics/` (gitignored). They must not be removed or overwritten
as part of cleanup, restart, test execution or a refresh.
