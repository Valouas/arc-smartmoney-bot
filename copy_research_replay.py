"""Isolated holding research with evidenced direct swaps and explicit credits.

Never writes a source DB, production trust registry or paper selection. Raw
integer token units avoid assuming historical decimals for the timing metric.
"""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
from copy import deepcopy
from decimal import localcontext
import hashlib
import json
from pathlib import Path

from web3 import Web3
from copy_ambiguity_audit import readonly,cached,load_ledger,OfflineRPC,CASH
from copy_exact_code_research import canonical_pool,code_diagnostic
from copy_holding_duration import measure,unknown,ADDRESS,HASH
from copy_holding_pilot import save_new
from decoder import wallet_token_deltas,classify_trade,hex_value,SWAP_TOPICS,TRANSFER_TOPIC,topic_to_address
from dex_registry import TrustedDexRegistry
from copy_route_research import prove_route
from copy_slipstream_research import reviewed_pool
from copy_distribution_research import reviewed_distribution
from copy_synthra_research import reviewed_pool as reviewed_synthra_pool
from copy_launchpad_research import prove_buy as prove_launchpad_buy, reviewed_code as reviewed_launchpad_code
from copy_v2_research import reviewed_pool as reviewed_v2_pool

NONASSET='0xc6203d86f5efbed7f9512755f3e6132ee50d03d1'
NONASSET_HASH='0x2825969fa042ed3875c136534af0516ab3226cbe2e1809629dae4642d5070f42'
READONLY_LOG_TOKEN='0xba4985efa2e16bd897da4b46a8f37dd80de4be8e'
READONLY_LOG_HASH='0xc562f804e4e9a90fc0e8f2720071e5bc7f768b79bb2fcbd9e0af70623018a93e'


class RawUnitsRPC(OfflineRPC):
    def swap_emitter_trusted(self,address,block):
        v2=getattr(self,'v2_proofs',{})
        if any(a==address.lower() for a,b in v2):
            if not reviewed_v2_pool(v2.get((address.lower(),block),{}),block):
                return False
        synthra=getattr(self,'synthra_proofs',{})
        if any(a==address.lower() for a,b in synthra):
            if not reviewed_synthra_pool(synthra.get((address.lower(),block),{}),block):
                return False
        proof=getattr(self,'pool_proofs',{}).get(address.lower(),{})
        if proof.get('status')=='MEMBERSHIP_OBSERVED_REVIEW_PENDING' and not reviewed_pool(proof,block):
            return False
        return super().swap_emitter_trusted(address,block)

    def token_meta(self,address):
        # Only side/cash-flow/confirmation from classify_trade are consumed.
        # Its float token_amount is replaced with an exact raw integer below.
        return dict(symbol='RAW_UNITS',decimals=0)


def receipt_info(wallet,payload,tx,receipt):
    if (not tx or not receipt or tx['hash'].lower()!=payload['tx_hash'] or
        receipt['transactionHash'].lower()!=payload['tx_hash'] or
        tx['blockHash']!=receipt['blockHash'] or tx['blockNumber']!=receipt['blockNumber'] or
        int(receipt['blockNumber'],16)!=payload['block'] or int(receipt['transactionIndex'],16)!=payload['index']):
        raise ValueError('RAW_SNAPSHOT_MISMATCH')
    cash_touch=False;swap=False
    for log in receipt['logs']:
        topics=log.get('topics',[])
        if topics and hex_value(topics[0]) in SWAP_TOPICS:swap=True
        if not topics or hex_value(topics[0])!=hex_value(TRANSFER_TOPIC):continue
        # ERC721 Transfer has four topics; never silently decode it as ERC20.
        if len(topics)==4:continue
        if (len(topics)!=3 or not ADDRESS.fullmatch(log['address']) or
            any(not HASH.fullmatch(t) or int(t[2:26],16) for t in topics[1:]) or
            not HASH.fullmatch(log['data']) or log.get('removed')):
            raise ValueError('MALFORMED_TRANSFER_EVIDENCE')
        ends={topic_to_address(t).lower() for t in topics[1:]}
        # A zero-value USDC Transfer is not a movement of wallet capital. Any
        # positive cash leg still blocks the nontrading-credit explanations.
        cash_touch |= wallet in ends and log['address'].lower() in CASH and int(log['data'],16)>0
    return dict(deltas={t.lower():v for t,v in wallet_token_deltas(receipt,wallet).items() if v},
                cash_touch=cash_touch,swap=swap,success=int(receipt['status'],16)==1)


def isolated_credit(wallet,tx,info,disposed,traded):
    tokens={t:v for t,v in info['deltas'].items() if t not in CASH}
    return bool(tokens and all(v>0 for v in tokens.values()) and not(set(tokens)&(disposed|traded)) and
        not info['cash_touch'] and not info['swap'] and info['success'] and
        tx.get('from','').lower()!=wallet and int(tx.get('value','0x0'),16)==0)


def nonasset_only(wallet,tx,payload,info,codes):
    if (len(info['deltas'])!=1 or info['cash_touch'] or info['swap'] or
        not info['success'] or tx.get('from','').lower()==wallet or int(tx.get('value','0x0'),16)):
        return False
    token=next(iter(info['deltas']))
    expected={NONASSET:NONASSET_HASH,READONLY_LOG_TOKEN:READONLY_LOG_HASH}.get(token)
    if expected is None:return False
    values=codes.get((token,payload['block']),set())
    if len(values)!=1:return False
    code=next(iter(values))
    if Web3.to_hex(Web3.keccak(code))!=expected:return False
    diagnostic=code_diagnostic('0x'+code.hex())
    if token==NONASSET:return diagnostic['status']=='NO_STORAGE_OR_CALL_OPCODES'
    # Specifically reviewed runtime: balanceOf reads mapping slot 4; transfer
    # and transferFrom emit logs and return true, without any ledger mutation.
    # SLOAD is allowed ONLY for this exact address/hash, never arbitrary tokens.
    return (diagnostic['status']=='REQUIRES_SEMANTIC_REVIEW' and
            diagnostic['opcode_inventory']==['0x54'])


def replay_wallet(wallet,run,records,raw,rpc,codes):
    output=deepcopy(records);changes=[];infos={};disposed=set();traded=set()
    if (run.get('version')!=1 or run.get('phase')!='COMPLETE' or run.get('error') or run.get('cursor')):
        return dict(holding=unknown('INCOMPLETE_LOCAL_HISTORY'),changes=[],records=output)
    try:
        for p in output:
            if p.get('outside_snapshot'):continue
            if p['block']>run['cutoff_block']:raise ValueError('OUTSIDE_BLOCK_COVERAGE')
            tx,receipt=raw.get(p['tx_hash'],(None,None));info=receipt_info(wallet,p,tx,receipt)
            infos[p['tx_hash']]=info
            if info['success']:disposed.update(t for t,v in info['deltas'].items() if v<0 and t not in CASH)
        for p in output:
            if p.get('outside_snapshot'):continue
            original=deepcopy(p);tx,receipt=raw[p['tx_hash']];info=infos[p['tx_hash']]
            if p.get('error'):
                try:
                    r=dict(receipt,status=int(receipt['status'],16),blockNumber=p['block'])
                    launch=prove_launchpad_buy(wallet,p,tx,receipt,info,codes)
                    if launch:
                        p['research_launchpad_evidence']=launch
                        new=[dict(side='BUY',token=launch['token'],symbol='RAW_UNITS',
                            token_amount=launch['raw_token_amount'],usdc_amount=int(launch['raw_usdc_amount'])/10**6,
                            confirmation_eligible=True)]
                    else:
                        new=classify_trade(rpc,r,wallet,1e-18,
                            native_value_wei=int(tx.get('value','0x0'),16) if tx.get('from','').lower()==wallet else 0,strict=True)
                    if len(new)==1 and not new[0].get('confirmation_eligible'):
                        route=prove_route(tx,r,wallet,new[0],rpc,getattr(rpc,'pool_proofs',{}))
                        if route:
                            new[0]['confirmation_eligible']=True
                            p['research_route_evidence']=route
                    if new and all(t.get('confirmation_eligible') is True for t in new):
                        p['trades']=new;p.pop('error',None)
                        p['inventory_gap']=bool(set(info['deltas'])-CASH-{t['token'].lower() for t in new})
                except (ValueError,TypeError,KeyError):pass
            for trade in p.get('trades',[]):
                t=trade['token'].lower();delta=info['deltas'].get(t,0)
                if (trade.get('confirmation_eligible') is not True or not info['success'] or
                    trade['side'] not in ('BUY','SELL') or (delta<=0 if trade['side']=='BUY' else delta>=0)):
                    raise ValueError('TRADE_RAW_QUANTITY_MISMATCH')
                trade['token_amount']=str(abs(delta));trade['quantity_unit']='RAW_TOKEN_UNITS'
                traded.add(t)
            if original.get('error') and not p.get('error'):
                reason=p['research_route_evidence']['method'].removeprefix('EXACT_') if p.get('research_route_evidence') else 'DIRECT_SWAP_WITH_REVIEWED_EXACT_CODE'
                if p.get('research_launchpad_evidence'):reason=p['research_launchpad_evidence']['method']
                changes.append(dict(tx_hash=p['tx_hash'],reason=reason,before=original,after=deepcopy(p)))
        # Isolation uses the entire complete receipt set and both retained/new trades.
        for p in output:
            if p.get('outside_snapshot') or p.get('trades') or not (p.get('error') or p.get('inventory_gap')):continue
            tx,receipt=raw[p['tx_hash']];info=infos[p['tx_hash']];reason=None;evidence=None
            # Generic credit rules cannot excuse decoder errors. Only the
            # exact reviewed input/receipt below can explain that known case.
            if not p.get('error'):
                if nonasset_only(wallet,tx,p,info,codes):reason='REVIEWED_NON_STATE_CHANGING_LOG_SOURCE'
                elif isolated_credit(wallet,tx,info,disposed,traded):reason='ISOLATED_NONTRADING_CREDIT'
            if not reason:
                evidence=reviewed_distribution(wallet,tx,receipt,info,disposed,traded)
                if evidence:reason=evidence['reason']
            if reason:
                before=deepcopy(p);p['inventory_gap']=False
                if evidence:p.pop('error',None)
                p['research_inventory_evidence']=dict(evidence or {},reason=reason,raw_deltas={t:str(v) for t,v in info['deltas'].items()})
                changes.append(dict(tx_hash=p['tx_hash'],reason=reason,before=before,after=deepcopy(p)))
        # uint256 balances have up to 78 digits. Default Decimal precision (28)
        # can lose unit remainders when FIFO adds/subtracts raw quantities.
        with localcontext() as context:
            context.prec=120
            holding=measure(output,run['cutoff_at'])
    except (ValueError,KeyError,TypeError):holding=unknown('RESEARCH_RAW_EVIDENCE_INCOMPLETE')
    return dict(holding=holding,changes=changes,records=output)


def run(campaign,scope,pool_dir,exact_dir,out,caches,ledgers,registry_path,slipstream_proofs=(),synthra_proofs=(),launchpad_proofs=(),v2_proofs=()):
    campaign,pool_dir,exact_dir,out=map(Path,(campaign,pool_dir,exact_dir,out))
    wallets=json.loads(Path(scope).read_text())['wallets']
    if (not isinstance(wallets,list) or not 1<=len(wallets)<=25 or len(set(wallets))!=len(wallets) or
        any(not isinstance(w,str) or not ADDRESS.fullmatch(w) or w!=w.lower() for w in wallets)):
        raise ValueError('invalid_scope')
    registry=TrustedDexRegistry(registry_path,5042)
    pool_manifest=json.loads((pool_dir/'manifest.json').read_text());pool_proofs={}
    for path in pool_dir.glob('pool_*.json'):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=pool_manifest[path.name]:raise ValueError('pool_digest_mismatch')
        p=json.loads(path.read_text())
        if canonical_pool(p):
            registry.entries[p['pool']]=p['runtime_code_hash'];pool_proofs[p['pool']]=p
    latest=sorted((exact_dir/'runs').glob('*/results_manifest.json'))[-1]
    manifest=json.loads(latest.read_text());exact_codes={}
    for name,digest in manifest.items():
        path=exact_dir/'results'/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('code_digest_mismatch')
        proof=json.loads(path.read_text())
        if not proof.get('error'):
            e=proof['entry'];exact_codes.setdefault((e['address'],e['block']),set()).add(bytes.fromhex(proof['code'][2:]))
    _,codes,provenance=load_ledger(ledgers,set(registry.entries))
    for k,v in exact_codes.items():codes.setdefault(k,set()).update(v)
    for path in map(Path,slipstream_proofs):
        digest=json.loads((path.parent/'manifest.json').read_text(encoding='utf-8')).get(path.name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('slipstream_digest_mismatch')
        proof=json.loads(path.read_text(encoding='utf-8'))
        if not reviewed_pool(proof):raise ValueError('slipstream_provenance_incomplete')
        address,block=proof['pool'],proof['block']
        registry.entries[address]=proof['pool_code_hash'];pool_proofs[address]=proof
        codes.setdefault((address,block),set()).add(bytes.fromhex(proof['pool_code'][2:]))
    synthra={}
    for path in map(Path,synthra_proofs):
        digest=json.loads((path.parent/'manifest.json').read_text(encoding='utf-8')).get(path.name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('synthra_digest_mismatch')
        for proof in json.loads(path.read_text(encoding='utf-8'))['rows']:
            address,block=proof['pool'],proof['block']
            if not reviewed_synthra_pool(proof,block):raise ValueError('synthra_provenance_incomplete')
            key=(address,block)
            if key in synthra and synthra[key]!=proof:raise ValueError('conflicting_synthra_proof')
            synthra[key]=proof;registry.entries[address]=proof['pool_code_hash']
            codes.setdefault(key,set()).add(bytes.fromhex(proof['pool_code'][2:]))
    for path in map(Path,launchpad_proofs):
        digest=json.loads((path.parent/'manifest.json').read_text(encoding='utf-8')).get(path.name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('launchpad_digest_mismatch')
        proof=json.loads(path.read_text(encoding='utf-8'))
        if not reviewed_launchpad_code(proof):raise ValueError('launchpad_provenance_incomplete')
        codes.setdefault((proof['address'],proof['block']),set()).add(bytes.fromhex(proof['code'][2:]))
    v2={}
    for path in map(Path,v2_proofs):
        digest=json.loads((path.parent/'manifest.json').read_text(encoding='utf-8')).get(path.name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('v2_digest_mismatch')
        for proof in json.loads(path.read_text(encoding='utf-8'))['pools']:
            address,block=proof['pool'],proof['block']
            if not reviewed_v2_pool(proof,block):raise ValueError('v2_provenance_incomplete')
            key=(address,block)
            if key in v2 and v2[key]!=proof:raise ValueError('conflicting_v2_proof')
            v2[key]=proof;registry.entries[address]=proof['runtime_code_hash']
            codes.setdefault(key,set()).add(bytes.fromhex(proof['code'][2:]))
    rpc=RawUnitsRPC(registry,{},codes)
    rpc.v2_proofs=v2
    rpc.synthra_proofs=synthra
    rpc.pool_proofs=pool_proofs
    out.mkdir(parents=True,exist_ok=False);rows=[]
    with ExitStack() as stack:
        db=stack.enter_context(closing(readonly(campaign/'campaign.sqlite3')))
        cs=[stack.enter_context(closing(readonly(p))) for p in caches if Path(p).exists()]
        for w in wallets:
            cur=db.execute('SELECT * FROM historical_runs WHERE wallet=?',(w,));r=cur.fetchone()
            if not r:raise ValueError('wallet_not_collected')
            header=dict(zip((d[0] for d in cur.description),r))
            records=[json.loads(p) for p, in db.execute('SELECT payload FROM historical_transactions WHERE wallet=?',(w,))]
            raw={p['tx_hash']:(cached(cs,'eth_getTransactionByHash',[p['tx_hash']]),cached(cs,'eth_getTransactionReceipt',[p['tx_hash']]))
                 for p in records if not p.get('outside_snapshot')}
            result=replay_wallet(w,header,records,raw,rpc,codes)
            save_new(out/(w+'.json'),dict(wallet=w,run=header,**result,source_records=records,paper_selected=False,live_authority=False))
            rows.append(dict(wallet=w,**result['holding'],changes=dict(Counter(c['reason'] for c in result['changes'])),
                unresolved=sum(bool(p.get('error') or p.get('inventory_gap')) for p in result['records']),paper_selected=False,live_authority=False))
            print(json.dumps(dict(wallet=w,status=result['holding']['holding_duration_status'],changes=len(result['changes']))),flush=True)
    summary=dict(rows=rows,method='ISOLATED_RESEARCH_REPLAY_RAW_UNITS',network_calls=0,source_modified=False,
        passed=sum(r['holding_duration_eligible'] for r in rows),too_short=sum(r['holding_duration_status']=='EXCLUDED_SHORT_HOLDING' for r in rows))
    save_new(out/'summary.json',summary);save_new(out/'metadata_provenance.json',provenance)
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()};save_new(out/'manifest.json',hashes)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('campaign','scope','pools','exact','output'):p.add_argument('--'+arg,required=True)
    p.add_argument('--cache',action='append',default=[]);p.add_argument('--ledger',action='append',default=[])
    p.add_argument('--slipstream-proof',action='append',default=[])
    p.add_argument('--synthra-proof',action='append',default=[])
    p.add_argument('--launchpad-proof',action='append',default=[])
    p.add_argument('--v2-proof',action='append',default=[])
    p.add_argument('--registry',default='trusted_dex.json');a=p.parse_args()
    run(a.campaign,a.scope,a.pools,a.exact,a.output,[Path(a.campaign)/'rpc_cache.sqlite3',*a.cache],
        [Path(a.campaign)/'requests.jsonl',*a.ledger],a.registry,a.slipstream_proof,a.synthra_proof,a.launchpad_proof,a.v2_proof)


if __name__=='__main__':main()
