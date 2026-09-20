"""Arc-only Zerion PnL enrichment. No portfolio aggregate fallback."""
import re
import time
from urllib.parse import urlsplit, parse_qs

from external_http import ExternalHTTP, ExternalError, wallet_address, number, digest

FIELDS = ('total_gain', 'realized_gain', 'unrealized_gain', 'relative_total_gain_percentage',
          'relative_realized_gain_percentage', 'relative_unrealized_gain_percentage',
          'net_invested', 'total_fee', 'received_external', 'sent_external', 'cost_basis',
          'realized_cost_basis', 'total_invested', 'sent_for_nfts', 'received_for_nfts')
CORE = ('total_gain', 'realized_gain', 'unrealized_gain', 'relative_total_gain_percentage',
        'net_invested', 'total_fee')


def normalize_pnl(data, wallet, chain, now):
    item, links = data.get('data'), data.get('links')
    if (data.get('errors') or not isinstance(item, dict) or item.get('type') != 'pnl'
            or wallet_address(item.get('id')) != wallet or not isinstance(item.get('attributes'), dict)
            or not isinstance(links, dict) or links.get('next') is not None):
        raise ExternalError('invalid_pnl_schema')
    if not isinstance(links.get('self'),str): raise ExternalError('invalid_pnl_scope')
    try: link = urlsplit(links['self'])
    except ValueError: raise ExternalError('invalid_pnl_scope') from None
    if (link.scheme != 'https' or link.netloc != 'api.zerion.io'
            or link.path.rstrip('/').lower() != '/v1/wallets/'+wallet+'/pnl'):
        raise ExternalError('invalid_pnl_scope')
    query = parse_qs(link.query, keep_blank_values=True)
    if ('filter[chain_ids]' in query and query['filter[chain_ids]'] != [chain]
            or 'currency' in query and query['currency'] != ['usd']):
        raise ExternalError('invalid_pnl_scope')
    meta = data.get('meta', {})
    if not isinstance(meta, dict): raise ExternalError('invalid_pnl_meta')
    if 'chain_ids' in meta and meta['chain_ids'] != [chain]: raise ExternalError('invalid_pnl_scope')
    attrs = item['attributes']
    metrics = {key:number(attrs.get(key), optional=True) for key in FIELDS}
    if not any(value is not None for value in metrics.values()): raise ExternalError('empty_pnl')
    breakdown = attrs.get('breakdown')
    normalized = None
    if breakdown is not None:
        if not isinstance(breakdown, dict): raise ExternalError('invalid_breakdown')
        normalized = {}
        for kind, assets in breakdown.items():
            if kind not in ('by_id', 'by_implementation') or not isinstance(assets, dict) or len(assets) > 5000:
                raise ExternalError('invalid_breakdown')
            normalized[kind] = {}
            for key, values in assets.items():
                if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_:.-]{1,200}', key) or not isinstance(values, dict):
                    raise ExternalError('invalid_breakdown')
                if kind == 'by_implementation' and key.split(':')[0] != chain:
                    raise ExternalError('invalid_pnl_scope')
                normalized[kind][key] = {k:number(values.get(k), optional=True)
                    for k in (*FIELDS, 'average_buy_price', 'average_sell_price')}
    missing = [key for key in FIELDS if metrics[key] is None]
    excluded = any(value for key,value in meta.items() if key != 'chain_ids')
    complete = not any(metrics[key] is None for key in CORE) and not excluded
    return {'wallet':wallet, 'source':'zerion', 'chain':chain, 'chain_id':5042,
            'retrieved_at':now, 'schema_version':1, 'raw_hash':digest(data),
            'scope':{'filter[chain_ids]':chain, 'currency':'usd', 'external_id':5042},
            'data_complete':complete, 'missing_fields':missing,
            'provider_exclusions_or_unknown_meta':bool(excluded),
            'metrics':{**metrics, 'realized_gain_percentage':metrics['relative_realized_gain_percentage'],
                       'unrealized_gain_percentage':metrics['relative_unrealized_gain_percentage'],
                       'received':metrics['received_external'], 'sent':metrics['sent_external']},
            'breakdown':normalized}


class ZerionClient:
    def __init__(self, cfg, http=None):
        if cfg.chain_id != 5042: raise ExternalError('wrong_chain')
        self.cfg = cfg
        self.http = http or ExternalHTTP('zerion', cfg.zerion_api_key)
        self.resolved = None

    def resolve_arc(self):
        data = self.http.get('chains/')
        rows = data.get('data')
        if (data.get('errors') or not isinstance(rows, list) or len(rows) > 1000
                or not isinstance(data.get('links'), dict) or data['links'].get('next') is not None):
            raise ExternalError('invalid_chain_registry')
        found = []
        for row in rows:
            if not isinstance(row, dict) or row.get('type') != 'chains' or not isinstance(row.get('attributes'), dict):
                raise ExternalError('invalid_chain_registry')
            external = row['attributes'].get('external_id')
            try:
                numeric = int(external, 16 if external.startswith('0x') else 10) if isinstance(external, str) else external
            except ValueError: continue  # Other chain families can use nonnumeric IDs.
            if type(numeric) is int and numeric == 5042:
                slug = row.get('id')
                if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', slug):
                    raise ExternalError('invalid_arc_identifier')
                found.append(slug)
        if len(found) != 1: raise ExternalError('arc_not_supported_or_ambiguous')
        self.resolved = found[0]
        return self.resolved

    def pnl(self, wallet, now=None):
        wallet = wallet_address(wallet)
        now = int(time.time()) if now is None else now
        chain = self.resolved or self.resolve_arc()
        data = self.http.get(f'wallets/{wallet}/pnl', {'currency':'usd', 'filter[chain_ids]':chain})
        return normalize_pnl(data, wallet, chain, now)

    def close(self): self.http.close()
