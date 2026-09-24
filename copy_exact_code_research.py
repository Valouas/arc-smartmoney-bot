"""Resumable exact-block code evidence for already-collected Top25 issues only."""
import argparse
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import time

from eth_abi import encode
from web3 import Web3
from copy_ambiguity_audit import readonly, cached
from copy_holding_campaign import CampaignBudget, connect, snapshot
from copy_holding_pilot import save_new, PilotLimit
from copy_holding_transport import AlchemyCampaignSession, CampaignRPCProvider, require_alchemy_arc
from copy_pool_provenance import FACTORY
from process_lock import ProcessLock
from rpc_transport import RPCError, SharedRateLimiter

INIT_HASH=bytes.fromhex('e34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54')
SOURCE='https://raw.githubusercontent.com/Uniswap/v3-periphery/main/contracts/libraries/PoolAddress.sol'


def canonical_pool(proof):
    """Only strengthen existing reciprocal factory evidence, never self-certify."""
    try:
        if proof.get('status')!='FACTORY_MEMBERSHIP_OBSERVED' or proof['factory']!=FACTORY:return False
        t0,t1,fee=proof['token0'],proof['token1'],proof['fee']
        if not 0<int(t0,16)<int(t1,16) or type(fee) is not int or not 0<fee<1000000:return False
        salt=Web3.keccak(encode(['address','address','uint24'],[t0,t1,fee]))
        address=Web3.to_hex(Web3.keccak(b'\xff'+bytes.fromhex(FACTORY[2:])+salt+INIT_HASH)[12:])
        code=bytes.fromhex(proof['code'].removeprefix('0x'))
        return bool(code and address==proof['pool'] and Web3.to_hex(Web3.keccak(code))==proof['runtime_code_hash'])
    except (ValueError,TypeError,KeyError):return False


def code_diagnostic(code):
    """Opcode inventory only, not an ERC20 verdict or permission to ignore logs."""
    raw=bytes.fromhex(code.removeprefix('0x'));ops=set();i=0
    while i<len(raw):
        op=raw[i];ops.add(op);n=op-0x5f if 0x60<=op<=0x7f else 0
        if i+1+n>len(raw):return dict(status='TRUNCATED_PUSH',code_hash=Web3.to_hex(Web3.keccak(raw)))
        i+=1+n
    # Inspect whole bytecode (including metadata), conservatively: metadata can
    # cause false negatives, never an incorrect claim of absent instructions.
    dangerous={0x54,0x55,0x5c,0x5d,0xf0,0xf1,0xf2,0xf4,0xf5,0xfa,0xff}
    return dict(status='NO_STORAGE_OR_CALL_OPCODES' if raw and not ops&dangerous else 'REQUIRES_SEMANTIC_REVIEW',
                code_hash=Web3.to_hex(Web3.keccak(raw)),size=len(raw),
                opcode_inventory=sorted(hex(o) for o in ops&dangerous),automatic_exclusion=False)


def prepare(campaign,audit_path,pool_dir,out,cfg,code_proofs=()):
    campaign,out,pool_dir=Path(campaign),Path(out),Path(pool_dir)
    settings=json.loads((campaign/'settings.json').read_text())
    allowed=settings.get('paid_rpc',{}).get('wallets',[])
    audit=json.loads(Path(audit_path).read_text())
    pools={};stored={}
    hashes=json.loads((pool_dir/'manifest.json').read_text())
    for path in pool_dir.glob('pool_*.json'):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=hashes.get(path.name):raise ValueError('pool_proof_digest_mismatch')
        p=json.loads(path.read_text())
        if canonical_pool(p):
            pools[p['pool']]=p
            stored[(p['pool'],p['block'])]=(p['code'],str(path),hashes[path.name])
    for path in map(Path,code_proofs):
        proof=json.loads(path.read_text())
        params=proof.get('params',[])
        if (proof.get('method')!='eth_getCode' or len(params)!=2 or proof.get('wallet') not in allowed or
                proof.get('error') or proof.get('response',{}).get('error')):raise ValueError('invalid_existing_code_proof')
        code=proof['response']['result'];bytes.fromhex(code.removeprefix('0x'))
        key=params[0],int(params[1],16)
        if key in stored and stored[key][0]!=code:raise ValueError('conflicting_existing_codes')
        stored[key]=(code,str(path),hashlib.sha256(path.read_bytes()).hexdigest())
    entries={}
    with ExitStack() as stack:
        db=stack.enter_context(closing(readonly(campaign/'campaign.sqlite3')))
        paths=[campaign/'rpc_cache.sqlite3',Path(cfg.rpc_cache_file),
               Path('outputs/holding_pilot_mainnet_20260922/rpc_cache.sqlite3'),Path('outputs/holding_pilot_live_20260922/rpc_cache.sqlite3')]
        caches=[stack.enter_context(closing(readonly(p))) for p in paths if p.exists()]
        for row in audit['rows']:
            w=row['wallet']
            if w not in allowed:raise ValueError('wallet_outside_paid_scope')
            for issue in row['issue_details']:
                h=issue['tx_hash']
                record=db.execute('SELECT payload FROM historical_transactions WHERE wallet=? AND tx_hash=?',(w,h)).fetchone()
                tx=json.loads(record[0]) if record and record[0] else {}
                targets={a:'pool' for a in issue.get('unreviewed_emitters',[]) if a in pools}
                if tx.get('inventory_gap'):targets.update({a:'token' for a in issue.get('tokens_raw',{})})
                if not targets:continue
                receipt=cached(caches,'eth_getTransactionReceipt',[h])
                if not receipt or int(receipt['blockNumber'],16)!=tx.get('block'):raise ValueError('missing_exact_receipt')
                emitters={str(log['address']).lower() for log in receipt['logs']}
                for address,kind in targets.items():
                    if address not in emitters:raise ValueError('target_not_in_receipt')
                    key=address+':'+str(tx['block'])
                    entries.setdefault(key,dict(wallet=w,tx_hash=h,address=address,block=tx['block'],kind=kind,
                        receipt_block_hash=receipt['blockHash'],expected_code_hash=pools.get(address,{}).get('runtime_code_hash')))
    if not 1<=len(entries)<=2500:raise ValueError('invalid_proof_scope')
    out.mkdir(parents=True,exist_ok=False);(out/'results').mkdir();(out/'runs').mkdir()
    # One anchor per token first, then all remaining exact-block proofs.
    plan=list(entries.values());seen=set()
    def order(e):
        first=e['address'] not in seen;seen.add(e['address'])
        return (not first,e['kind']!='token',e['wallet'],e['block'])
    plan=sorted(plan,key=order)
    save_new(out/'plan.json',plan)
    save_new(out/'policy.json',dict(plan_sha256=hashlib.sha256((out/'plan.json').read_bytes()).hexdigest(),
        campaign=str(campaign.resolve()),max_attempts=2500,max_seconds=3600,source=SOURCE,
        max_reserved_usd_per_run=.25,source_data_modified=False,engine_authority=False))
    reused=0
    for idx,entry in enumerate(plan):
        prior=stored.get((entry['address'],entry['block']))
        if prior:
            code,source,digest=prior;diagnostic=code_diagnostic(code)
            save_new(out/'results'/f'{idx:04d}.json',dict(entry=entry,code=code,diagnostic=diagnostic,
                matches_pool_anchor=entry['kind']=='pool' and diagnostic['code_hash']==entry['expected_code_hash'],
                reused_source=source,reused_sha256=digest,engine_authority=False,paper_selected=False))
            reused+=1
    print(json.dumps(dict(planned=len(plan),canonical_pools=len(pools),reused=reused)),flush=True)


class ExactBudget(CampaignBudget):
    def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);self.exact_attempts=0
    def take_paid(self,method):
        if method not in {'eth_chainId','eth_getCode'} or self.exact_attempts>=2500:raise PilotLimit()
        value=super().take_paid(method);self.exact_attempts+=1;return value


def run(out,cfg):
    out=Path(out);policy=json.loads((out/'policy.json').read_text());campaign=Path(policy['campaign'])
    require_alchemy_arc(cfg.rpc_url)
    if hashlib.sha256((out/'plan.json').read_bytes()).hexdigest()!=policy['plan_sha256']:raise ValueError('plan_changed')
    plan=json.loads((out/'plan.json').read_text());settings=json.loads((campaign/'settings.json').read_text())
    if not settings.get('paid_rpc',{}).get('enabled'):raise ValueError('paid_not_enabled')
    if not 1<=len(plan)<=2500 or any(p['wallet'] not in settings['paid_rpc']['wallets'] for p in plan):raise ValueError('scope_changed')
    with ProcessLock(campaign/'worker.lock'),closing(connect(campaign/'campaign.sqlite3')) as db:
        run_dir=out/'runs'/str(time.time_ns());run_dir.mkdir()
        snapshot(db,run_dir,'before_exact_proofs')
        budget=ExactBudget(campaign,db,settings);budget.directory=run_dir
        budget.deadline=min(budget.deadline,time.monotonic()+3600)
        before=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
        s=AlchemyCampaignSession(budget,cfg.rpc_url)
        rpc=CampaignRPCProvider(cfg.rpc_url,5042,SharedRateLimiter(cfg.rpc_rate_limit_file,1),session=s)
        stop='INTERRUPTED';new=0;retained=0
        try:
            for idx,entry in enumerate(plan):
                target=out/'results'/f'{idx:04d}.json'
                if target.exists():
                    saved=json.loads(target.read_text())
                    if saved.get('entry')!=entry:raise ValueError('saved_proof_scope_mismatch')
                    retained+=1;continue
                if (out/'STOP').exists():stop='STOP_REQUESTED';break
                budget.wallet=entry['wallet']
                try:
                    code=rpc.make_request('eth_getCode',[entry['address'],hex(entry['block'])])['result']
                    diagnostic=code_diagnostic(code)
                    result=dict(entry=entry,code=code,diagnostic=diagnostic,
                                matches_pool_anchor=entry['kind']=='pool' and diagnostic['code_hash']==entry['expected_code_hash'])
                except PilotLimit:stop='LIMIT';break
                except RPCError as e:result=dict(entry=entry,error=e.category)
                result.update(engine_authority=False,paper_selected=False)
                save_new(target,result);new+=1
                if new%25==0:print(json.dumps(dict(new=new,retained=retained,planned=len(plan))),flush=True)
            else:stop='COMPLETE'
        finally:
            s.close();after=list(db.execute('SELECT attempts,reserved_cu FROM campaign_paid_budget WHERE id=1').fetchone())
            summary=dict(stop=stop,new=new,retained=retained,planned=len(plan),before=before,after=after,
                         attempts=after[0]-before[0],reserved_cu=after[1]-before[1],engine_authority=False)
            save_new(run_dir/'summary.json',summary)
            files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (out/'results').glob('*.json')}
            save_new(run_dir/'results_manifest.json',files)
            print(json.dumps(summary),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='action',required=True)
    a=s.add_parser('prepare');a.add_argument('--campaign',required=True);a.add_argument('--audit',required=True);a.add_argument('--pools',required=True);a.add_argument('--output',required=True);a.add_argument('--code-proofs',action='append',default=[])
    a=s.add_parser('run');a.add_argument('--output',required=True)
    a=p.parse_args()
    from config import Settings
    cfg=Settings()
    if a.action=='prepare':prepare(a.campaign,a.audit,a.pools,a.output,cfg,a.code_proofs)
    else:run(a.output,cfg)


if __name__=='__main__':main()
