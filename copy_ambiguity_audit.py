"""Offline diagnostic of stored holding ambiguities; never grants authority."""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3

from copy_candidate_report import ADDRESS
from decoder import classify_trade, wallet_token_deltas, SWAP_TOPICS, hex_value
from dex_registry import TrustedDexRegistry
from rpc import USDC_ERC20, NATIVE_MIRROR
from rpc_transport import _valid_result, _matches_request

CASH={USDC_ERC20.lower(),NATIVE_MIRROR.lower()}
CAUSE_LABELS={
    'NO_REVIEWED_SWAP_EMITTER':'Swap sans émetteur DEX approuvé',
    'INCOMING_TOKEN_WITHOUT_CONFIRMED_BUY':'Tokens reçus sans achat confirmé — inventaire à rapprocher',
    'INCOMING_ONLY_NO_OBSERVED_DISPOSAL':'Tokens reçus, sans sortie observée dans cet historique',
    'OUTGOING_TOKEN_WITHOUT_CONFIRMED_SALE':'Tokens sortis sans vente confirmée',
    'SWAP_FLOW_NOT_CONFIRMED':'Chemin des fonds du swap non confirmé',
    'MISSING_STORED_HISTORICAL_CODE':'Code historique de l’émetteur absent des journaux disponibles',
    'swap without observable USDC cost basis':'Swap sans contrepartie USDC observable',
}


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    return db


def cached(databases,method,params):
    key=json.dumps(params,sort_keys=True,separators=(',',':'))
    for db in databases:
        row=db.execute('SELECT payload,digest FROM rpc_cache_entries WHERE chain=5042 AND method=? AND params=?',(method,key)).fetchone()
        if not row or hashlib.sha256(row[0].encode()).hexdigest()!=row[1]:continue
        try:
            value=json.loads(row[0])
            if _valid_result(method,value) and _matches_request(method,params,value):return value
        except (ValueError,TypeError,KeyError):pass
    return None


def load_ledger(paths,targets,code_keys=None):
    """Only retain relevant stored eth_call/eth_getCode responses, with provenance.

    Consistent saved decimals are diagnostic, not proof of decimals at trade time.
    Historical code must have an explicit matching numeric block identifier.
    """
    decimals=defaultdict(set);codes=defaultdict(set);provenance=defaultdict(list);code_pool={}
    for path in paths:
        path=Path(path)
        if not path.exists():continue
        with path.open(encoding='utf-8') as f:
            for number,line in enumerate(f,1):
                if 'eth_call' not in line and 'eth_getCode' not in line:continue
                try:
                    event=json.loads(line);scope=event.get('scope',{});params=scope.get('params') or []
                    if event.get('state')!='response' or event.get('status')!=200:continue
                    response=event.get('response',{})
                    if response.get('error') or not isinstance(response.get('result'),str):continue
                    result=response['result'];method=scope.get('method')
                    if method=='eth_call' and isinstance(params[0],dict):
                        call=params[0];addr=call.get('to','').lower()
                        if addr not in targets or call.get('data',call.get('input','')).lower()!='0x313ce567':continue
                        if len(result)!=66 or not result.startswith('0x'):continue
                        value=int(result,16)
                        if not 0<=value<=255:continue
                        decimals[addr].add(value);key='decimals:'+addr
                    elif method=='eth_getCode' and len(params)==2:
                        addr=params[0].lower()
                        if addr not in targets or not isinstance(params[1],str) or not params[1].startswith('0x'):continue
                        block=int(params[1],16)
                        if code_keys is not None and (addr,block) not in code_keys:continue
                        code=bytes.fromhex(result.removeprefix('0x'))
                        code=code_pool.setdefault(hashlib.sha256(code).digest(),code)
                        codes[(addr,block)].add(code);key=f'code:{addr}:{block}'
                    else:continue
                    if len(provenance[key])<3:
                        provenance[key].append(dict(file=str(path),line=number,sha256=hashlib.sha256(line.encode()).hexdigest()))
                except (ValueError,TypeError,KeyError,IndexError,AttributeError):continue
    return decimals,codes,dict(provenance)


class OfflineRPC:
    def __init__(self,registry,decimals,codes):
        self.registry,self.decimals,self.codes=registry,decimals,codes
        self.trust_checks={}

    def token_meta(self,address):
        values=self.decimals.get(address.lower(),set())
        if not values:raise ValueError('MISSING_STORED_DECIMALS')
        if len(values)!=1:raise ValueError('CONFLICTING_STORED_DECIMALS')
        return dict(symbol='?',decimals=next(iter(values)))

    def swap_emitter_trusted(self,address,block):
        address=address.lower()
        if address not in self.registry.entries:
            self.trust_checks[address]='UNREVIEWED';return False
        codes=self.codes.get((address,block),set())
        if not codes:raise ValueError('MISSING_STORED_HISTORICAL_CODE')
        if len(codes)!=1:raise ValueError('CONFLICTING_STORED_HISTORICAL_CODE')
        trusted=self.registry.trusts(address,next(iter(codes)))
        self.trust_checks[address]='TRUSTED' if trusted else 'CODE_MISMATCH'
        return trusted


def diagnose(wallet,payload,tx,receipt,rpc,disposed,full_raw_coverage):
    rpc.trust_checks={}
    if tx is None or receipt is None:return dict(cause='MISSING_RAW_TRANSACTION_OR_RECEIPT')
    try:
        if (tx['blockHash']!=receipt['blockHash'] or tx['blockNumber']!=receipt['blockNumber'] or
                int(tx['blockNumber'],16)!=payload['block'] or
                int(receipt['transactionIndex'],16)!=payload['index']):
            return dict(cause='RAW_SNAPSHOT_MISMATCH')
        receipt=dict(receipt,status=int(receipt['status'],16),blockNumber=int(receipt['blockNumber'],16))
        deltas={k.lower():v for k,v in wallet_token_deltas(receipt,wallet).items() if v}
        noncash={k:v for k,v in deltas.items() if k not in CASH}
        emitters=sorted({str(log['address']).lower() for log in receipt['logs']
            if log.get('topics') and hex_value(log['topics'][0]) in SWAP_TOPICS})
        details=dict(tokens_raw={k:str(v) for k,v in noncash.items()},swap_emitters=emitters,
            unreviewed_emitters=[e for e in emitters if e not in rpc.registry.entries])
        if payload.get('inventory_gap'):
            incoming=bool(noncash) and all(v>0 for v in noncash.values())
            isolated=(incoming and full_raw_coverage and not (set(noncash)&disposed))
            cause=('INCOMING_ONLY_NO_OBSERVED_DISPOSAL' if isolated else
                   'INCOMING_TOKEN_WITHOUT_CONFIRMED_BUY' if incoming else
                   'OUTGOING_TOKEN_WITHOUT_CONFIRMED_SALE' if noncash and all(v<0 for v in noncash.values()) else
                   'MIXED_OR_UNEXPLAINED_INVENTORY_FLOW')
            return dict(details,cause=cause,requires_wallet_inventory_audit=not isolated)
        try:
            trades=classify_trade(rpc,receipt,wallet,1e-18,
                native_value_wei=int(tx.get('value','0x0'),16) if str(tx.get('from','')).lower()==wallet else 0,strict=True)
            unconfirmed=any(t.get('confirmation_eligible') is not True for t in trades)
            if unconfirmed:
                cause=('HISTORICAL_EMITTER_CODE_MISMATCH' if 'CODE_MISMATCH' in rpc.trust_checks.values() else
                    'NO_REVIEWED_SWAP_EMITTER' if emitters and not any(e in rpc.registry.entries for e in emitters) else
                    'SWAP_FLOW_NOT_CONFIRMED')
            else:cause='LOCAL_REPLAY_DIFFERS_REQUIRES_REVIEW'
            return dict(details,cause=cause,diagnostic_trades=trades,trust_checks=rpc.trust_checks)
        except ValueError as exc:
            allowed={'conflicting USDC representations','mixed USDC flow needs trace accounting',
                'swap without observable USDC cost basis','unattributable basket flow','token decimals unavailable',
                'MISSING_STORED_DECIMALS','CONFLICTING_STORED_DECIMALS',
                'MISSING_STORED_HISTORICAL_CODE','CONFLICTING_STORED_HISTORICAL_CODE'}
            return dict(details,cause=str(exc) if str(exc) in allowed else 'REPLAY_SCHEMA_ERROR')
    except (ValueError,TypeError,KeyError,OverflowError):return dict(cause='RAW_SCHEMA_ERROR')


def audit(campaign,wallets,cache_paths,ledger_paths,registry_path):
    if (not isinstance(wallets,list) or not 1<=len(wallets)<=25 or
        any(not isinstance(w,str) or not ADDRESS.fullmatch(w) for w in wallets)):
        raise ValueError('invalid_audit_wallets')
    wallets=[w.lower() for w in wallets]
    if len(set(wallets))!=len(wallets):raise ValueError('duplicate_audit_wallets')
    registry=TrustedDexRegistry(registry_path,5042)
    records={};raw={};targets=set(registry.entries);disposed=defaultdict(set);coverage={};code_keys=set()
    missing_raw=Counter();complete={}
    with ExitStack() as stack:
        db=stack.enter_context(closing(readonly(Path(campaign)/'campaign.sqlite3')))
        caches=[stack.enter_context(closing(readonly(p))) for p in cache_paths if Path(p).exists()]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for w in wallets:
            complete[w]=False
            if 'historical_runs' in tables:
                cur=db.execute('SELECT * FROM historical_runs WHERE wallet=?',(w,));header=cur.fetchone()
                if header:
                    run=dict(zip((c[0] for c in cur.description),header))
                    complete[w]=(run.get('version')==1 and run.get('phase')=='COMPLETE'
                        and run.get('error') is None and run.get('cursor') is None)
            rows=db.execute('SELECT tx_hash,payload FROM historical_transactions WHERE wallet=?',(w,)).fetchall()
            if not rows:raise ValueError('wallet_missing_from_campaign')
            records[w]=[(h,json.loads(p) if p else None) for h,p in rows];coverage[w]=True
            for h,payload in records[w]:
                tx,receipt=raw.get(h,(None,None))
                if tx is None or receipt is None:
                    tx,receipt=cached(caches,'eth_getTransactionByHash',[h]),cached(caches,'eth_getTransactionReceipt',[h])
                issue=payload is None or payload.get('error') or payload.get('inventory_gap')
                if issue:raw[h]=(tx,receipt)
                if not tx or not receipt:coverage[w]=False;missing_raw[w]+=1;continue
                ds=wallet_token_deltas(receipt,w)
                disposed[w].update(k.lower() for k,v in ds.items() if v<0 and k.lower() not in CASH)
                if payload and (payload.get('error') or payload.get('inventory_gap')):
                    targets.update(k.lower() for k in ds)
                    code_keys.update((str(l['address']).lower(),int(receipt['blockNumber'],16)) for l in receipt['logs']
                        if str(l['address']).lower() in registry.entries and l.get('topics') and hex_value(l['topics'][0]) in SWAP_TOPICS)
        decimals,codes,provenance=load_ledger(ledger_paths,targets,code_keys)
        rpc=OfflineRPC(registry,decimals,codes);result=[];global_causes=Counter();emitter_counts=Counter()
        for w in wallets:
            issues=[];counts=Counter()
            for h,payload in records[w]:
                if payload is None:
                    issues.append(dict(tx_hash=h,cause='MISSING_STORED_PAYLOAD'));counts['MISSING_STORED_PAYLOAD']+=1;continue
                if not payload.get('error') and not payload.get('inventory_gap'):continue
                tx,receipt=raw[h]
                d=diagnose(w,payload,tx,receipt,rpc,disposed[w],coverage[w] and complete[w])
                issues.append(dict(tx_hash=h,stored_error=payload.get('error'),stored_inventory_gap=bool(payload.get('inventory_gap')),**d))
                counts[d['cause']]+=1;emitter_counts.update(d.get('unreviewed_emitters',[]))
            global_causes.update(counts)
            result.append(dict(wallet=w,transactions=len(records[w]),issues=len(issues),causes=dict(counts),
                raw_coverage_complete=coverage[w],missing_raw_transactions=missing_raw[w],
                stored_generation_complete=complete[w],issue_details=issues,
                holding_eligible=False,paper_selected=False,live_authority=False))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(),wallets=len(wallets),
        transactions=sum(r['transactions'] for r in result),issues=sum(r['issues'] for r in result),
        network_calls=0,credits_consumed=0,source_data_modified=False,causes=dict(global_causes),
        unreviewed_emitters=dict(emitter_counts.most_common()),rows=result,metadata_provenance=provenance,
        limitations=['Diagnostic only: no holding/paper/live eligibility is conferred.',
            'Consistent saved decimals are not proof of decimals at each historical block.',
            'Incoming-only with no observed disposal is not proof of spam or globally irrelevant inventory.',
            'Existing decoder and trusted registry are unchanged.'])


def write_report(result,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    (out/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    esc=lambda x:html.escape(str(x));rows=[]
    for r in sorted(result['rows'],key=lambda r:r['issues']):
        issues=''.join('<li>'+esc(CAUSE_LABELS.get(k,k))+': '+str(v)+'</li>' for k,v in r['causes'].items())
        details=''.join('<li><code>'+esc(i['tx_hash'])+'</code> — '+esc(CAUSE_LABELS.get(i['cause'],i['cause']))+'</li>' for i in r['issue_details'])
        rows.append(f'<tr><td><code>{esc(r["wallet"])}</code></td><td>{r["transactions"]}</td><td>{r["issues"]}</td><td><ul>{issues}</ul><details><summary>Transactions</summary><ul>{details}</ul></details></td></tr>')
    page='''<!doctype html><meta charset="utf-8"><title>Audit local des 25 wallets</title>
<style>body{font:15px system-ui;background:#101923;color:#e3eaf1;padding:28px}table{border-collapse:collapse;width:100%}td,th{padding:14px;border-bottom:1px solid #405060;text-align:left;vertical-align:top}code{font-size:12px}details{max-width:900px}li{margin:8px 0}h1{color:#72e3c0}</style>
<h1>Audit local des ambiguïtés</h1>'''
    page+=f'<p>{result["wallets"]} wallets · {result["transactions"]} transactions · {result["issues"]} cas examinés · 0 appel réseau.</p>'
    page+='<p>Diagnostic uniquement : aucune nouvelle autorité de copie. Les catégories expliquent les blocages ; elles ne modifient pas les données ni les seuils. Les crédits entrants sans sortie observée ne sont pas automatiquement considérés comme du spam.</p>'
    page+='<table><tr><th>Wallet</th><th>Transactions</th><th>Cas</th><th>Causes et preuves</th></tr>'+''.join(rows)+'</table>'
    (out/'audit.html').write_text(page,encoding='utf-8')
    hashes={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in out.iterdir() if f.is_file()}
    (out/'manifest.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign',required=True);p.add_argument('--wallets',required=True)
    p.add_argument('--cache',action='append',default=[]);p.add_argument('--ledger',action='append',default=[])
    p.add_argument('--registry',default='trusted_dex.json');p.add_argument('--output-dir',required=True)
    a=p.parse_args(argv);campaign=Path(a.campaign)
    wallets=json.loads(Path(a.wallets).read_text())['wallets']
    result=audit(campaign,wallets,[campaign/'rpc_cache.sqlite3',*a.cache],
        [campaign/'requests.jsonl',*a.ledger],a.registry)
    write_report(result,a.output_dir)
    print(json.dumps({k:result[k] for k in ('wallets','transactions','issues','causes','unreviewed_emitters','network_calls')},indent=2))


if __name__=='__main__':main()
