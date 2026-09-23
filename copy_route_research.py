"""Offline proof for V3/V3 or V3/V4 two-hop routes; no engine authority.

Actual pool Transfer deltas must exactly reconcile each Swap event. Hook fees,
flash accounting or mixed routes that do not reconcile remain unresolved.
"""
from collections import defaultdict, deque
from itertools import combinations
from eth_abi import encode, decode
from eth_abi.exceptions import DecodingError, EncodingError
from web3 import Web3

from copy_exact_code_research import canonical_pool
from copy_slipstream_research import reviewed_pool
from copy_native_route_research import reviewed_native_payment
from decoder import TRANSFER_TOPIC, SWAP_TOPICS, hex_value, topic_to_address, wallet_token_deltas
from rpc import USDC_ERC20, NATIVE_MIRROR

MANAGER='0x8366a39cc670b4001a1121b8f6a443a643e40951'
V3=hex_value(Web3.keccak(text='Swap(address,address,int256,int256,uint160,uint128,int24)'))
V4=hex_value(Web3.keccak(text='Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)'))
CASH=USDC_ERC20.lower()
MIRROR=NATIVE_MIRROR.lower()
KEY_TYPES=['address','address','uint24','int24','address']


def recover_key(calldata,tokens,pool_id):
    """Candidates are untrusted; only a 160-byte PoolKey hash match is evidence."""
    raw=bytes.fromhex(calldata.removeprefix('0x'))
    if len(raw)>65536 or not 2<=len(tokens)<=16:return None
    triples=set()
    for offset in (0,4):
        words=[int.from_bytes(raw[i:i+32],'big') for i in range(offset,len(raw)-31,32)]
        for i in range(len(words)-2):
            fee,spacing,hook=words[i:i+3]
            if 0<=fee<2**24 and 0<spacing<=32767 and 0<=hook<2**160:
                triples.add((fee,spacing,'0x'+f'{hook:040x}'))
    if len(triples)>128:return None
    matches=set()
    for a,b in combinations(sorted(tokens),2):
        for fee,spacing,hook in triples:
            key=(a,b,fee,spacing,hook)
            if hex_value(Web3.keccak(encode(KEY_TYPES,key)))==hex_value(pool_id):matches.add(key)
    return next(iter(matches)) if len(matches)==1 else None


def pool_delta(receipt,address):
    net={t.lower():v for t,v in wallet_token_deltas(receipt,address).items() if v}
    # Both interfaces represent the same USDC balance on Arc. Require exact
    # agreement when both are logged; never sum them or silently mix decimals.
    if MIRROR in net:
        if CASH not in net or net[MIRROR]!=net[CASH]*10**12:raise ValueError('cash_mismatch')
        del net[MIRROR]
    return net


def prove_route(tx,receipt,wallet,trade,rpc,pools):
    try:return _prove(tx,receipt,wallet.lower(),trade,rpc,pools)
    except (ValueError,TypeError,KeyError,OverflowError,DecodingError,EncodingError):return None


def _prove(tx,receipt,wallet,trade,rpc,pools):
    if (tx.get('from','').lower()!=wallet or
        receipt['status']!=1 or trade['side'] not in ('BUY','SELL')):return None
    native_payment=None
    if int(tx.get('value','0x0'),16)!=0:
        native_payment=reviewed_native_payment(tx,receipt,wallet,trade)
        if native_payment is None:return None
    logs=receipt['logs']
    swaps=[l for l in logs if l.get('topics') and hex_value(l['topics'][0]) in SWAP_TOPICS]
    kinds=[hex_value(l['topics'][0]) for l in swaps]
    if (len(swaps)!=2 or kinds.count(V3) not in (1,2) or any(k not in (V3,V4) for k in kinds)
        or len({l['address'].lower() for l in swaps})!=2):return None
    transfers=[l for l in logs if len(l.get('topics',[]))==3 and hex_value(l['topics'][0])==hex_value(TRANSFER_TOPIC)]
    tokens={l['address'].lower() for l in transfers}-{MIRROR}
    if not 2<=len(tokens)<=16:return None
    graph=defaultdict(list);proofs=[]
    for l in transfers:
        if l.get('removed') or len(l['data'])!=66:return None
        if int(l['data'],16)<=0:continue
        if any(len(t)!=66 or int(t[2:26],16) for t in l['topics'][1:]):return None
        currency=l['address'].lower();currency=CASH if currency==MIRROR else currency
        a,b=[topic_to_address(t).lower() for t in l['topics'][1:]]
        graph[(a,currency)].append(((b,currency),0))
    for index,l in enumerate(swaps):
        address=l['address'].lower();kind=hex_value(l['topics'][0])
        if l.get('removed') or len(l['topics'])!=3 or not rpc.swap_emitter_trusted(address,receipt['blockNumber']):return None
        data=bytes.fromhex(l['data'].removeprefix('0x'))
        if kind==V3:
            p=pools.get(address)
            slipstream=bool(p and reviewed_pool(p,receipt['blockNumber']))
            if not p or not (canonical_pool(p) or slipstream) or p['pool']!=address or len(data)!=160:return None
            expected_hash=p['pool_code_hash'] if slipstream else p['runtime_code_hash']
            if rpc.registry.entries.get(address)!=expected_hash:return None
            a0,a1,*_=decode(['int256','int256','uint160','uint128','int24'],data)
            pair=(p['token0'],p['token1']);expected=dict(zip(pair,(a0,a1)))
            detail=dict(kind='REVIEWED_SLIPSTREAM' if slipstream else 'CANONICAL_V3',emitter=address,currencies=pair)
        else:
            if address!=MANAGER or len(data)!=192:return None
            key=recover_key(tx.get('input',''),tokens,l['topics'][1])
            if key is None:return None
            a0,a1,*_=decode(['int128','int128','uint160','uint128','int24','uint24'],data)
            pair=key[:2];expected=dict(zip(pair,(-a0,-a1)))
            detail=dict(kind='REVIEWED_V4',emitter=address,pool_id=l['topics'][1],pool_key=key)
        if a0*a1>=0 or pool_delta(receipt,address)!=expected:return None
        incoming=next(t for t,v in expected.items() if v>0)
        outgoing=next(t for t,v in expected.items() if v<0)
        graph[(address,incoming)].append(((address,outgoing),1<<index))
        proofs.append(dict(detail,raw_pool_deltas={t:str(v) for t,v in expected.items()}))
    # Reach wallet output only through BOTH proven conversions and actual flows.
    token=trade['token'].lower()
    first,last=(CASH,token) if trade['side']=='BUY' else (token,CASH)
    queue=deque([((wallet,first),0)]);seen=set(queue)
    while queue:
        node,mask=queue.popleft()
        if node==(wallet,last) and mask==3:
            method='EXACT_RECONCILED_V3_V3_ROUTE' if kinds.count(V3)==2 else 'EXACT_RECONCILED_V3_V4_ROUTE'
            if any(p['kind']=='REVIEWED_SLIPSTREAM' for p in proofs):
                method='EXACT_RECONCILED_SLIPSTREAM_ROUTE'
            result=dict(method=method,pools=proofs,engine_authority=False)
            if native_payment:result['native_payment']=native_payment
            return result
        if node[0]==wallet and (node!=(wallet,first) or mask):continue
        for target,bit in graph[node]:
            state=(target,mask|bit)
            if state not in seen:seen.add(state);queue.append(state)
    return None
