# AGENTS.md — Arc Smart-Money Radar

## Project goal
Research and paper-trading system for Arc mainnet that:
- indexes new token launches;
- tracks seed and auto-discovered wallets;
- discounts common-funder / related-wallet clusters;
- detects spray-and-pray scanner wallets;
- scores selective early buyers;
- emits Telegram alerts;
- performs paper trading only.

## Safety / execution constraint
Do not add real-money automatic execution, private-key loading, transaction signing,
or unattended swap broadcasting unless the repository owner explicitly changes
that requirement. Preserve read-only + paper-trading defaults.

## First task for Codex
Before adding features, perform a complete V4 audit:
1. Verify Arc chain/RPC assumptions and native-USDC handling.
2. Verify Arcscan endpoint paths and response parsers against live responses.
3. Review swap reconstruction for false positives/negatives and multi-hop trades.
4. Review SQLite concurrency and migrations across the four processes.
5. Review wallet intelligence math for selection/survivorship bias.
6. Review auto-discovery quarantine/promotion logic and anti-Sybil clustering.
7. Add unit/integration tests for every bug or assumption found.
8. Do not silently change scoring thresholds; document suggested changes separately.

## Secret handling
- `.env` must never be committed.
- Never print Telegram tokens or other credentials.
- Use `.env.example` for variable names only.

## Development
- Python 3.10+
- Keep modules small and typed where practical.
- Prefer deterministic tests with fixtures over live-network tests.
- Mark network integration tests separately.
- Preserve backwards-compatible SQLite migrations where practical.
