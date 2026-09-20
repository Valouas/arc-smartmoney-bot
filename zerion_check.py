"""Read-only Zerion diagnostic; never writes wallet or selection state."""
import argparse
import json
from contextlib import closing

from config import SETTINGS
from external_http import ExternalError, wallet_address
from wallet_policy import infrastructure_addresses
from zerion_client import ZerionClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wallet')
    args = parser.parse_args(argv)
    try:
        wallet = wallet_address(args.wallet)
        if wallet in infrastructure_addresses(SETTINGS): raise ExternalError('infrastructure_excluded')
        if not SETTINGS.zerion_enabled:
            print('Zerion disabled; set ZERION_ENABLED=true to test.'); return 0
        with closing(ZerionClient(SETTINGS)) as client:
            print(json.dumps(client.pnl(wallet), indent=2, allow_nan=False))
        print('EXTERNAL DATA != LIVE SIGNAL AUTHORITY')
        return 0
    except ExternalError as error:
        print('Zerion unavailable | category='+error.category); return 1
    except Exception as error:
        print('Zerion unavailable | category='+type(error).__name__); return 1


if __name__ == '__main__': raise SystemExit(main())
