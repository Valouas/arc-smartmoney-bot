"""Explicit, bounded public-RPC balance and token-price evidence for copy research.

Never called by copy_candidate_report; never reads API credentials or signs.
"""
import argparse
import json
import re
import time
from decimal import Decimal
from pathlib import Path
import requests

from rpc_transport import ResilientRPCProvider, SharedRateLimiter, RPCError

PUBLIC_RPC = 'https://rpc.mainnet.arc.io'
USDC = '0x3600000000000000000000000000000000000000'
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')


def uint256(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise ValueError('invalid_uint256_response')
    return int(value, 16)


def check_funds(candidates, rpc, record, *, target=30, maximum=120, clock=time.time,
                minimum_usd=10, token_value=None):
    """Require observed USDC plus priced tokens >= minimum; never infer an empty portfolio."""
    if not 1 <= target <= maximum <= 200:
        raise ValueError('invalid_scope')
    minimum = Decimal(str(minimum_usd))
    if not minimum.is_finite() or minimum <= 0:
        raise ValueError('invalid_minimum_usd')
    if len({w['wallet'].lower() for w in candidates}) != len(candidates):
        raise ValueError('duplicate_wallet')
    if any(not ADDRESS.fullmatch(w['wallet']) or not w.get('copy_research_eligible') for w in candidates):
        raise ValueError('invalid_research_candidates')
    def request(method, params):
        response = rpc.make_request(method, params)
        record({'kind': 'rpc_evidence', 'method': method, 'params': params,
                'response': response, 'received_at': clock()})
        if 'error' in response or 'result' not in response:
            raise ValueError('rpc_error_or_missing_result')
        return response['result']
    if request('eth_chainId', []) != '0x13b2':
        raise ValueError('wrong_chain')
    block = request('eth_getBlockByNumber', ['latest', False])
    block_number, block_hash = block['number'], block['hash']
    timestamp = int(block['timestamp'], 16)
    if not 0 <= clock()-timestamp <= 300:
        raise ValueError('stale_or_future_block')
    if uint256(request('eth_call', [{'to': USDC, 'data': '0x313ce567'}, block_number])) != 6:
        raise ValueError('wrong_usdc_decimals')
    results = []
    funded = 0
    for candidate in candidates[:maximum]:
        wallet = candidate['wallet'].lower()
        result = dict(wallet=wallet, block_number=block_number, block_hash=block_hash,
                      block_timestamp=timestamp, checked_at=clock(), asset='native_USDC_ERC20_view',
                      liquid_usdc=None, observed_value_usd=None, minimum_usd=str(minimum),
                      funds_status='UNKNOWN', live_signal_authority=False,
                      paper_selected=False)
        try:
            raw = request('eth_call', [{'to': USDC, 'data': '0x70a08231'+wallet[2:].rjust(64, '0')}, block_number])
            units = uint256(raw)
            usdc = Decimal(units)/Decimal(10**6)
            tokens, details = Decimal(0), []
            if usdc < minimum and token_value is not None:
                tokens, details = token_value(candidate, request, block_number, minimum-usdc)
                if not tokens.is_finite() or tokens < 0:
                    raise ValueError('invalid_token_value')
            value = usdc + tokens
            result.update(raw_balance=raw, liquid_usdc=str(usdc), observed_value_usd=str(value),
                          valued_tokens=details, funds_status='AT_LEAST_MINIMUM' if value >= minimum
                          else 'BELOW_MINIMUM_OBSERVED_PORTFOLIO_INCOMPLETE')
            funded += value >= minimum
        except (RPCError, ValueError, KeyError, TypeError):
            result['error'] = 'balance_unavailable_or_invalid'
        results.append(result)
        record({'kind': 'wallet_observation', **result})
        if funded >= target:
            break
    final = request('eth_getBlockByNumber', [block_number, False])
    if final['hash'] != block_hash:
        raise ValueError('block_changed_no_funding_confirmation')
    return dict(chain_id=5042, provider='public_rpc', block_number=block_number,
                block_hash=block_hash, block_timestamp=timestamp, checked=len(results),
                qualifying_wallets=funded, minimum_usd=str(minimum), observations=results, nansen_http_calls=0,
                nansen_credits_consumed=0, notice='Observed subset at a dated block with public token quotes; not complete portfolio, executable liquidation value or future funding')


def priced_token(pairs, token):
    """Price belongs to baseToken only. Liquidity is required, not a sellability guarantee."""
    choices = []
    if not isinstance(pairs, list):
        return None
    for p in pairs:
        try:
            if p['chainId'] != 'arc' or p['baseToken']['address'].lower() != token.lower():
                continue
            price, liquidity = Decimal(str(p['priceUsd'])), Decimal(str(p['liquidity']['usd']))
            if price.is_finite() and liquidity.is_finite() and price > 0 and liquidity > 0:
                choices.append((liquidity, price, p.get('pairAddress')))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return max(choices, key=lambda x:x[0]) if choices else None


def token_valuator(record, session, *, max_tokens=20):
    cache = {}
    def value(candidate, request, block, needed):
        total, details = Decimal(0), []
        tokens = list(dict.fromkeys(t.lower() for t in candidate.get('known_tokens', [])
                                   if isinstance(t, str) and ADDRESS.fullmatch(t)))
        for token in tokens[:max_tokens]:
            if token in (USDC, '0xfffffffffffffffffffffffffffffffffffffffe'):
                continue
            entry = {'token': token, 'status': 'UNKNOWN'}
            try:
                raw = request('eth_call', [{'to':token,'data':'0x70a08231'+candidate['wallet'][2:].rjust(64,'0')},block])
                units = uint256(raw)
                entry['raw_balance'] = raw
                if units == 0:
                    entry['status'] = 'ZERO_TOKEN_BALANCE'
                    details.append(entry)
                    continue
                decimals = uint256(request('eth_call', [{'to':token,'data':'0x313ce567'},block]))
                if decimals > 36:
                    raise ValueError('unsupported_decimals')
                if token not in cache:
                    time.sleep(.21)
                    with session.get('https://api.dexscreener.com/tokens/v1/arc/'+token,
                                     timeout=10, allow_redirects=False) as response:
                        response.raise_for_status()
                        body = response.json()
                    record({'kind':'price_evidence','provider':'dexscreener','token':token,
                            'response':body,'received_at':time.time()})
                    cache[token] = priced_token(body,token)
                quote = cache[token]
                if quote is None:
                    raise ValueError('no_price')
                liquidity, price, pair = quote
                amount = Decimal(units)/Decimal(10**decimals)
                estimate = amount*price
                entry.update(status='VALUED',decimals=decimals,amount=str(amount),price_usd=str(price),
                             value_usd=str(estimate),liquidity_usd=str(liquidity),pair=pair)
                total += estimate
            except (RPCError, requests.RequestException, ValueError, KeyError, TypeError, ArithmeticError):
                entry['error'] = 'token_balance_or_price_unavailable'
            details.append(entry)
            if total >= needed:
                break
        return total, details
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', required=True, help='JSON array of scored candidate rows, already in rank order')
    parser.add_argument('--output', required=True, help='New JSON evidence file; existing files never replaced')
    parser.add_argument('--target', type=int, default=30)
    parser.add_argument('--maximum', type=int, default=120)
    parser.add_argument('--minimum-usd', type=Decimal, default=Decimal(10))
    args = parser.parse_args()
    rows = json.loads(Path(args.candidates).read_text(encoding='utf-8'))
    output = Path(args.output)
    if output.exists():
        parser.error('output already exists; evidence is never overwritten')
    ledger_path = output.with_suffix('.jsonl')
    with ledger_path.open('x', encoding='utf-8') as ledger:
        def record(value):
            ledger.write(json.dumps(value, allow_nan=False)+'\n')
            ledger.flush()
            if value.get('kind') == 'wallet_observation':
                print(value['wallet'], value['funds_status'], value['liquid_usdc'], flush=True)
        limiter = SharedRateLimiter('data/copy_funds_public_rpc_budget.sqlite3', 1)
        rpc = ResilientRPCProvider(PUBLIC_RPC, 5042, limiter, cache=False)
        prices = requests.Session()
        try:
            result = check_funds(rows, rpc, record, target=args.target, maximum=args.maximum,
                                 minimum_usd=args.minimum_usd, token_value=token_valuator(record, prices))
        finally:
            rpc.session.close()
            prices.close()
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(f"Checked: {result['checked']} | Value >= minimum: {result['qualifying_wallets']} | Nansen calls: 0")


if __name__ == '__main__':
    main()
