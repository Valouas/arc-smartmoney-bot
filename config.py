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
    external_discovery_enabled: bool = os.getenv('EXTERNAL_DISCOVERY_ENABLED', 'false').lower() == 'true'
    external_poll_seconds: int = _i('EXTERNAL_POLL_SECONDS', 900)
    nansen_api_key: str = field(default=os.getenv('NANSEN_API_KEY', ''), repr=False)
    nansen_bulk_enabled: bool = os.getenv('NANSEN_BULK_ENABLED', 'false').lower() == 'true'
    nansen_bulk_max_requests: int = _i('NANSEN_BULK_MAX_REQUESTS', 20000)
    dune_enabled: bool = os.getenv('DUNE_ENABLED', 'false').lower() == 'true'
    dune_api_key: str = field(default=os.getenv('DUNE_API_KEY', ''), repr=False)
    dune_query_id: str = os.getenv('DUNE_QUERY_ID', '').strip()
    dune_refresh_seconds: int = _i('DUNE_REFRESH_SECONDS', 900)
    dune_max_candidates: int = _i('DUNE_MAX_CANDIDATES', 500)
    zerion_enabled: bool = os.getenv('ZERION_ENABLED', 'false').lower() == 'true'
    zerion_api_key: str = field(default=os.getenv('ZERION_API_KEY', ''), repr=False)
    zerion_refresh_seconds: int = _i('ZERION_REFRESH_SECONDS', 3600)
    zerion_max_wallets_per_cycle: int = _i('ZERION_MAX_WALLETS_PER_CYCLE', 25)
    zerion_requests_per_second: float = _f('ZERION_REQUESTS_PER_SECOND', 1)
    zerion_daily_request_limit: int = _i('ZERION_DAILY_REQUEST_LIMIT', 280)
    zerion_budget_file: str = os.getenv('ZERION_BUDGET_FILE', 'data/zerion_budget.sqlite3')
    rpc_url: str = os.getenv("ARC_RPC_URL", "https://rpc.arc-scan.org")
    rpc_fallback_urls: tuple[str, ...] = tuple(u.strip() for u in os.getenv("ARC_RPC_FALLBACK_URLS", "").split(',') if u.strip())
    rpc_requests_per_second: float = _f("ARC_RPC_REQUESTS_PER_SECOND", 2)
    rpc_compute_units_per_second: float = _f("ARC_RPC_COMPUTE_UNITS_PER_SECOND", 0)
    rpc_fallback_requests_per_second: float = _f("ARC_RPC_FALLBACK_REQUESTS_PER_SECOND", 2)
    rpc_rate_limit_file: str = os.getenv("ARC_RPC_RATE_LIMIT_FILE", "data/rpc_rate_limit.sqlite3")
    rpc_cache_enabled: bool = os.getenv('ARC_RPC_CACHE_ENABLED','false').lower() == 'true'
    rpc_cache_file: str = os.getenv('ARC_RPC_CACHE_FILE','data/rpc_cache.sqlite3')
    scanner_log_window_blocks: int = _i('SCANNER_LOG_WINDOW_BLOCKS',10)
    automatic_backup_enabled: bool = os.getenv('AUTOMATIC_BACKUP_ENABLED', 'true').lower() == 'true'
    automatic_backup_keep_all: bool = os.getenv('AUTOMATIC_BACKUP_KEEP_ALL', 'true').lower() == 'true'
    automatic_backup_directory: str = os.getenv('AUTOMATIC_BACKUP_DIRECTORY', 'data/automatic_backups')
    automatic_backup_interval_seconds: int = _i('AUTOMATIC_BACKUP_INTERVAL_SECONDS', 3600)
    automatic_backup_keep_last: int = _i('AUTOMATIC_BACKUP_KEEP_LAST', 24)
    automatic_backup_keep_daily: int = _i('AUTOMATIC_BACKUP_KEEP_DAILY', 7)
    automatic_backup_max_age_seconds: int = _i('AUTOMATIC_BACKUP_MAX_AGE_SECONDS', 10800)
    automatic_backup_timeout_seconds: int = _i('AUTOMATIC_BACKUP_TIMEOUT_SECONDS', 300)
    automatic_backup_reserve_mb: int = _i('AUTOMATIC_BACKUP_RESERVE_MB', 2048)
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
    # Discovery sources only; do not change forward winner/promotion thresholds.
    discovery_min_multiple: float = _f('DISCOVERY_MIN_MULTIPLE', 5)
    discovery_priority_multiple: float = _f('DISCOVERY_PRIORITY_MULTIPLE', 10)
    discovery_source_tokens_per_cycle: int = _i('DISCOVERY_SOURCE_TOKENS_PER_CYCLE', 3)
    discovery_source_retry_seconds: int = _i('DISCOVERY_SOURCE_RETRY_SECONDS', 300)
    scanner_capture_rate: float = _f("SCANNER_CAPTURE_RATE", 0.35)
    scanner_min_launch_buys: int = _i("SCANNER_MIN_LAUNCH_BUYS", 15)
    scanner_buys_per_active_hour: float = _f("SCANNER_BUYS_PER_ACTIVE_HOUR", 12)
    profile_min_evaluable_trades: int = _i("PROFILE_MIN_EVALUABLE_TRADES", 5)
    profile_refresh_seconds: int = _i("PROFILE_REFRESH_SECONDS", 60)
    intelligence_reload_seconds: int = _i("INTELLIGENCE_RELOAD_SECONDS", 30)

    discovery_poll_seconds: int = _i("DISCOVERY_POLL_SECONDS", 60)
    historical_analysis_enabled: bool = os.getenv('HISTORICAL_ANALYSIS_ENABLED', 'false').lower() == 'true'
    historical_transactions_per_step: int = _i('HISTORICAL_TRANSACTIONS_PER_STEP', 10)
    historical_rpc_workers: int = _i('HISTORICAL_RPC_WORKERS', 1)
    historical_poll_seconds: float = _f('HISTORICAL_POLL_SECONDS', 60)
    historical_active_poll_seconds: float = _f('HISTORICAL_ACTIVE_POLL_SECONDS', 5)
    historical_refresh_seconds: int = _i('HISTORICAL_REFRESH_SECONDS', 21600)
    historical_priority_enabled: bool = os.getenv('HISTORICAL_PRIORITY_ENABLED', 'true').lower() == 'true'
    historical_focus_steps: int = _i('HISTORICAL_FOCUS_STEPS', 4)
    historical_inactive_days: int = _i('HISTORICAL_INACTIVE_DAYS', 3)
    historical_inactive_refresh_seconds: int = _i('HISTORICAL_INACTIVE_REFRESH_SECONDS', 86400)
    historical_retry_seconds: int = _i('HISTORICAL_RETRY_SECONDS', 300)
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
    paper_live_enabled: bool = os.getenv('PAPER_LIVE_ENABLED', 'false').lower() == 'true'
    paper_copy_paused: bool = os.getenv('PAPER_COPY_PAUSED', 'false').lower() == 'true'
    research_paper_only: bool = os.getenv('RESEARCH_PAPER_ONLY', 'false').lower() == 'true'
    paper_log_query_blocks: int = _i('PAPER_LOG_QUERY_BLOCKS', 10)
    paper_catchup_window_blocks: int = _i('PAPER_CATCHUP_WINDOW_BLOCKS', 40)
    alchemy_webhook_enabled: bool = os.getenv('ALCHEMY_WEBHOOK_ENABLED', 'false').lower() == 'true'
    alchemy_webhook_host: str = os.getenv('ALCHEMY_WEBHOOK_HOST', '127.0.0.1')
    alchemy_webhook_port: int = _i('ALCHEMY_WEBHOOK_PORT', 8000)
    alchemy_webhook_signing_key: str = field(default=os.getenv('ALCHEMY_WEBHOOK_SIGNING_KEY', ''),repr=False)
    alchemy_notify_auth_token: str = field(default=os.getenv('ALCHEMY_NOTIFY_AUTH_TOKEN',''),repr=False)
    alchemy_webhook_id: str = os.getenv('ALCHEMY_WEBHOOK_ID','')
    alchemy_webhook_expected_url: str = os.getenv('ALCHEMY_WEBHOOK_EXPECTED_URL','')
    alchemy_sync_enabled: bool = os.getenv('ALCHEMY_SYNC_ENABLED','false').lower() == 'true'
    alchemy_sync_seconds: int = _i('ALCHEMY_SYNC_SECONDS',300)

    def __post_init__(self):
        if (self.zerion_daily_request_limit < 1
                or not math.isfinite(self.zerion_requests_per_second) or not 0 < self.zerion_requests_per_second <= 10
                or self.external_poll_seconds < 60
                or self.dune_refresh_seconds < 60 or self.zerion_refresh_seconds < 60
                or not 1 <= self.dune_max_candidates <= 5000
                or not 1 <= self.zerion_max_wallets_per_cycle <= 100):
            raise ValueError('Invalid external data collection limits')
        if not 10 <= self.paper_log_query_blocks <= 1000 or not 40 <= self.paper_catchup_window_blocks <= 2000:
            raise ValueError('Invalid paper transport windows')
        if self.research_paper_only and not (self.historical_analysis_enabled and self.paper_live_enabled):
            raise ValueError('Research/paper mode requires historical analysis and live paper')
        if (not math.isfinite(self.discovery_min_multiple) or self.discovery_min_multiple < 1
                or not math.isfinite(self.discovery_priority_multiple)
                or self.discovery_priority_multiple < self.discovery_min_multiple
                or self.discovery_source_tokens_per_cycle < 1 or self.discovery_source_retry_seconds < 1):
            raise ValueError('Invalid discovery source settings')
        if (not 1 <= self.historical_focus_steps <= 16 or self.historical_inactive_days < 1
                or self.historical_inactive_refresh_seconds < 1 or self.historical_retry_seconds < 1):
            raise ValueError('Invalid historical scheduling settings')
        if self.alchemy_sync_seconds < 60:
            raise ValueError('ALCHEMY_SYNC_SECONDS must be at least 60')
        if not 1 <= self.scanner_log_window_blocks <= 10:
            raise ValueError('SCANNER_LOG_WINDOW_BLOCKS must be between 1 and 10')
        if (self.automatic_backup_interval_seconds < 60 or self.automatic_backup_keep_last < 2
                or self.automatic_backup_keep_daily < 1 or self.automatic_backup_timeout_seconds < 1
                or self.automatic_backup_reserve_mb < 0
                or self.automatic_backup_max_age_seconds < self.automatic_backup_interval_seconds):
            raise ValueError('Invalid automatic backup settings')
        if not 1 <= self.historical_rpc_workers <= 16:
            raise ValueError('HISTORICAL_RPC_WORKERS must be between 1 and 16')
        if not math.isfinite(self.historical_poll_seconds) or self.historical_poll_seconds < 1:
            raise ValueError('HISTORICAL_POLL_SECONDS must be finite and at least 1')
        if not math.isfinite(self.historical_active_poll_seconds) or self.historical_active_poll_seconds < 1:
            raise ValueError('HISTORICAL_ACTIVE_POLL_SECONDS must be finite and at least 1')
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
