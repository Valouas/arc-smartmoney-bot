"""Start separately with python -m paper_copy. It is never added to run_v4.py."""
import argparse
import json
import logging
import time
from pathlib import Path
from config import SETTINGS
from market_data import DexScreenerClient
from process_lock import ProcessLock
from rpc import ArcRPC
from .execution import Execution
from .policy import Policy, decimal
from .report import report
from .scanner import Scanner, ChainConsistencyError
from .selection import load_selection, SelectionError
from .store import Store
from .webhook import LiveWakeup
from paper_admission import DEFAULT_SELECTION


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',default=DEFAULT_SELECTION)
    parser.add_argument('--db',default='data/paper_copy.sqlite3')
    parser.add_argument('--policy',help='JSON simulation assumptions; frozen for each database')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--status',action='store_true',help='Read-only balances, orders and checkpoint; no RPC')
    parser.add_argument('--webhook-db',default='data/arc_bot.sqlite3',help='Read-only signed webhook inbox; enabled by ALCHEMY_WEBHOOK_ENABLED')
    parser.add_argument('--no-webhook',action='store_true',help='Use the original RPC polling only')
    args = parser.parse_args(argv)
    if args.status: report(args.db); return 0
    policy = Policy(**json.loads(Path(args.policy).read_text(encoding='utf-8-sig'))) if args.policy else Policy()
    logging.basicConfig(level=logging.WARNING,format='%(message)s')
    with ProcessLock(args.db+'.lock'):
        store = Store(args.db,policy)
        rpc,market = None,None
        previous_status = None
        try:
            if store.get('halt_reason'):
                print('PAPER COPY HALTED | prior chain inconsistency requires review')
                return 2
            wakeup=LiveWakeup(store,args.webhook_db,policy) if SETTINGS.alchemy_webhook_enabled and not args.no_webhook else None
            if wakeup: print('PAPER COPY | webhook triggers enabled | ordered RPC verification + periodic fallback',flush=True)
            while True:
                now = int(time.time())
                try:
                    selected = load_selection(args.selection,SETTINGS,now)
                    review_count=sum(e.get('admission')=='PAPER_REVIEW' for e in selected.values())
                    status = 'PAPER_WALLETS='+str(len(selected))+' | PAPER_REVIEW='+str(review_count)+' | STRICT='+str(len(selected)-review_count)
                except SelectionError:
                    selected = {}; status = 'SELECTION_UNAVAILABLE: nouveaux achats desactives'
                # Expiration/removal blocks buys while preserving exit tracking.
                if not selected:
                    with store.db: store.db.execute('UPDATE wallets SET active=0')
                else:
                    # Removal takes effect even when RPC is temporarily unavailable.
                    with store.db:
                        for row in store.db.execute('SELECT wallet FROM wallets WHERE active=1').fetchall():
                            if row[0] not in selected:
                                store.db.execute('UPDATE wallets SET active=0 WHERE wallet=?',(row[0],))
                if status != previous_status:
                    print(status,flush=True); previous_status = status
                try:
                    needs_rpc = bool(selected) or any(decimal(r[0]) > 0 for r in store.db.execute('SELECT quantity FROM positions'))
                    if rpc is None and needs_rpc:
                        rpc = ArcRPC(SETTINGS.rpc_url,5042)
                        try: rpc.sanity_check()
                        except Exception:
                            rpc.w3.provider.session.close(); rpc = None
                            raise
                        market = DexScreenerClient(SETTINGS.dexscreener_chain_id)
                    if rpc is not None and needs_rpc:
                        scanner = Scanner(store,rpc,policy,SETTINGS)
                        execution = Execution(store,policy,market)
                        due=(wakeup.due(selected,set(selected)|set(scanner.watched()),now) if wakeup else True)
                        blocks=0
                        if due or args.once:
                            blocks = scanner.cycle(selected,on_block=lambda: execution.pending(int(time.time())))
                            if wakeup:
                                wakeup.refresh(set(scanner.watched()),int(time.time()))
                                wakeup.scanned(selected,blocks,int(time.time()))
                        execution.pending(int(time.time()))
                        if due or args.once:
                            print('PAPER COPY | blocks='+str(blocks)+' | next='+str(store.get('next_block')),flush=True)
                    else:
                        if wakeup: wakeup.refresh(set(),now)
                        Execution(store,policy,None).pending(now)
                except ChainConsistencyError:
                    with store.db: store.set('halt_reason','CHAIN_CONSISTENCY_REQUIRES_REVIEW')
                    print('PAPER COPY HALTED | chain consistency requires review; no checkpoint skipped',flush=True)
                    return 2
                except Exception as error:
                    # No raw provider exceptions/URLs, source payloads or credentials.
                    print('PAPER COPY retry later | category='+type(error).__name__,flush=True)
                    if args.once: return 1
                if args.once:
                    report(args.db); return 0
                time.sleep(policy.poll_seconds)
        except KeyboardInterrupt: return 0
        finally:
            store.close()
            if rpc is not None: rpc.w3.provider.session.close()
            if market is not None: market.s.close()


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print('PAPER COPY failed | category='+type(error).__name__)
        raise SystemExit(1) from None
