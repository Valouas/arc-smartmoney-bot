"""Optional external observations -> existing local audit -> existing paper policy."""
import argparse
import json
import time
from contextlib import closing

from config import SETTINGS
from dune_discovery import DuneDiscovery
from external_http import ExternalError, wallet_address
from external_wallet_store import ExternalStore
from process_lock import ProcessLock
from wallet_policy import infrastructure_addresses
from zerion_client import ZerionClient


def cycle(store, cfg, *, dune_only=False, now=None, dune_factory=DuneDiscovery, zerion_factory=ZerionClient):
    fixed_now = now
    accepted_at = lambda: int(time.time()) if fixed_now is None else fixed_now
    now = int(time.time()) if now is None else now
    excluded = infrastructure_addresses(cfg)
    result = {'dune_candidates':0, 'zerion_wallets':0, 'errors':[], 'live_signal_authority':False}
    if cfg.dune_enabled and store.due('dune','',now):
        try:
            with closing(dune_factory(cfg)) as client: fetched = client.fetch(now)
            received = accepted_at()
            fetched['retrieved_at'] = received
            store.dune(fetched,received,cfg)
            result.update(dune_candidates=len(fetched['rows']), dune_rejected=fetched['rejected'],
                          dune_selection_limited=fetched['selection_limited'])
        except ExternalError as error:
            with store.db: store.record_attempt('dune','',accepted_at(),max(cfg.dune_refresh_seconds,error.retry_after),error.category)
            result['errors'].append('dune:'+error.category)
    store.bridge(cfg,now,excluded)
    if cfg.zerion_enabled and not dune_only and store.due('zerion','',now):
        wallets = store.enrichment_wallets(now,cfg.zerion_max_wallets_per_cycle,excluded)
        if wallets:
            try:
                with closing(zerion_factory(cfg)) as client:
                    client.resolve_arc()  # Revalidate numeric 5042 before every enrichment cycle.
                    with store.db: store.record_attempt('zerion','',accepted_at(),0)
                    for wallet in wallets:
                        try:
                            wallet_address(wallet)
                            payload = client.pnl(wallet,now)
                            received = accepted_at()
                            payload['retrieved_at'] = received
                            store.metric(wallet,payload,received,cfg)
                            result['zerion_wallets'] += 1
                        except ExternalError as error:
                            store.metric(wallet,None,accepted_at(),cfg,error)
                            result['errors'].append('zerion:'+error.category)
                            if error.category in ('http_401','http_403','http_429') or error.retry_after:
                                raise
            except ExternalError as error:
                with store.db: store.record_attempt('zerion','',accepted_at(),max(cfg.zerion_refresh_seconds,error.retry_after),error.category)
                if 'zerion:'+error.category not in result['errors']: result['errors'].append('zerion:'+error.category)
    return result


def run(*, once=False, dune_only=False, db_path='data/arc_bot.sqlite3', cfg=SETTINGS):
    if (dune_only and not cfg.dune_enabled) or (not dune_only and not cfg.external_discovery_enabled):
        print('External discovery disabled; no database or network changes.'); return 0
    if cfg.chain_id != 5042:
        print('External discovery unavailable | category=wrong_chain'); return 1
    try:
        with ProcessLock(db_path+'.external.lock'), closing(ExternalStore(db_path)) as store:
            while True:
                try:
                    result = cycle(store,cfg,dune_only=dune_only)
                    print(json.dumps(result),flush=True)
                    code = 1 if result['errors'] else 0
                except Exception as error:
                    print('External cycle deferred | category='+type(error).__name__,flush=True)
                    code = 1
                if once: return code
                time.sleep(min(cfg.dune_refresh_seconds,cfg.zerion_refresh_seconds))
    except KeyboardInterrupt: return 0
    except Exception as error:
        category = error.category if isinstance(error,ExternalError) else type(error).__name__
        print('External pipeline unavailable | category='+category); return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--db',default='data/arc_bot.sqlite3')
    args = parser.parse_args(argv)
    return run(once=args.once,db_path=args.db)


if __name__ == '__main__': raise SystemExit(main())
