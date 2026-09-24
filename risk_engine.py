from __future__ import annotations
from web3 import Web3
from models import RiskReport
from config import Settings

class RiskEngine:
    def __init__(self, rpc, market_client, arcscan_client, settings: Settings):
        self.rpc = rpc
        self.market = market_client
        self.arcscan = arcscan_client
        self.cfg = settings

    def inspect(self, token: str) -> RiskReport:
        token = Web3.to_checksum_address(token)
        code = self.rpc.get_code(token)
        report = RiskReport(token=token, has_code=bool(code))
        if not code:
            report.hard_fail_reasons.append("no_contract_code")
            return report
        market = self.market.token_snapshot(token)
        report.market = market
        top10, holders = self.arcscan.concentration(token)
        report.top10_concentration_pct = top10
        report.holder_count = holders
        if market is None:
            report.hard_fail_reasons.append("market_data_unavailable")
        else:
            if market.liquidity_usd is None:
                report.hard_fail_reasons.append("liquidity_unknown")
            elif market.liquidity_usd < self.cfg.min_liquidity_usd:
                report.hard_fail_reasons.append(f"liquidity_below_{self.cfg.min_liquidity_usd:.0f}")
            mc = market.market_cap_usd or market.fdv_usd
            if mc is None:
                report.hard_fail_reasons.append("market_cap_unknown")
            else:
                if mc < self.cfg.min_market_cap_usd: report.hard_fail_reasons.append("market_cap_too_small")
                if mc > self.cfg.max_market_cap_usd: report.hard_fail_reasons.append("market_cap_too_large_for_early_filter")
            age = market.pair_age_hours
            if not market.price_usd or market.price_usd <= 0:
                report.hard_fail_reasons.append("price_unknown")
            if age is None: report.hard_fail_reasons.append("pair_age_unknown")
            elif age > self.cfg.max_pair_age_hours: report.hard_fail_reasons.append("pair_too_old")
        if top10 is None:
            report.hard_fail_reasons.append("top10_concentration_unknown")
        elif top10 > self.cfg.max_top10_pct:
            report.hard_fail_reasons.append(f"top10_concentration_{top10:.1f}%")
        report.warnings.append("source_verification_not_used")
        return report
