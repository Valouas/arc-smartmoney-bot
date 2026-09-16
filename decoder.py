from __future__ import annotations
from collections import defaultdict
from web3 import Web3
from rpc import TRANSFER_TOPIC, USDC_ERC20, NATIVE_MIRROR

SWAP_TOPICS = {
    Web3.keccak(text=s).hex().removeprefix("0x").lower()
    for s in ("Swap(address,uint256,uint256,uint256,uint256,address)",
              "Swap(address,address,int256,int256,uint160,uint128,int24)",
              "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
}

def hex_value(value):
    return (value.hex() if hasattr(value, "hex") else str(value)).removeprefix("0x").lower()

def swap_connected(receipt, token, wallet):
    """Require a token transfer path between the wallet and a swap-emitting pool.

    This supports router intermediaries but is evidence, not proof of economic intent.
    Unknown DEX event ABIs remain research-only.
    """
    pools = {str(log["address"]).lower() for log in receipt.get("logs", [])
             if log.get("topics") and hex_value(log["topics"][0]) in SWAP_TOPICS}
    edges = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if str(log.get("address", "")).lower() != token.lower() or len(topics) != 3:
            continue
        if hex_value(topics[0]) != hex_value(TRANSFER_TOPIC):
            continue
        edges.append((topic_to_address(topics[1]).lower(), topic_to_address(topics[2]).lower()))
    reachable = {wallet.lower()}
    for _ in range(len(edges)):
        for a, b in edges:
            if a in reachable or b in reachable:
                reachable.update((a, b))
    return bool(pools & reachable)

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
        if len(topics) != 3:
            continue
        t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        if hex_value(t0) != hex_value(TRANSFER_TOPIC):
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

def classify_trade(rpc, receipt, wallet: str, min_usdc_flow: float, native_value_wei: int = 0, strict=False):
    if int(receipt.get("status", 1)) != 1:
        return []
    deltas = wallet_token_deltas(receipt, wallet)
    usdc_raw = deltas.get(USDC_ERC20, 0)
    erc20_usdc = usdc_raw / 1_000_000.0
    native_usdc_spent = max(0, int(native_value_wei or 0)) / 1e18
    mirrored = NATIVE_MIRROR in deltas
    if mirrored:
        native_net = deltas[NATIVE_MIRROR] / 1e18
        if usdc_raw and abs(native_net - erc20_usdc) > 0.000001:
            if strict: raise ValueError("conflicting USDC representations")
            return []
        erc20_usdc = native_net
        native_usdc_spent = 0.0  # Mirror gives net movement, including refunds.
    usdc = erc20_usdc
    # msg.value is gross, not net: refunds/internal payments need traces. Do not
    # sum two interfaces of the same balance or invent a mixed-flow cost basis.
    if native_usdc_spent and erc20_usdc:
        if strict: raise ValueError("mixed USDC flow needs trace accounting")
        return []
    total_spend = max(0.0, -erc20_usdc) or native_usdc_spent

    if max(abs(erc20_usdc), native_usdc_spent) < min_usdc_flow:
        if strict and not erc20_usdc and any(
                log.get("topics") and hex_value(log["topics"][0]) in SWAP_TOPICS
                for log in receipt.get("logs", [])):
            raise ValueError("swap without observable USDC cost basis")
        return []

    non_usdc = {t: v for t, v in deltas.items() if t not in (USDC_ERC20, NATIVE_MIRROR) and v}
    if len(non_usdc) != 1:
        if strict and non_usdc: raise ValueError("unattributable basket flow")
        return []  # Basket swaps / LP operations have no attributable per-token price.
    events = []
    for token, raw_delta in deltas.items():
        if token in (USDC_ERC20, NATIVE_MIRROR) or raw_delta == 0:
            continue
        meta = rpc.token_meta(token)
        decimals = meta.get("decimals")
        if not isinstance(decimals, int) or not 0 <= decimals <= 255:
            raise ValueError("token decimals unavailable")
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
            "confirmation_eligible": not native_usdc_spent and swap_connected(receipt, token, wallet),
        })
    return events
