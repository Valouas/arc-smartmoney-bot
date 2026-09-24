# Arc Smart-Money Radar V4 — Auto-Discovery

**Windows:** double-click `LANCER_BOT.bat` to run the analyzer, webhook and separate
paper-copy bot together. `ETAT_BOT.bat` shows their state; `ARRETER_BOT.bat` stops
the managed processes. See [launcher details](docs/WINDOWS_LAUNCHER.md).

**Local dashboard:** open `TABLEAU_DE_BORD.bat` or http://127.0.0.1:8001 after
starting the launcher. Paper portfolios, realized PnL, open inventory, historical
rankings and collection progress refresh every 10 seconds, using read-only local
data without additional RPC calls. The dashboard stays local; ngrok serves only
the webhook on port 8000.

V4 extends the V3 launch universe and wallet intelligence system with automatic
smart-wallet discovery.

**Historical analyzer mode:** set `HISTORICAL_ANALYSIS_ENABLED=true` to collect
complete wallet histories independently of scanner lag, rank realized FIFO
results, and export `HISTORICALLY_QUALIFIED` wallets for a separate paper bot.
This adds a fifth resumable worker and replaces auto-discovery's forward-dependent
qualification in this mode. It does not grant runtime trading authority.
See [historical selection and consumer contract](docs/HISTORICAL_SELECTION.md).
Use `python wallet_ranking.py --historical --top 20` for this ranking.

**Separate live paper-copy bot:** `python -m paper_copy` consumes the separate
`paper_review_selection.json` export: concentration and insufficient regularity
are explicit warnings for exploratory paper, alongside existing historical-data
warnings. All other admission requirements still apply. Strict
`historical_selection.json` qualification remains available and unchanged.
Each wallet has an independent virtual 1,000 USDC account.
It sizes buys between 10 and 100 USDC relative to prior median buys and copies
proportional sells. It is not started by `run_v4.py`. See
[paper-copy setup, assumptions and commands](docs/PAPER_COPY.md).

**Optional Alchemy Address Activity ingress:** `ALCHEMY_WEBHOOK_ENABLED=true`
adds a local receiver to `run_v4.py`. Signed notifications queue live triggers
for the existing V4 engine and wake the independent `paper_copy` scanner for
paper-admitted wallets, without moving either checkpoint from webhook data. See
[webhook setup and ngrok commands](docs/ALCHEMY_WEBHOOK.md).

The core rule is deliberately conservative:

**Seeing a wallet early on one winner is not enough to add it to the trading
signal set.**

A discovered address moves through:

```text
WINNER EARLY BUYER
    ↓
QUARANTINE
    ↓
VERIFIED OBSERVABLE HISTORY + FORWARD OBSERVATION
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
   receiving that token while spending net observable USDC, with supported swap evidence.

Plain transfers, airdrops and arbitrary recipients are therefore not promoted as
buyers merely because they received the token.

## Quarantine

Every newly discovered buyer is first stored in `discovery_candidates`.

Default:

```text
DISCOVERY_QUARANTINE_HOURS=12
```

During quarantine V4 reconstructs observable historical activity into
`research_trades` and starts observing the candidate prospectively. Historical
winner-selected trades do not train its production score. Supported transfer logs
also identify watched wallets in relayed transactions.

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
- winners must come from sufficiently distinct known deployer/funding components;
- a fresh version 2 forward profile must have adequate mature outcomes and no
  recorded coverage/reconstruction failures or missing mature quotes.

These are intentionally stricter than simply finding two winners.

## Why losers matter

For every candidate V4 scores **verified observable forward Arc launch buys**,
after discovery and the observation epoch. The opportunity universe includes
observed ERC-20 deployments without a liquid market, matched to recorded wallet
block coverage and activity hours.

Execution price is calculated from the transaction's observed:

```text
USDC spent / token amount received
```

The first purchase of each token is evaluated against one future quote in a fixed
window (24 hours, with five minutes of grace by default). Initial stakes determine
conviction; later purchases do not rewrite them. Pending and missing outcomes are
reported separately. Missing mature outcomes block confirmation eligibility.

## Generated watchlist

Passing wallets are written to:

```text
generated_watchlist.json
```

`bot.py` reloads this automatically. The manually researched `watchlist.json`
still has priority if the same wallet is present in both files.

A promoted wallet gets a dynamic weight derived from its forward intelligence
score. Seeds also need a fresh eligible profile; seed weights alone cannot confirm.

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

## Wallet ranking

Compare all discovered and profiled wallets without changing promotion rules:

```powershell
python wallet_ranking.py --top 20
python wallet_ranking.py --top 20 --diverse
python wallet_ranking.py --clusters
```

The console shows the top 20; `data/wallet_ranking.csv` and the summary include
everyone. This read-only report distinguishes forward evidence from research
history and grants no signal authority. See [wallet ranking](docs/WALLET_RANKING.md)
for the reporting categories, fields and limitations. Capital allocation,
conviction sizing and identity diversity are descriptive only; see
[capital allocation](docs/CAPITAL_ALLOCATION.md) for formulas and alert metadata.

## Forward backtest

Continue using:

```powershell
python backtest_v3.py
```

The report evaluates the first forward VALIDATED signal per token at a fixed
horizon, includes missing-result win-rate bounds, and separately reports realized
simulated PnL from the paper fill ledger. It does not establish a trading edge.

## Installation

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item trusted_dex.example.json trusted_dex.json
```

Before running `python run_v4.py`, populate `trusted_dex.json` with reviewed
immutable pool/PoolManager addresses and runtime-code hashes for Arc. The example
is empty: unknown DEX contracts have zero confirmation power. Configure trust
before collecting forward scoring evidence. See the
[follow-up migration and configuration guide](docs/AUDIT_V4_REMEDIATION.md).

Paper fills now model configurable fees, slippage, impact and limited capacity,
including partial exits and re-entry. These are simulation assumptions. Alerts
and proposals persist in SQLite; `python status.py` reports ambiguous deliveries
requiring manual review. A process lock prevents two scanners using the same DB.

## Execution safety

This package remains a research / paper-trading system. It contains no private
key loader, signer or automatic real-money transaction broadcaster.

## V4 audit and verification

See [the issue #1 audit](docs/AUDIT_V4.md) and
[follow-up corrections](docs/AUDIT_V4_REMEDIATION.md) for fixes, observed API
schemas, limitations and migration effects. The system reconstructs **verified observable
history**, not a guaranteed complete economic history. Unknown/capped histories
stay in quarantine. Gross native value without net mirror evidence cannot confirm
signals. Legacy automatic promotions must be revalidated after migration.

Install test dependencies with `pip install -r requirements-dev.txt` and run
`python -m pytest -m "not network" -q`. Public network contract tests are opt-in:
set `ARC_RUN_NETWORK=1` and run `python -m pytest -m network -v`.

RPC throttling, bounded retries, verified fallbacks and exact PowerShell commands
are described in [the RPC operations guide](docs/RPC_RESILIENCE.md).
Use `python rpc_check.py` for a small read-only check of your configured RPC.

Reviewed DEX emitters remain swap evidence but cannot become smart-money wallets.
For existing candidates/profiles, follow the [targeted infrastructure cleanup](docs/DEX_INFRASTRUCTURE.md)
after stopping all four processes; it preserves historical trades and other wallets.

Telegram now sends only one notification per new validated buy opportunity and
one per newly promoted wallet. Research, blocked signals and paper updates stay
local. See [notification policy and upgrade behavior](docs/TELEGRAM_NOTIFICATIONS.md).
