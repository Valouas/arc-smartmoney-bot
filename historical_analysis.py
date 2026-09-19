"""Historical analyzer worker or read-only report for a separate paper bot."""
import argparse
import logging
import time

from arcscan import ArcscanClient
from config import SETTINGS
from historical_collector import HistoricalCollector
from historical_report import build_report, publish, ReportExportError
from process_lock import ProcessLock
from rpc import ArcRPC
from storage import Storage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='One bounded collection cycle then report')
    parser.add_argument('--report-only', action='store_true', help='No network and no database mutation')
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--all', action='store_true', help='Display all wallets')
    parser.add_argument('--diverse', action='store_true')
    parser.add_argument('--db', default='data/arc_bot.sqlite3')
    parser.add_argument('--output-dir', default='data')
    args = parser.parse_args(argv)
    if args.top < 1: parser.error('--top must be positive')
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    def report():
        rows = build_report(args.db, SETTINGS)
        return publish(rows, SETTINGS, args.output_dir, None if args.all else args.top, args.diverse)
    if args.report_only:
        report(); return 0
    with ProcessLock(args.db+'.historical.lock'):
        db = Storage(args.db)
        arc = ArcscanClient(SETTINGS.arcscan_api_url)
        rpc = ArcRPC(SETTINGS.rpc_url, SETTINGS.chain_id)
        try:
            rpc.sanity_check()
            collector = HistoricalCollector(db, rpc, arc, SETTINGS)
            while True:
                started = time.monotonic()
                result = collector.cycle()
                collected = time.monotonic()
                published = True
                try:
                    report()
                except ReportExportError as error:
                    published = False
                    logging.warning('Historical export deferred; collection continues | %s', error.safe_details)
                print({**result, 'collection_seconds': round(collected-started, 3),
                       'report_seconds': round(time.monotonic()-collected, 3),
                       'rpc_workers': SETTINGS.historical_rpc_workers,
                       'report_published': published,
                       'pause_seconds': 0 if args.once else SETTINGS.historical_poll_seconds}, flush=True)
                if args.once: return 0 if published else 1
                time.sleep(SETTINGS.historical_poll_seconds)
        except KeyboardInterrupt:
            return 0
        finally:
            db.db.close(); arc.s.close(); rpc.w3.provider.session.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # No raw exception body: transport/provider errors may contain credentials.
        import traceback
        from pathlib import Path
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print('Historical analysis failed | category='+type(error).__name__+
              ' site='+Path(frame.filename).name+':'+str(frame.lineno)+
              ' errno='+str(getattr(error, 'errno', None))+
              ' winerror='+str(getattr(error, 'winerror', None)), flush=True)
        raise SystemExit(1) from None
