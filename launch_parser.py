from __future__ import annotations
import re
from arcscan import ArcscanError

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

def _find_lists(obj):
    if isinstance(obj, list):
        yield obj
        for x in obj:
            yield from _find_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _find_lists(v)

def _addr(v):
    if isinstance(v, str) and ADDRESS_RE.match(v):
        return v.lower()
    if isinstance(v, dict):
        for k in ("address","contract","contract_address","token_address","addr"):
            x = v.get(k)
            if isinstance(x, str) and ADDRESS_RE.match(x):
                return x.lower()
    return None

def _int(v):
    try: return int(v)
    except Exception: return None

def _extract_contract_row(row):
    if not isinstance(row, dict): return None
    address = None
    for k in ("address","contract_address","contract","addr"):
        address = _addr(row.get(k))
        if address: break
    if not address: return None
    standard = str(row.get("standard") or row.get("token_standard") or row.get("type") or "").upper()
    symbol = row.get("symbol")
    name = row.get("name")
    token_obj = row.get("token")
    if isinstance(token_obj, dict):
        symbol = symbol or token_obj.get("symbol")
        name = name or token_obj.get("name")
        address = _addr(token_obj) or address
        standard = standard or str(token_obj.get("standard") or "").upper()
    deployer = None
    for k in ("deployer","creator","created_by","from"):
        deployer = _addr(row.get(k))
        if deployer: break
    block = None
    for k in ("block","block_number","deployment_block","created_block","creation_block"):
        block = _int(row.get(k))
        if block is not None: break
    timestamp = None
    for k in ("timestamp","deployed_at","created_at","time"):
        timestamp = _int(row.get(k))
        if timestamp is not None:
            if timestamp > 10_000_000_000: timestamp //= 1000
            break
    is_erc20 = "ERC-20" in standard or "ERC20" in standard
    if standard and not is_erc20: return None
    return {"address":address,"symbol":symbol,"name":name,"deployer":deployer,"deployment_block":block,"deployed_at":timestamp,"is_erc20":is_erc20}

def extract_contract_candidates(data):
    out = {}
    for lst in _find_lists(data):
        for row in lst:
            parsed = _extract_contract_row(row)
            if not parsed: continue
            if parsed["is_erc20"] or parsed["symbol"] or parsed["name"]:
                out[parsed["address"]] = parsed
    return list(out.values())


def parse_deployment_page(data):
    """Validate authoritative rows; retain every address but admit only ERC-20.

    Arcscan's documented unclassified category is ``token.standard="unknown"``
    in the captured saved-cursor response. Missing/null standards are not that
    category. Do not recursively infer deployments from labels or nested lists.
    """
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ArcscanError("unrecognized launch discovery page")
    aliases = {"erc20":"erc20", "erc-20":"erc20", "erc721":"erc721",
               "erc-721":"erc721", "erc1155":"erc1155", "erc-1155":"erc1155",
               "unknown":"unknown"}
    candidates, keys = [], []
    for row in data["items"]:
        if not isinstance(row, dict):
            raise ArcscanError("unrecognized deployment")
        address = _addr(row.get("address"))
        if not address:
            raise ArcscanError("invalid deployment address")
        token = row.get("token")
        if token is not None and not isinstance(token, dict):
            raise ArcscanError("malformed deployment token metadata")
        token = token or {}
        if "address" in token and _addr(token["address"]) != address:
            raise ArcscanError("conflicting deployment addresses")
        standards = []
        for node in (row, token):
            if "standard" not in node:
                continue
            value = node["standard"]
            if not isinstance(value, str) or not value:
                raise ArcscanError("malformed deployment standard")
            standard = aliases.get(value.lower())
            if standard is None:
                raise ArcscanError("unknown deployment standard")
            standards.append(standard)
        if not standards:
            raise ArcscanError("missing deployment standard")
        if len(set(standards)) != 1:
            raise ArcscanError("conflicting deployment standards")
        keys.append(address)
        if standards[0] != "erc20":
            continue
        parsed = _extract_contract_row(dict(row, standard="erc20"))
        if (not parsed or parsed["address"] != address or parsed["deployment_block"] is None
                or parsed["deployed_at"] is None):
            raise ArcscanError("incomplete deployment metadata")
        candidates.append(parsed)
    return candidates, keys

def extract_tokens_from_transfers(data):
    out = {}
    for lst in _find_lists(data):
        for row in lst:
            if not isinstance(row, dict): continue
            token = row.get("token")
            addr = _addr(token)
            symbol = None; name = None
            if isinstance(token, dict):
                if str(token.get("standard", "erc20")).lower().replace("-", "") != "erc20": continue
                symbol = token.get("symbol"); name = token.get("name")
            if not addr:
                for k in ("token_address","contract_address","address"):
                    addr = _addr(row.get(k))
                    if addr: break
            if addr:
                out[addr] = {"address":addr,"symbol":symbol or row.get("symbol"),"name":name or row.get("name"),"deployer":None,"deployment_block":None,"deployed_at":None,"is_erc20":True}
    return list(out.values())
