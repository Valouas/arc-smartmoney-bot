"""Recover at most five exact-block, reviewed-emitter proofs; no history changes."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import time

from copy_candidate_report import ADDRESS
from copy_holding_campaign import CampaignBudget, connect, snapshot
from copy_holding_pilot import save_new, PilotLimit
from copy_holding_transport import AlchemyCampaignSession, CampaignRPCProvider, require_alchemy_arc
from dex_registry import TrustedDexRegistry
from process_lock import ProcessLock
from rpc_transport import SharedRateLimiter, RPCError


def validate_plan(plan, db, settings, registry):
    allowed = settings.get('paid_rpc', {}).get('wallets', [])
    if not isinstance(plan, list) or not 1 <= len(plan) <= 5:
        raise ValueError('invalid_proof_count')
    seen = set()
    for entry in plan:
        wallet = entry.get('wallet', '')
        params = entry.get('params')
        if (not ADDRESS.fullmatch(wallet) or wallet not in allowed or
                entry.get('method') != 'eth_getCode' or not isinstance(params, list) or len(params) != 2 or
                params[0] not in registry.entries or not isinstance(params[1], str) or
                not params[1].startswith('0x')):
            raise ValueError('proof_outside_authorized_scope')
        block = int(params[1], 16)
        row = db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND tx_hash=?',
                         (wallet, entry.get('tx_hash'))).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        if payload.get('block') != block or not payload.get('error'):
            raise ValueError('proof_not_for_stored_failed_transaction')
        key = tuple(params)
        if key in seen:
            raise ValueError('duplicate_proof')
        seen.add(key)
    return plan


class ProofBudget(CampaignBudget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proof_attempts = 0

    def take_paid(self, method):
        if method not in {'eth_chainId', 'eth_getCode'} or self.proof_attempts >= 20:
            raise PilotLimit()
        sequence = super().take_paid(method)
        self.proof_attempts += 1
        return sequence


def recover(campaign, plan_path, output, cfg):
    campaign, output = Path(campaign), Path(output)
    require_alchemy_arc(cfg.rpc_url)
    plan = json.loads(Path(plan_path).read_text())
    settings = json.loads((campaign/'settings.json').read_text())
    if not settings.get('paid_rpc', {}).get('enabled'):
        raise ValueError('paid_provider_not_enabled')
    registry = TrustedDexRegistry(cfg.trusted_dex_registry, 5042)
    with ProcessLock(campaign/'worker.lock'), closing(connect(campaign/'campaign.sqlite3')) as db:
        validate_plan(plan, db, settings, registry)
        output.mkdir(parents=True, exist_ok=False)
        snapshot(db, output, 'before_proofs')
        save_new(output/'plan.json', plan)
        budget = ProofBudget(campaign, db, settings)
        budget.directory = output
        budget.deadline = min(budget.deadline, time.monotonic()+120)
        before = list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
        results = []
        session = AlchemyCampaignSession(budget, cfg.rpc_url)
        provider = CampaignRPCProvider(cfg.rpc_url, 5042, SharedRateLimiter(cfg.rpc_rate_limit_file, 1), session=session)
        try:
            for entry in plan:
                budget.wallet = entry['wallet']
                try:
                    response = provider.make_request('eth_getCode', entry['params'])
                    code = bytes.fromhex(response['result'].removeprefix('0x'))
                    result = dict(entry, executed=True, code=response['result'],
                                  reviewed_hash_matches=registry.trusts(entry['params'][0], code),
                                  code_sha256=hashlib.sha256(code).hexdigest())
                except RPCError as error:
                    result = dict(entry, executed=True, error=error.category)
                results.append(result)
                save_new(output/f'proof_{len(results):02d}.json', result)
        finally:
            session.close()
            after = list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
            save_new(output/'summary.json', dict(results=results, before=before, after=after,
                attempts=after[0]-before[0], reserved_cu=after[1]-before[1],
                history_changed=False, paper_selected=False, live_authority=False))
            files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
            save_new(output/'manifest.json', files)
    return dict(proofs=len(results),matching=sum(r.get('reviewed_hash_matches',False) for r in results),
                attempts=after[0]-before[0],reserved_cu=after[1]-before[1])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign',required=True);p.add_argument('--plan',required=True);p.add_argument('--output',required=True)
    a=p.parse_args()
    from config import Settings
    print(json.dumps(recover(a.campaign,a.plan,a.output,Settings())))


if __name__=='__main__': main()
