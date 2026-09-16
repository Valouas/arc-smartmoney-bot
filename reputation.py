from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import json, time

@dataclass
class WalletReputation:
    address: str
    signals: int = 0
    early_winners: int = 0
    validated_winners: int = 0
    failed_signals: int = 0
    exits_observed: int = 0
    profitable_exits: int = 0
    avg_entry_rank: float | None = None
    avg_return_pct: float | None = None
    last_seen: int = 0

    @property
    def hit_rate(self) -> float:
        denom = self.early_winners + self.failed_signals
        return self.early_winners / denom if denom else 0.0

    @property
    def exit_hit_rate(self) -> float:
        return self.profitable_exits / self.exits_observed if self.exits_observed else 0.0

    @property
    def score(self) -> int:
        hit = (self.early_winners + 1.5) / (self.early_winners + self.failed_signals + 4.0)
        breadth = min(1.0, self.signals / 12.0)
        winners = min(1.0, self.early_winners / 5.0)
        exit_quality = self.exit_hit_rate
        raw = 100 * (0.45 * hit + 0.20 * breadth + 0.25 * winners + 0.10 * exit_quality)
        return int(max(0, min(100, round(raw))))

    @property
    def dynamic_weight(self) -> int:
        s = self.score
        if s >= 80: return 4
        if s >= 65: return 3
        if s >= 45: return 2
        return 1

class ReputationBook:
    def __init__(self, path="data/reputation.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, WalletReputation] = {}
        self.load()

    def load(self):
        if not self.path.exists(): return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for addr, row in raw.items():
                self.items[addr.lower()] = WalletReputation(**row)
        except Exception:
            self.items = {}

    def save(self):
        payload = {a: asdict(r) for a, r in self.items.items()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, address: str) -> WalletReputation:
        a = address.lower()
        if a not in self.items:
            self.items[a] = WalletReputation(address=a)
        return self.items[a]

    def effective_weight(self, address: str, seed_weight: int) -> int:
        r = self.get(address)
        if r.signals < 3: return seed_weight
        return max(1, min(4, round((seed_weight + r.dynamic_weight) / 2)))

    def mark_signal(self, address: str):
        r = self.get(address); r.signals += 1; r.last_seen = int(time.time())

    def mark_known_winner(self, address: str, entry_rank: int | None = None):
        r = self.get(address); r.early_winners += 1
        if entry_rank is not None:
            if r.avg_entry_rank is None: r.avg_entry_rank = float(entry_rank)
            else:
                n = max(1, r.early_winners)
                r.avg_entry_rank += (entry_rank - r.avg_entry_rank) / n
        r.last_seen = int(time.time())

    def mark_failed_signal(self, address: str):
        r = self.get(address); r.failed_signals += 1; r.last_seen = int(time.time())

    def mark_exit(self, address: str, profitable: bool):
        r = self.get(address); r.exits_observed += 1
        if profitable: r.profitable_exits += 1
        r.last_seen = int(time.time())
