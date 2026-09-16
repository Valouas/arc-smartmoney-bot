from __future__ import annotations
import re

ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
TX = re.compile(r"^0x[a-fA-F0-9]{64}$")

def walk(obj, path=""):
    if isinstance(obj, dict):
        yield path.lower(), obj
        for k,v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            yield from walk(v, p)
    elif isinstance(obj, list):
        for i,v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")

def address(v):
    if isinstance(v, str) and ADDR.match(v): return v.lower()
    if isinstance(v, dict):
        for k in ("address","addr","account"):
            x=v.get(k)
            if isinstance(x,str) and ADDR.match(x): return x.lower()
    return None

def txhash(v):
    if isinstance(v, str) and TX.match(v): return v.lower()
    if isinstance(v, dict):
        for k in ("tx_hash","transaction_hash","hash","transactionHash"):
            x=v.get(k)
            if isinstance(x,str) and TX.match(x): return x.lower()
    return None

def extract_tx_hashes(data):
    out=[]; seen=set()
    for path,obj in walk(data):
        h=txhash(obj)
        if h and h not in seen:
            seen.add(h); out.append(h)
        if isinstance(obj,dict):
            for k,v in obj.items():
                if "tx" in k.lower() or "hash" in k.lower():
                    h=txhash(v)
                    if h and h not in seen:
                        seen.add(h); out.append(h)
    return out

def extract_transfer_rows(data, expected_token=None):
    out=[]; seen=set()
    for path,obj in walk(data):
        if not isinstance(obj,dict): continue
        h=txhash(obj)
        if not h: continue
        frm=None; to=None; token=None
        for k in ("from","from_address","sender"):
            frm=address(obj.get(k))
            if frm: break
        for k in ("to","to_address","recipient"):
            to=address(obj.get(k))
            if to: break
        token_obj=obj.get("token")
        token=address(token_obj)
        if not token:
            for k in ("token_address","contract_address","asset"):
                token=address(obj.get(k))
                if token: break
        if not token and expected_token: token=expected_token.lower()
        if not (frm and to and token): continue
        block=None
        for k in ("block","block_number","blockNumber"):
            try:
                if obj.get(k) is not None:
                    block=int(obj.get(k)); break
            except Exception: pass
        timestamp=None
        for k in ("timestamp","time","created_at"):
            try:
                if obj.get(k) is not None:
                    timestamp=int(obj.get(k))
                    if timestamp>10_000_000_000: timestamp//=1000
                    break
            except Exception: pass
        key=(h,frm,to,token)
        if key in seen: continue
        seen.add(key)
        out.append({"tx_hash":h,"block":block,"from":frm,"to":to,"token":token,"timestamp":timestamp})
    return out
