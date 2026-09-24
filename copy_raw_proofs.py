"""Fill a small, explicit raw-evidence gap in authorized Top25 research only."""
import argparse
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import time

from copy_ambiguity_audit import cached, readonly
from copy_holding_campaign import CampaignBudget, connect, snapshot
from copy_holding_duration import ADDRESS, HASH
from copy_holding_pilot import save_new, PilotLimit
from copy_holding_transport import AlchemyCampaignSession, CampaignRPCProvider, require_alchemy_arc
from process_lock import ProcessLock
from rpc_cache import RPCCache
from rpc_transport import SharedRateLimiter, RPCError, _valid_result, _matches_request

METHODS={'eth_getTransactionByHash','eth_getTransactionReceipt'}


def validate_plan(plan,db,allowed,caches):
    if not isinstance(plan,list) or not 1<=len(plan)<=150:
        raise ValueError('invalid_raw_proof_count')
    seen=set()
    for e in plan:
        method,h=e.get('method'),e.get('tx_hash','')
        wallets=e.get('wallets');block=e.get('block')
        if (method not in METHODS or not HASH.fullmatch(h) or
            not isinstance(wallets,list) or not wallets or len(wallets)>25 or
            type(block) is not int or block<0 or (method,h) in seen):
            raise ValueError('invalid_raw_proof')
        for wallet in wallets:
            if not isinstance(wallet,str) or not ADDRESS.fullmatch(wallet) or wallet not in allowed:
                raise ValueError('wallet_outside_paid_scope')
            row=db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND tx_hash=?',(wallet,h)).fetchone()
            p=json.loads(row[0]) if row and row[0] else {}
            if p.get('block')!=block or p.get('tx_hash')!=h or p.get('outside_snapshot'):
                raise ValueError('proof_not_in_stored_snapshot')
        if cached(caches,method,[h]) is not None:
            raise ValueError('raw_proof_already_available')
        seen.add((method,h))
    return plan


class RawProofBudget(CampaignBudget):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.raw_attempts=0
    def take_paid(self,method):
        if method not in METHODS|{'eth_chainId'} or self.raw_attempts>=450:raise PilotLimit()
        seq=super().take_paid(method);self.raw_attempts+=1;return seq


def recover(campaign,plan_path,output,cfg,cache_paths):
    campaign,output=Path(campaign),Path(output)
    require_alchemy_arc(cfg.rpc_url)
    settings=json.loads((campaign/'settings.json').read_text())
    if not settings.get('paid_rpc',{}).get('enabled'):raise ValueError('paid_not_enabled')
    plan=json.loads(Path(plan_path).read_text())
    with ProcessLock(campaign/'worker.lock'),closing(connect(campaign/'campaign.sqlite3')) as db:
        with ExitStack() as stack:
            caches=[stack.enter_context(closing(readonly(p))) for p in cache_paths if Path(p).exists()]
            validate_plan(plan,db,settings['paid_rpc']['wallets'],caches)
        output.mkdir(parents=True,exist_ok=False)
        snapshot(db,output,'before_raw_proofs');save_new(output/'plan.json',plan)
        cache=RPCCache(output/'rpc_cache.sqlite3')
        budget=RawProofBudget(campaign,db,settings);budget.directory=output
        budget.deadline=min(budget.deadline,time.monotonic()+900)
        before=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
        session=AlchemyCampaignSession(budget,cfg.rpc_url)
        provider=CampaignRPCProvider(cfg.rpc_url,5042,SharedRateLimiter(cfg.rpc_rate_limit_file,1),session=session)
        results=[];stop='INTERRUPTED'
        try:
            for e in plan:
                if (output/'STOP').exists():stop='STOP_REQUESTED';break
                budget.wallet=e['wallets'][0];m=e['method'];params=[e['tx_hash']]
                try:
                    value=provider.make_request(m,params)['result']
                    if (not _valid_result(m,value) or not _matches_request(m,params,value) or
                        int(value['blockNumber'],16)!=e['block']):raise ValueError('raw_snapshot_mismatch')
                    # Isolated research cache, never the production RPC cache.
                    cache.put(5042,m,params,value,e['block'])
                    result=dict(e,status='RAW_PROOF_AVAILABLE',result=value)
                except PilotLimit:stop='LIMIT';break
                except RPCError as exc:result=dict(e,status='RPC_FAILURE',category=exc.category)
                except (ValueError,TypeError,KeyError):result=dict(e,status='INVALID_RAW_RESPONSE')
                results.append(result);save_new(output/f'proof_{len(results):03d}.json',result)
                print(json.dumps(dict(done=len(results),total=len(plan),status=result['status'])),flush=True)
            else:stop='COMPLETE'
        finally:
            session.close()
            after=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
            save_new(output/'summary.json',dict(stop=stop,done=len(results),planned=len(plan),
                available=sum(r['status']=='RAW_PROOF_AVAILABLE' for r in results),before=before,after=after,
                attempts=after[0]-before[0],reserved_cu=after[1]-before[1],history_changed=False,
                paper_selected=False,live_authority=False,nansen_calls=0))
            save_new(output/'manifest.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('campaign','plan','output'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--cache',action='append',default=[])
    a=parser.parse_args()
    from config import Settings
    cfg=Settings()
    recover(a.campaign,a.plan,a.output,cfg,[Path(a.campaign)/'rpc_cache.sqlite3',Path(cfg.rpc_cache_file),*a.cache])
