-- Arc mainnet (5042), discovery only. Never a profit/promotion assertion.
-- Coverage: last 7 days; reviewed Uniswap v4 PoolManager; direct USDC pairs;
-- one Swap per transaction; wallet must both pay/receive USDC ERC20 and
-- receive/pay the token. Contract accounts are not excluded by code type.
-- Native pool USDC is priced with 18 decimals; ERC20 settlement uses 6.
-- Transactions lacking ERC20 USDC settlement are deliberately not attributed.
-- Future hourly median swap quotes are research observations, not executable
-- exit prices or realized PnL. No unobserved open position is valued at zero.
WITH
pool AS (
    SELECT contract_address, id, currency0, currency1,
        CASE WHEN currency0 IN (0x0000000000000000000000000000000000000000,
                                0x3600000000000000000000000000000000000000)
             THEN currency1 ELSE currency0 END AS token,
        currency0 IN (0x0000000000000000000000000000000000000000,
                      0x3600000000000000000000000000000000000000) AS usd_is_zero,
        CASE WHEN currency0 = 0x0000000000000000000000000000000000000000
                   OR currency1 = 0x0000000000000000000000000000000000000000
             THEN 1e18 ELSE 1e6 END AS usd_scale
    FROM uniswap_v4_arc.poolmanager_evt_initialize
    WHERE contract_address = 0x8366a39cc670b4001a1121b8f6a443a643e40951
      AND (
        (currency0 IN (0x0000000000000000000000000000000000000000,
                       0x3600000000000000000000000000000000000000)
         AND currency1 NOT IN (0x0000000000000000000000000000000000000000,
                               0x3600000000000000000000000000000000000000))
        OR
        (currency1 IN (0x0000000000000000000000000000000000000000,
                       0x3600000000000000000000000000000000000000)
         AND currency0 NOT IN (0x0000000000000000000000000000000000000000,
                               0x3600000000000000000000000000000000000000))
      )
),
all_swaps AS (
    SELECT *, count(*) OVER (PARTITION BY evt_tx_hash) AS swaps_in_tx
    FROM uniswap_v4_arc.poolmanager_evt_swap
    WHERE evt_block_date >= current_date - interval '7' day
      AND evt_block_time >= current_timestamp - interval '7' day
      AND contract_address = 0x8366a39cc670b4001a1121b8f6a443a643e40951
),
quotes AS (
    SELECT s.evt_tx_hash, s.evt_block_time, p.token,
        abs(CAST(CASE WHEN p.usd_is_zero THEN s.amount0 ELSE s.amount1 END AS double)) / p.usd_scale
            / nullif(abs(CAST(CASE WHEN p.usd_is_zero THEN s.amount1 ELSE s.amount0 END AS double)), 0) AS price_per_raw,
        s.swaps_in_tx
    FROM all_swaps s JOIN pool p ON p.contract_address = s.contract_address AND p.id = s.id
    WHERE (s.amount0 < 0 AND s.amount1 > 0) OR (s.amount0 > 0 AND s.amount1 < 0)
),
hourly AS (
    SELECT token, date_trunc('hour', evt_block_time) AS hour,
           approx_percentile(price_per_raw, 0.5) AS price_per_raw
    FROM quotes
    WHERE price_per_raw > 0 AND is_finite(price_per_raw)
    GROUP BY 1, 2
),
future_prices AS (
    SELECT token, hour,
           max(price_per_raw) OVER (PARTITION BY token ORDER BY hour
               ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS future_price
    FROM hourly
),
settlement AS (
    SELECT q.evt_tx_hash, q.evt_block_time, q.token, t.contract_address,
           t."from" AS sender, t."to" AS recipient, CAST(t.value AS double) AS raw_amount
    FROM quotes q JOIN erc20_arc.evt_transfer t ON t.evt_tx_hash = q.evt_tx_hash
      AND (t.contract_address = q.token OR t.contract_address = 0x3600000000000000000000000000000000000000)
    WHERE q.swaps_in_tx = 1
      AND t.evt_block_date >= current_date - interval '7' day
      AND t.evt_block_time >= current_timestamp - interval '7' day
),
net_flows AS (
    SELECT evt_tx_hash, evt_block_time, token, wallet,
        sum(CASE WHEN contract_address = token THEN sign * raw_amount ELSE 0 END) AS token_net,
        sum(CASE WHEN contract_address = 0x3600000000000000000000000000000000000000
                 THEN sign * raw_amount / 1e6 ELSE 0 END) AS usd_net
    FROM settlement CROSS JOIN UNNEST(ARRAY[sender, recipient], ARRAY[-1, 1]) AS flow(wallet, sign)
    WHERE wallet NOT IN (0x0000000000000000000000000000000000000000,
                         0x8366a39cc670b4001a1121b8f6a443a643e40951)
    GROUP BY 1, 2, 3, 4
),
trades AS (
    SELECT *, token_net > 0 AS is_buy, abs(usd_net) AS notional,
        abs(usd_net / token_net) AS entry_price
    FROM net_flows
    WHERE ((token_net > 0 AND usd_net < 0) OR (token_net < 0 AND usd_net > 0))
      AND is_finite(token_net) AND is_finite(usd_net)
),
winner_buyers AS (
    SELECT t.wallet, max(f.future_price / t.entry_price) AS best_observed_multiple,
           count(DISTINCT t.token) AS winner_tokens
    FROM trades t JOIN future_prices f ON f.token = t.token
      AND f.hour = date_trunc('hour', t.evt_block_time)
    WHERE t.is_buy AND f.future_price / t.entry_price >= 5
    GROUP BY 1
),
wallet_activity AS (
    SELECT t.wallet,
        CAST(to_unixtime(min(t.evt_block_time) AT TIME ZONE 'UTC') AS bigint) AS first_seen_at,
        CAST(to_unixtime(max(t.evt_block_time) AT TIME ZONE 'UTC') AS bigint) AS last_seen_at,
        count_if(t.is_buy) AS buy_count, count_if(NOT t.is_buy) AS sell_count,
        count(*) AS trade_count, count(DISTINCT t.token) AS distinct_tokens,
        sum(t.notional) AS volume_usd
    FROM trades t JOIN winner_buyers w ON w.wallet = t.wallet
    GROUP BY 1
)
SELECT concat('0x', lower(to_hex(a.wallet))) AS wallet, CAST(5042 AS bigint) AS chain_id,
    a.first_seen_at, a.last_seen_at, a.buy_count, a.sell_count, a.trade_count,
    a.distinct_tokens, a.volume_usd,
    CAST(NULL AS double) AS realized_pnl, CAST(NULL AS double) AS roi,
    CAST(NULL AS double) AS win_rate,
    w.best_observed_multiple, w.winner_tokens,
    'uniswap_v4_single_swap_erc20_usdc_settlement_7d' AS coverage
FROM wallet_activity a JOIN winner_buyers w ON w.wallet = a.wallet
ORDER BY (w.best_observed_multiple >= 10) DESC, w.winner_tokens DESC,
         w.best_observed_multiple DESC, a.wallet
LIMIT 500
