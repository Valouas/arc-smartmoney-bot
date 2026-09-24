from __future__ import annotations
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


class ProposalQueue:
    """One pending proposal per token/side; SQLite is authoritative, JSONL an export."""
    def __init__(self, storage, path='data/proposals.jsonl'):
        self.storage = storage
        self.path = Path(path)

    def add(self, signal):
        m = signal.risk.market if signal.risk else None
        identity = f'{signal.token.lower()}:{signal.side}:{signal.first_seen}'
        proposal = {
            'id':hashlib.sha256(identity.encode()).hexdigest()[:24],
            'created_at':int(time.time()), 'status':'PENDING_HUMAN_REVIEW',
            'side':signal.side,'token':signal.token.lower(),
            'symbol':getattr(m,'symbol','?'), 'reference_price_usd':getattr(m,'price_usd',None),
            'signal_score':signal.score,'cluster_wallets':[e.wallet for e in signal.events],
            'total_observed_usdc':signal.total_usdc,'market_url':getattr(m,'url',None),
        }
        return self.storage.save_proposal(proposal)

    def export(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,delete=False) as f:
                name = f.name
                for row in self.storage.db.execute('SELECT payload FROM proposals ORDER BY rowid'):
                    f.write(row[0]+'\n')
                f.flush(); os.fsync(f.fileno())
            os.replace(name,self.path)
        finally:
            if name and os.path.exists(name): os.unlink(name)
