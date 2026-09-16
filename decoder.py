from __future__ import annotations
from collections import defaultdict
from web3 import Web3
from rpc import TRANSFER_TOPIC, USDC_ERC20

def topic_to_address(topic) -> str:
    h = topic.hex() if hasattr(topic, "hex") else str(topic)
    h = h[2:] if h.startswith("0x") else h
    return Web3.to_checksum_address("0x" + h[-40:])

def data_to_int(data) -> int:
    if isinstance(data, (bytes, bytearray)):
        return int.from_bytes(data, "big")
    s = data.hex() if hasattr(data, "hex") else str(data)
    return int(s, 16)

def wallet_token_deltas(receipt, wallet: str) -> dict[str, int]:
    """Net ERC-20 Transfer deltas for one wallet inside one receipt."""
    wallet = Web3.to_checksum_address(wallet)
    deltas: dict[str, int] = defaultdict(int)

    for log in receipt["logs"]:
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        if t0.lower() != TRANSFER_TOPIC.lower():
            continue
        try:
            frm = topic_to_address(topics[1])
            to = topic_to_address(topics[2])
            amount = data_to_int(log["data"])
            token = Web3.to_checksum_address(log["address"])
        except Exception:
            continue
        if frm.lower() == wallet.lower():
            deltas[token] -= amount
        if to.lower() == wallet.lower():
            deltas[token] += amount
    return dict(deltas)

def classify_trade(rpc, receipt, wallet: str, min_usdc_flow: float, native_value_wei: int = 0):
    deltas = wallet_token_deltas(receipt, wallet)
    usdc_raw = deltas.get(USDC_ERC20, 0)
    erc20_usdc = usdc_raw / 1_000_000.0
    native_usdc_spent = max(0, int(native_value_wei or 0)) / 1e18
    usdc = erc20_usdc
    total_spend = max(0.0, -erc20_usdc) + native_usdc_spent

    if max(abs(erc20_usdc), native_usdc_spent) < min_usdc_flow:
        return []

    events = []
    for token, raw_delta in deltas.items():
        if token.lower() == USDC_ERC20.lower() or raw_delta == 0:
            continue
        meta = rpc.token_meta(token)
        decimals = int(meta.get("decimals", 18))
        amount = raw_delta / (10 ** decimals)

        if amount > 0 and (usdc < 0 or native_usdc_spent >= min_usdc_flow):
            side = "BUY"
            observed_usdc = total_spend
        elif amount < 0 and usdc > 0:
            side = "SELL"
            observed_usdc = usdc
        else:
            continue

        events.append({
            "side": side,
            "token": token,
            "symbol": str(meta.get("symbol", "?")),
            "token_amount": abs(amount),
            "usdc_amount": abs(observed_usdc),
        })
    return events
