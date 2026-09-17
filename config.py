from __future__ import annotations
import os
import math
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))

def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))

@dataclass(frozen=True)
class Settings:
    rpc_url: str = os.getenv("ARC_RPC_URL", "https://rpc.arc-scan.org")
    rpc_fallback_urls: tuple[str, ...] = tuple(u.strip() for u in os.getenv("ARC_RPC_FALLBACK_URLS", "").split(',') if u.strip())
    rpc_requests_per_second: float = _f("ARC_RPC_REQUESTS_PER_SECOND", 2)
    rpc_compute_units_per_second: float = _f("ARC_RPC_COMPUTE_UNITS_PER_SECOND", 0)
    rpc_fallback_requests_per_second: float = _f("ARC_RPC_FALLBACK_REQUESTS_PER_SECOND", 2)
    rpc_rate_limit_file: str = os.getenv("ARC_RPC_RATE_LIMIT_FILE", "data/rpc_rate_limit.sqlite3")
    chain_id: int = _i("ARC_CHAIN_ID", 5042)
    arcscan_api_url: str = os.getenv(
        "ARCSCAN_API_URL", "https://api.arc-scan.org/v1"
    ).rstrip("/")
    dexscreener_chain_id: str = os.getenv("DEXSCREENER_CHAIN_ID", "arc")

    poll_interval: float = _f("POLL_INTERVAL_SECONDS", 0.70)
    start_block: str = os.getenv("START_BLOCK", "latest")
    min_usdc_flow: float = _f("MIN_USDC_FLOW", 25)
    cluster_window_seconds: int = _i("CLUSTER_WINDOW_SECONDS", 1200)
    exit_cluster_window_seconds: int = _i("EXIT_CLUSTER_WINDOW_SECONDS", 600)

    min_liquidity_usd: float = _f("MIN_LIQUIDITY_USD", 25000)
    min_market_cap_usd: float = _f("MIN_MARKET_CAP_USD", 50000)
    max_market_cap_usd: float = _f("MAX_MARKET_CAP_USD", 3000000)
    max_pair_age_hours: float = _f("MAX_PAIR_AGE_HOURS", 24)
    max_top10_pct: float = _f("MAX_TOP10_CONCENTRATION_PCT", 35)
    min_cluster_weight: int = _i("MIN_CLUSTER_WEIGHT", 5)
    min_distinct_wallets: int = _i("MIN_DISTINCT_WALLETS", 2)

    launch_poll_seconds: int = _i("LAUNCH_POLL_SECONDS", 15)
    launch_candidate_recheck_seconds: int = _i("LAUNCH_CANDIDATE_RECHECK_SECONDS", 60)
    launch_candidate_expire_hours: int = _i("LAUNCH_CANDIDATE_EXPIRE_HOURS", 168)
    launch_min_liquidity_usd: float = _f("LAUNCH_MIN_LIQUIDITY_USD", 1000)
    launch_contract_page_size: int = _i("LAUNCH_CONTRACT_PAGE_SIZE", 100)
    launch_transfer_page_size: int = _i("LAUNCH_TRANSFER_PAGE_SIZE", 100)
    launch_max_market_checks_per_cycle: int = _i("LAUNCH_MAX_MARKET_CHECKS_PER_CYCLE", 40)

    winner_return_pct: float = _f("WINNER_RETURN_PCT", 100)
    scanner_capture_rate: float = _f("SCANNER_CAPTURE_RATE", 0.35)
    scanner_min_launch_buys: int = _i("SCANNER_MIN_LAUNCH_BUYS", 15)
    scanner_buys_per_active_hour: float = _f("SCANNER_BUYS_PER_ACTIVE_HOUR", 12)
    profile_min_evaluable_trades: int = _i("PROFILE_MIN_EVALUABLE_TRADES", 5)
    profile_refresh_seconds: int = _i("PROFILE_REFRESH_SECONDS", 60)
    intelligence_reload_seconds: int = _i("INTELLIGENCE_RELOAD_SECONDS", 30)

    discovery_poll_seconds: int = _i("DISCOVERY_POLL_SECONDS", 60)
    historical_analysis_enabled: bool = os.getenv('HISTORICAL_ANALYSIS_ENABLED', 'false').lower() == 'true'
    historical_transactions_per_step: int = _i('HISTORICAL_TRANSACTIONS_PER_STEP', 10)
    historical_refresh_seconds: int = _i('HISTORICAL_REFRESH_SECONDS', 21600)
    historical_max_age_hours: int = _i('HISTORICAL_MAX_AGE_HOURS', 48)
    historical_period_hours: int = _i('HISTORICAL_PERIOD_HOURS', 6)
    historical_min_profitable_periods: int = _i('HISTORICAL_MIN_PROFITABLE_PERIODS', 2)
    historical_max_concentration: float = _f('HISTORICAL_MAX_CONCENTRATION', 0.8)
    discovery_early_buyers: int = _i("DISCOVERY_EARLY_BUYERS", 50)
    discovery_transfer_pages: int = _i("DISCOVERY_TRANSFER_PAGES", 20)
    discovery_activity_pages: int = _i("DISCOVERY_ACTIVITY_PAGES", 20)
    discovery_page_size: int = _i("DISCOVERY_PAGE_SIZE", 100)
    discovery_max_wallets_per_cycle: int = _i("DISCOVERY_MAX_WALLETS_PER_CYCLE", 10)
    discovery_quarantine_hours: int = _i("DISCOVERY_QUARANTINE_HOURS", 12)
    promotion_min_score: int = _i("PROMOTION_MIN_SCORE", 68)
    promotion_min_winners: int = _i("PROMOTION_MIN_WINNERS", 2)
    promotion_min_launch_buys: int = _i("PROMOTION_MIN_LAUNCH_BUYS", 8)
    promotion_max_capture_rate: float = _f("PROMOTION_MAX_CAPTURE_RATE", 0.25)
    promotion_max_wallets: int = _i("PROMOTION_MAX_WALLETS", 100)
    watchlist_reload_seconds: int = _i("WATCHLIST_RELOAD_SECONDS", 30)

    # Operational/data-quality parameters; existing scoring thresholds are unchanged.
    launch_discovery_pages: int = _i("LAUNCH_DISCOVERY_PAGES", 10)
    outcome_horizon_seconds: int = _i("OUTCOME_HORIZON_SECONDS", 86400)
    outcome_grace_seconds: int = _i("OUTCOME_GRACE_SECONDS", 300)
    identity_cache_seconds: int = _i("IDENTITY_CACHE_SECONDS", 3600)
    identity_workers: int = _i("IDENTITY_WORKERS", 4)
    trusted_dex_registry: str = os.getenv("TRUSTED_DEX_REGISTRY", "trusted_dex.json")
    paper_fee_bps: float = _f("PAPER_FEE_BPS", 30)
    paper_slippage_bps: float = _f("PAPER_SLIPPAGE_BPS", 50)
    paper_volume_fraction: float = _f("PAPER_VOLUME_FRACTION", 0.01)
    paper_liquidity_fraction: float = _f("PAPER_LIQUIDITY_FRACTION", 0.001)

    paper_notional_usd: float = _f("PAPER_NOTIONAL_USD", 100)
    paper_stop_loss_pct: float = _f("PAPER_STOP_LOSS_PCT", 25)
    paper_take_profit_pct: float = _f("PAPER_TAKE_PROFIT_PCT", 150)

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    alchemy_webhook_enabled: bool = os.getenv('ALCHEMY_WEBHOOK_ENABLED', 'false').lower() == 'true'
    alchemy_webhook_host: str = os.getenv('ALCHEMY_WEBHOOK_HOST', '127.0.0.1')
    alchemy_webhook_port: int = _i('ALCHEMY_WEBHOOK_PORT', 8000)
    alchemy_webhook_signing_key: str = field(default=os.getenv('ALCHEMY_WEBHOOK_SIGNING_KEY', ''),repr=False)

    def __post_init__(self):
        if not 1 <= self.alchemy_webhook_port <= 65535:
            raise ValueError('Invalid webhook port')
        if (min(self.historical_transactions_per_step, self.historical_refresh_seconds,
                self.historical_max_age_hours, self.historical_period_hours,
                self.historical_min_profitable_periods) < 1
                or not 0 < self.historical_max_concentration <= 1):
            raise ValueError('Invalid historical analysis settings')
        if (not math.isfinite(self.rpc_compute_units_per_second)
                or (self.rpc_compute_units_per_second != 0 and self.rpc_compute_units_per_second < 60)):
            raise ValueError("ARC_RPC_COMPUTE_UNITS_PER_SECOND must be 0 or finite and at least 60")
        if not math.isfinite(self.rpc_requests_per_second) or self.rpc_requests_per_second <= 0:
            raise ValueError("ARC_RPC_REQUESTS_PER_SECOND must be finite and positive")
        if not math.isfinite(self.rpc_fallback_requests_per_second) or self.rpc_fallback_requests_per_second <= 0:
            raise ValueError("ARC_RPC_FALLBACK_REQUESTS_PER_SECOND must be finite and positive")
        if (self.launch_discovery_pages < 1 or self.outcome_horizon_seconds < 1
                or self.outcome_grace_seconds < 1 or self.identity_cache_seconds < 0
                or not 1 <= self.identity_workers <= 8):
            raise ValueError("Invalid observation or identity settings")

SETTINGS = Settings()
