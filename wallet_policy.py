"""Reviewed DEX infrastructure is swap evidence, never smart-money identity.

No code/EOA heuristic: an unlisted smart account is unaffected. Exclusion uses
the listed addresses, independently of whether their bytecode currently matches
the hash required by TrustedDexRegistry.trusts().
"""
import json
from functools import lru_cache
from pathlib import Path

from web3 import Web3


class InfrastructureRegistryError(RuntimeError):
    pass


class ExcludedInfrastructureError(ValueError):
    pass


@lru_cache(maxsize=8)
def _read_emitters(path, chain_id, mtime_ns, size):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or data.get('chain_id') != chain_id:
            raise ValueError('Wrong registry chain')
        rows = data['emitters']
        if not isinstance(rows, list):
            raise ValueError('Invalid emitters')
        if any(not isinstance(row, dict) or not isinstance(row.get('address'), str) for row in rows):
            raise ValueError('Invalid emitter address')
        return frozenset(Web3.to_checksum_address(row['address']).lower() for row in rows)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise InfrastructureRegistryError('Invalid DEX infrastructure registry; wallet authority blocked') from None


def infrastructure_addresses(cfg=None):
    if cfg is None:
        from config import SETTINGS
        cfg = SETTINGS
    path = Path(cfg.trusted_dex_registry).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        # The shipped empty registry is optional; no addresses are inferred.
        return frozenset()
    except OSError:
        raise InfrastructureRegistryError('DEX infrastructure registry unavailable') from None
    return _read_emitters(str(path), cfg.chain_id, stat.st_mtime_ns, stat.st_size)


def is_dex_infrastructure(wallet, cfg=None):
    return str(wallet).lower() in infrastructure_addresses(cfg)


def require_smart_money_wallet(wallet, cfg=None):
    if is_dex_infrastructure(wallet, cfg):
        raise ExcludedInfrastructureError('Reviewed DEX infrastructure cannot be a smart-money wallet')
