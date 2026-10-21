# Arc Smart-Money Radar V4 — Auto-Discovery

V4 extends the V3 launch universe and wallet intelligence system with automatic
smart-wallet discovery.

The core rule is deliberately conservative:

**Seeing a wallet early on one winner is not enough to add it to the trading
signal set.**

A discovered address moves through:

```text
WINNER EARLY BUYER
    ↓
QUARANTINE
    ↓
FULL ARC HISTORY RECONSTRUCTION
    ↓
SELECTIVITY / HIT RATE / CONVICTION / PRECOCITY PROFILE
    ↓
PROMOTABLE
    ↓
GENERATED WATCHLIST
```

## Four processes

Start everything with:

```powershell
python run_v4.py
```

This starts:

1. `launch_indexer.py` — indexes the observable Arc launch universe.
2. `wallet_profiler.py` — profiles currently observed wallets.
3. `auto_discovery.py` — finds and vets new candidate wallets.
4. `bot.py` — live read-only scanner + paper trading.

## How V4 discovers wallets

A launch is considered a historical winner when a collected post-launch snapshot
has risen by at least `WINNER_RETURN_PCT` from the first recorded launch price.

For a new winner V4:

1. downloads the token's transfer history from Arcscan;
2. orders it oldest-first;
3. looks at the earliest recipients;
4. fetches the transaction and receipt from Arc RPC;
5. only accepts an address as an early buyer when the receipt actually shows it
   receiving that token while spending observable USDC.

Plain transfers, airdrops and arbitrary recipients are therefore not promoted as
buyers merely because they received the token.

## Quarantine

Every newly discovered buyer is first stored in `discovery_candidates`.

Default:

```text
DISCOVERY_QUARANTINE_HOURS=12
```

During quarantine V4 reconstructs the wallet's sent transaction activity and
stores its buy/sell decisions in `research_trades`.

A candidate has **zero live confirmation power** during this stage.

## Promotion gates

Default gates require all of these:

```text
PROMOTION_MIN_SCORE=68
PROMOTION_MIN_WINNERS=2
PROMOTION_MIN_LAUNCH_BUYS=8
PROMOTION_MAX_CAPTURE_RATE=0.25
```

Additionally:

- `SCANNER_BOT` is rejected;
- `IGNORE` is rejected;
- a candidate funded by the same address as an already promoted wallet is rejected;
- the global auto-watchlist is capped at 100 wallets;
- the anti-sybil common-funder rule still applies after promotion.

These are intentionally stricter than simply finding two winners.

## Why losers matter

For every candidate V4 reconstructs **all observable Arc launch buys**, not only
the token through which the wallet was discovered.

Execution price is calculated from the transaction's observed:

```text
USDC spent / token amount received
```

This allows its later launch snapshots to be compared to the wallet's own entry
rather than pretending the launch's first recorded price was its entry price.

## Generated watchlist

Passing wallets are written to:

```text
generated_watchlist.json
```

`bot.py` reloads this automatically. The manually researched `watchlist.json`
still has priority if the same wallet is present in both files.

A promoted wallet gets a dynamic weight derived from its V3 intelligence score.

## Inspecting discovery

```powershell
python discovery_status.py
```

Shows:

- quarantined candidates;
- source-winner count;
- best early-buyer rank;
- intelligence score/class;
- reason a candidate has not been promoted;
- promoted wallets.

## Forward backtest

Continue using:

```powershell
python backtest_v3.py
```

The point is to compare VALIDATED signals before and after auto-discovered
wallets start contributing.

## Installation

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python run_v4.py
```

## Execution safety

This package remains a research / paper-trading system. It contains no private
key loader, signer or automatic real-money transaction broadcaster.

## License

**Non-commercial use only.** Licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

> Read-only research tool with paper trading only. It never holds private keys and never signs transactions. Not financial advice.
