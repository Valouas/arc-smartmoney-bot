"""Bounded research of pools seen in the authorized Top25. Never approves emitters."""
import argparse
from collections import Counter
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import time

from web3 import Web3
from copy_ambiguity_audit import cached, readonly
from copy_holding_campaign import CampaignBudget, connect, snapshot
from copy_holding_pilot import PilotLimit, save_new
from copy_holding_transport import AlchemyCampaignSession, CampaignRPCProvider, require_alchemy_arc
from decoder import SWAP_TOPICS, hex_value
from process_lock import ProcessLock
from rpc_transport import RPCError, SharedRateLimiter

# Reviewed against the official deployment record on 2026-09-22. This is a
# research anchor, NOT a modification of the engine's trusted_dex registry.
FACTORY='0xf0db7b58379503491d857db50ac9ece64c653918'
SOURCE='https://raw.githubusercontent.com/Uniswap/contracts/main/deployments/json/5042.json'
SELECTORS={s:Web3.to_hex(Web3.keccak(text=s)[:4]) for s in
           ('factory()','token0()','token1()','fee()','getPool(address,address,uint24)')}


def address_word(value):
    if not isinstance(value,str) or len(value)!=66 or not value.startswith('0x') or int(value[2:26],16):
        raise ValueError('malformed_address_result')
    int(value[26:],16)
    result='0x'+value[26:].lower()
    if int(result,16)==0:raise ValueError('zero_address')
    return result


def attest(provider, entry):
    pool,block=entry['pool'],hex(entry['block'])
    def call(to,data):return provider.make_request('eth_call',[dict(to=to,data=data),block])['result']
    factory=address_word(call(pool,SELECTORS['factory()']))
    if factory!=FACTORY:return dict(status='DIFFERENT_FACTORY',reported_factory=factory)
    token0=address_word(call(pool,SELECTORS['token0()']))
    token1=address_word(call(pool,SELECTORS['token1()']))
    if int(token0,16)>=int(token1,16):raise ValueError('invalid_token_order')
    raw_fee=call(pool,SELECTORS['fee()'])
    if not isinstance(raw_fee,str) or len(raw_fee)!=66 or not raw_fee.startswith('0x'):
        raise ValueError('invalid_fee')
    fee=int(raw_fee,16)
    if not 0<fee<2**24:raise ValueError('invalid_fee')
    data=SELECTORS['getPool(address,address,uint24)']+token0[2:].zfill(64)+token1[2:].zfill(64)+hex(fee)[2:].zfill(64)
    registered=address_word(call(FACTORY,data))
    if registered!=pool:return dict(status='FACTORY_MEMBERSHIP_MISMATCH',registered=registered)
    response=provider.make_request('eth_getCode',[pool,block])['result']
    code=bytes.fromhex(response.removeprefix('0x'))
    if not code:raise ValueError('empty_pool_code')
    return dict(status='FACTORY_MEMBERSHIP_OBSERVED',factory=factory,token0=token0,token1=token1,fee=fee,
                runtime_code_hash=Web3.to_hex(Web3.keccak(code)),code=response,
                limitation='Exact-block membership evidence only; code/route review still required.')


class ProbeBudget(CampaignBudget):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.probe_attempts=0
    def take_paid(self,method):
        if method not in {'eth_chainId','eth_getCode','eth_call'} or self.probe_attempts>=700:raise PilotLimit()
        n=super().take_paid(method);self.probe_attempts+=1;return n


def build_plan(audit,db,caches,allowed):
    entries={}
    for row in audit['rows']:
        wallet=row['wallet']
        if wallet not in allowed:raise ValueError('wallet_outside_paid_scope')
        for issue in row['issue_details']:
            for pool in issue.get('unreviewed_emitters',[]):
                if pool in entries:continue
                h=issue['tx_hash']
                stored=db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND tx_hash=?',(wallet,h)).fetchone()
                payload=json.loads(stored[0]) if stored and stored[0] else {}
                receipt=cached(caches,'eth_getTransactionReceipt',[h])
                if (not receipt or type(payload.get('block')) is not int or
                        int(receipt['blockNumber'],16)!=payload['block'] or not any(
                        str(l['address']).lower()==pool and l.get('topics') and hex_value(l['topics'][0]) in SWAP_TOPICS
                        for l in receipt['logs'])):
                    raise ValueError('missing_stored_emitter_evidence')
                entries[pool]=dict(wallet=wallet,tx_hash=h,pool=pool,block=payload['block'])
    if not 1<=len(entries)<=100:raise ValueError('invalid_pool_scope')
    return list(entries.values())


def run(campaign,audit_path,output,cfg):
    campaign,output=Path(campaign),Path(output)
    require_alchemy_arc(cfg.rpc_url)
    settings=json.loads((campaign/'settings.json').read_text())
    if not settings.get('paid_rpc',{}).get('enabled'):raise ValueError('paid_not_enabled')
    with ProcessLock(campaign/'worker.lock'),closing(connect(campaign/'campaign.sqlite3')) as db:
        with ExitStack() as stack:
            paths=[campaign/'rpc_cache.sqlite3',Path(cfg.rpc_cache_file),
                   Path('outputs/holding_pilot_mainnet_20260922/rpc_cache.sqlite3'),
                   Path('outputs/holding_pilot_live_20260922/rpc_cache.sqlite3')]
            caches=[stack.enter_context(closing(readonly(p))) for p in paths if p.exists()]
            plan=build_plan(json.loads(Path(audit_path).read_text()),db,caches,settings['paid_rpc']['wallets'])
        output.mkdir(parents=True,exist_ok=False)
        snapshot(db,output,'before_pool_probes')
        save_new(output/'plan.json',dict(entries=plan,official_factory=FACTORY,source=SOURCE,
            max_attempts=700,max_reserved_usd=0.07,engine_authority=False))
        budget=ProbeBudget(campaign,db,settings);budget.directory=output
        budget.deadline=min(budget.deadline,time.monotonic()+1200)
        before=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
        session=AlchemyCampaignSession(budget,cfg.rpc_url)
        provider=CampaignRPCProvider(cfg.rpc_url,5042,SharedRateLimiter(cfg.rpc_rate_limit_file,1),session=session)
        results=[];stop='INTERRUPTED'
        try:
            for entry in plan:
                if (output/'STOP').exists():stop='STOP_REQUESTED';break
                budget.wallet=entry['wallet']
                try:result=dict(entry,**attest(provider,entry))
                except PilotLimit:stop='LIMIT';break
                except RPCError as e:result=dict(entry,status='RPC_FAILURE',category=e.category)
                except (ValueError,KeyError,TypeError):result=dict(entry,status='INVALID_POOL_RESPONSE')
                result.update(engine_authority=False,paper_selected=False)
                results.append(result);save_new(output/f'pool_{len(results):03d}.json',result)
                print(json.dumps(dict(done=len(results),total=len(plan),status=result['status'])),flush=True)
            else:stop='COMPLETE'
        finally:
            session.close()
            after=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
            save_new(output/'summary.json',dict(stop=stop,done=len(results),planned=len(plan),
                statuses=dict(Counter(r['status'] for r in results)),before=before,after=after,
                attempts=after[0]-before[0],reserved_cu=after[1]-before[1],history_changed=False,
                engine_authority=False,paper_selected=False))
            files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
            save_new(output/'manifest.json',files)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign',required=True);p.add_argument('--audit',required=True);p.add_argument('--output',required=True)
    a=p.parse_args()
    from config import Settings
    run(a.campaign,a.audit,a.output,Settings())


if __name__=='__main__':main()
