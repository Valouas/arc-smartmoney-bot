"""Explicit simulation assumptions; independent of wallet promotion."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from statistics import median


def decimal(value):
    if isinstance(value, bool): raise ValueError('Invalid amount')
    try: number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError): raise ValueError('Invalid amount') from None
    if not number.is_finite() or number < 0: raise ValueError('Invalid amount')
    return number


@dataclass(frozen=True)
class Policy:
    initial_cash: str = '1000'
    min_buy: str = '10'
    normal_buy: str = '50'
    max_buy: str = '100'
    fee_bps: str = '30'
    slippage_bps: str = '50'
    gas_usdc: str = '0.01'
    liquidity_fraction: str = '0.001'
    volume_fraction: str = '0.01'
    max_signal_age: int = 60
    confirmations: int = 2
    block_window: int = 40
    poll_seconds: float = 2
    min_baseline_buys: int = 5

    def __post_init__(self):
        if any(type(getattr(self,k)) is not int for k in ('max_signal_age','confirmations','block_window','min_baseline_buys')):
            raise ValueError('Collection counts must be integers')
        values = {k: decimal(v) for k,v in asdict(self).items()}
        if not 0 < values['min_buy'] <= values['normal_buy'] <= values['max_buy'] <= values['initial_cash']:
            raise ValueError('Invalid paper cash or buy bounds')
        if not 0 <= values['fee_bps'] < 10000 or not 0 <= values['slippage_bps'] < 10000:
            raise ValueError('Invalid costs')
        if not all(0 < values[k] <= 1 for k in ('liquidity_fraction','volume_fraction')):
            raise ValueError('Invalid paper capacity')
        if any(values[k] < 1 for k in ('max_signal_age','confirmations','block_window','min_baseline_buys')) or self.poll_seconds <= 0:
            raise ValueError('Invalid collection policy')


def size_buy(source_usdc, prior_buys, policy):
    """Past buys only; never use the triggering buy to set its own baseline."""
    source = decimal(source_usdc)
    if source <= 0: raise ValueError('Nonpositive buy')
    sizes = [decimal(x) for x in prior_buys]
    if any(x <= 0 for x in sizes): raise ValueError('Invalid baseline')
    baseline = median(sizes) if len(sizes) >= policy.min_baseline_buys else None
    # Insufficient history: observe the trade, but do not invent conviction.
    amount = decimal(policy.min_buy) if baseline is None else decimal(policy.normal_buy)*source/baseline
    amount = min(decimal(policy.max_buy), max(decimal(policy.min_buy), amount))
    return {'budget': str(amount.quantize(Decimal('0.01'))),
            'baseline': str(baseline) if baseline else None, 'samples': len(sizes),
            'confidence': 'LOW' if baseline is None else 'HIGH' if len(sizes) >= 10 else 'MEDIUM'}
