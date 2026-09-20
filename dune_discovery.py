"""Read an existing Arc query execution; never execute SQL or grant authority."""
import argparse
import re
import time
from collections import Counter

from external_http import ExternalHTTP, ExternalError, wallet_address, number, stamp, digest
from wallet_policy import infrastructure_addresses


def normalize_row(row, now, excluded):
    if not isinstance(row, dict): raise ExternalError('invalid_row')
    wallet = wallet_address(row.get('wallet'))
    if wallet in excluded: raise ExternalError('infrastructure_excluded')
    # Explicit numeric chain identity is required in the query output contract.
    if type(row.get('chain_id')) is not int or row['chain_id'] != 5042:
        raise ExternalError('wrong_or_missing_chain')
    first, last = stamp(row.get('first_seen_at')), stamp(row.get('last_seen_at'))
    if not first <= last <= now: raise ExternalError('incoherent_timestamps')
    out = {'wallet':wallet, 'chain_id':5042, 'first_seen_at':first, 'last_seen_at':last}
    for key in ('buy_count', 'sell_count', 'trade_count', 'distinct_tokens'):
        value = row.get(key)
        if type(value) is not int or not 0 <= value <= 2**53: raise ExternalError('invalid_count')
        out[key] = value
    if out['buy_count']+out['sell_count'] > out['trade_count']:
        raise ExternalError('incoherent_counts')
    out['volume_usd'] = number(row.get('volume_usd'))
    if out['volume_usd'] < 0: raise ExternalError('negative_volume')
    for key in ('realized_pnl', 'roi', 'win_rate'): out[key] = number(row.get(key), optional=True)
    out['raw_hash'] = digest(row)
    return out


class DuneDiscovery:
    def __init__(self, cfg, http=None):
        if cfg.chain_id != 5042: raise ExternalError('wrong_chain')
        if not cfg.dune_query_id.isdecimal() or int(cfg.dune_query_id) < 1:
            raise ExternalError('missing_query_id')
        self.cfg = cfg
        self.http = http or ExternalHTTP('dune', cfg.dune_api_key)

    def fetch(self, now=None):
        now = int(time.time()) if now is None else now
        cfg, offset, execution, expected, produced = self.cfg, 0, None, None, None
        rows, rejected, seen = [], Counter(), set()
        excluded = infrastructure_addresses(cfg)
        for _ in range(100):
            limit = min(100, cfg.dune_max_candidates-offset)
            path = f'query/{cfg.dune_query_id}/results' if execution is None else f'execution/{execution}/results'
            data = self.http.get(path, {'limit':limit, 'offset':offset, 'allow_partial_results':'false'})
            current = data.get('execution_id')
            if (data.get('state') != 'QUERY_STATE_COMPLETED' or data.get('is_execution_finished') is not True
                    or data.get('error') or type(data.get('query_id')) is not int
                    or data['query_id'] != int(cfg.dune_query_id)
                    or not isinstance(current, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', current)
                    or (execution is not None and execution != current)):
                raise ExternalError('invalid_execution')
            ended = stamp(data.get('execution_ended_at'))
            if ended > now or (produced is not None and produced != ended): raise ExternalError('invalid_execution_time')
            if data.get('expires_at') is not None and stamp(data['expires_at']) <= now: raise ExternalError('expired_execution')
            execution, produced = current, ended
            result = data.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('metadata'), dict): raise ExternalError('invalid_result')
            batch, meta = result.get('rows'), result['metadata']
            if any(obj.get(flag) for obj in (data,result,meta) for flag in ('is_partial','truncated','capped')):
                raise ExternalError('partial_result')
            total = meta.get('total_row_count')
            if (not isinstance(batch, list) or len(batch) > limit or type(total) is not int or total < 0
                    or (expected is not None and total != expected) or offset+len(batch) > total
                    or ('row_count' in meta and meta['row_count'] != len(batch))):
                raise ExternalError('invalid_pagination')
            expected = total
            for raw in batch:
                try:
                    row = normalize_row(raw, min(now, ended), excluded)
                except ExternalError as error:
                    rejected[error.category] += 1
                    continue
                if row['wallet'] in seen: raise ExternalError('duplicate_wallet')
                seen.add(row['wallet']); rows.append(row)
            following = data.get('next_offset')
            consumed = offset+len(batch)
            if consumed < total:
                if type(following) is not int or following != consumed or following <= offset:
                    raise ExternalError('invalid_pagination')
            elif following is not None: raise ExternalError('invalid_pagination')
            if consumed >= total or consumed >= cfg.dune_max_candidates:
                return {'rows':rows, 'rejected':dict(rejected), 'execution_id':execution,
                        'query_id':int(cfg.dune_query_id), 'execution_ended_at':produced,
                        'retrieved_at':now, 'selection_limited':consumed < total,
                        'total_rows':total, 'schema_version':1}
            offset = following
        raise ExternalError('pagination_limit')

    def close(self): self.http.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    from external_wallet_pipeline import run
    return run(once=args.once, dune_only=True)


if __name__ == '__main__': raise SystemExit(main())
