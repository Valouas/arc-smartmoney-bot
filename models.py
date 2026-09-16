from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import time

@dataclass
class WalletInfo:
    address: str
    label: str
    weight: int
    group: str

@dataclass
class TradeEvent:
    tx_hash: str
    block_number: int
    timestamp: int
    wallet: str
    wallet_label: str
    wallet_weight: int
    side: str
    token: str
    token_symbol: str
    token_amount: float
    usdc_amount: float
    identity_id: Optional[str] = None
    reputation_score: Optional[int] = None
    intelligence_class: Optional[str] = None
    confirmation_eligible: bool = True
    trade_verified: bool = True
    # Descriptive metadata only; scoring/eligibility never reads these fields.
    sizing: dict = field(default_factory=dict)
    identity_known: Optional[bool] = None
    identity_cluster_size: Optional[int] = None

@dataclass
class MarketSnapshot:
    token: str
    symbol: str = "?"
    price_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    volume_m5: Optional[float] = None
    volume_h1: Optional[float] = None
    buys_m5: Optional[int] = None
    sells_m5: Optional[int] = None
    pair_created_at_ms: Optional[int] = None
    pair_address: Optional[str] = None
    dex_id: Optional[str] = None
    url: Optional[str] = None

    @property
    def pair_age_hours(self) -> Optional[float]:
        if not self.pair_created_at_ms:
            return None
        return max(0.0, (time.time() * 1000 - self.pair_created_at_ms) / 3_600_000)

    @property
    def volume_acceleration(self) -> Optional[float]:
        if self.volume_m5 is None or self.volume_h1 in (None, 0):
            return None
        baseline_5m = self.volume_h1 / 12.0
        return self.volume_m5 / max(baseline_5m, 1.0)

@dataclass
class RiskReport:
    token: str
    has_code: bool
    top10_concentration_pct: Optional[float] = None
    holder_count: Optional[int] = None
    market: Optional[MarketSnapshot] = None
    hard_fail_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.has_code and not self.hard_fail_reasons

@dataclass
class ClusterSignal:
    token: str
    side: str
    events: list[TradeEvent]
    distinct_wallets: int
    independent_identities: int
    ignored_wallets: int
    cluster_weight: int
    total_usdc: float
    first_seen: int
    last_seen: int
    risk: Optional[RiskReport] = None
    score: int = 0
    status: str = "WATCH"

    def to_dict(self):
        return asdict(self)
