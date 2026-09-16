from __future__ import annotations
import time
from collections import defaultdict, deque
from models import TradeEvent, ClusterSignal

class ClusterBook:
    def __init__(self, buy_window: int, sell_window: int):
        self.buy_window = buy_window
        self.sell_window = sell_window
        self.events = defaultdict(lambda: {"BUY": deque(), "SELL": deque()})

    def add(self, event: TradeEvent) -> ClusterSignal:
        now = event.timestamp or int(time.time())
        q = self.events[event.token][event.side]
        q.append(event)
        window = self.buy_window if event.side == "BUY" else self.sell_window
        cutoff = now - window
        while q and q[0].timestamp < cutoff:
            q.popleft()

        latest = {}
        for e in q:
            latest[e.wallet.lower()] = e
        events = list(latest.values())

        eligible = [e for e in events if e.confirmation_eligible and e.wallet_weight > 0]
        by_identity = {}
        for e in eligible:
            identity = (e.identity_id or e.wallet).lower()
            previous = by_identity.get(identity)
            if previous is None or e.wallet_weight > previous.wallet_weight:
                by_identity[identity] = e

        independent_events = list(by_identity.values())
        return ClusterSignal(
            token=event.token,
            side=event.side,
            events=events,
            distinct_wallets=len(events),
            independent_identities=len(independent_events),
            ignored_wallets=len(events) - len(eligible),
            cluster_weight=sum(e.wallet_weight for e in independent_events),
            total_usdc=sum(e.usdc_amount for e in events),
            first_seen=min(e.timestamp for e in events),
            last_seen=max(e.timestamp for e in events),
        )
