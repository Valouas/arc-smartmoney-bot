# Zerion PnL subquota (verified 21 September 2026)

The Developer dashboard's 2,000 daily requests are not all available for PnL.
Zerion documents a 25% quota for Wallet PnL, Portfolio Chart and DeFi Positions:
https://zerion.io/api/endpoints/wallet-pnl

A live Arc PnL request returned HTTP 429 with these non-secret headers:

```
ratelimit-limit: 500 500;w=86400
ratelimit-remaining: 0
ratelimit-reset: 3730
ratelimit-org-day-limit: 2000
ratelimit-org-day-remaining: 1410
ratelimit-org-day-reset: 3730
```

The reset was midnight UTC (02:00 Europe/Zurich on this date). The transport
now honors the longest applicable exhausted-counter reset or Retry-After.
It does not turn a 429 into empty PnL, retry an exhausted daily quota inline,
or infer that global remaining credits override the PnL subquota. The existing
pipeline persists this pause and resumes automatically when due.

The local SQLite request ledger counts all HTTP attempts, including rejected
requests and retries. It is a conservative cap, not the provider's billable
usage counter; it is not reset or reduced to match a dashboard screenshot.

At about 500 PnL requests/day, roughly 7,000 outstanding wallets require about
two weeks of quota windows, longer if errors or refreshes consume calls. This
is a first external PnL pass, not a complete local audit or evidence of forward
copy profitability. Paper remains explicitly paused via PAPER_COPY_PAUSED=true.
No scoring, promotion, history or checkpoint changes are involved.
