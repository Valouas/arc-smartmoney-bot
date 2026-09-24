"""Add paper watch addresses to one explicitly configured Alchemy webhook.

Notification subscriptions are not trading authority. Never remove remote entries,
create webhooks, change their destination, or touch scanner/paper checkpoints.
"""
import argparse
import hmac
import re
import time

import requests

from atomic_json import write_json
from config import SETTINGS
from paper_copy.webhook import watch_addresses
from process_lock import ProcessLock

BASE = 'https://dashboard.alchemy.com/api/'
STATUS = 'data/alchemy_sync_status.json'


class SyncError(RuntimeError):
    """Only fixed error categories, never server bodies or credentials."""


def addresses(values):
    if not isinstance(values,(list,set,tuple)) or any(
            not isinstance(v,str) or not re.fullmatch(r'0x[0-9a-fA-F]{40}',v) for v in values):
        raise SyncError('invalid_addresses')
    return {v.lower() for v in values}


class NotifyClient:
    def __init__(self, cfg, session=None):
        self.cfg = cfg
        self.session = session or requests.Session()

    def request(self, method, route, **kwargs):
        try:
            r = self.session.request(method,BASE+route,
                headers={'X-Alchemy-Token':self.cfg.alchemy_notify_auth_token},
                timeout=15,allow_redirects=False,**kwargs)
        except requests.RequestException:
            raise SyncError('transport') from None
        if r.status_code != 200:
            raise SyncError('http_'+str(r.status_code))
        try:
            result = r.json()
        except ValueError:
            raise SyncError('invalid_json') from None
        if not isinstance(result,dict):
            raise SyncError('invalid_schema')
        return result

    def verify(self):
        cfg = self.cfg
        if not all((cfg.alchemy_notify_auth_token,cfg.alchemy_webhook_id,
                    cfg.alchemy_webhook_expected_url,cfg.alchemy_webhook_signing_key)):
            raise SyncError('missing_configuration')
        rows = self.request('GET','team-webhooks').get('data')
        if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):
            raise SyncError('invalid_webhooks')
        found = [r for r in rows if r.get('id')==cfg.alchemy_webhook_id]
        if len(found)!=1:
            raise SyncError('webhook_not_unique')
        hook = found[0]
        if (hook.get('network')!='ARC_MAINNET' or hook.get('webhook_type')!='ADDRESS_ACTIVITY'
                or hook.get('is_active') is not True
                or hook.get('webhook_url')!=cfg.alchemy_webhook_expected_url):
            raise SyncError('webhook_configuration_mismatch')
        key = hook.get('signing_key')
        if not isinstance(key,str) or not hmac.compare_digest(key.encode(),cfg.alchemy_webhook_signing_key.encode()):
            raise SyncError('signing_key_mismatch')

    def remote_addresses(self):
        result, cursors, cursor, expected = set(), set(), None, None
        for _ in range(1001):
            params = {'webhook_id':self.cfg.alchemy_webhook_id,'limit':100}
            if cursor: params['after'] = cursor
            data = self.request('GET','webhook-addresses',params=params)
            batch = addresses(data.get('data'))
            if len(batch)!=len(data['data']) or result & batch:
                raise SyncError('duplicate_address_page')
            pagination = data.get('pagination')
            if not isinstance(pagination,dict) or not isinstance(pagination.get('cursors'),dict):
                raise SyncError('invalid_pagination')
            total = pagination.get('total_count')
            if type(total) is not int or not 0 <= total <= 100000:
                raise SyncError('invalid_total')
            if expected is not None and expected!=total:
                raise SyncError('changing_address_list')
            expected = total
            result.update(batch)
            cursor = pagination['cursors'].get('after')
            if cursor in (None,''):
                if len(result)!=expected: raise SyncError('incomplete_address_list')
                return result
            if not isinstance(cursor,str) or cursor in cursors or not batch or len(result)>=expected:
                raise SyncError('invalid_cursor')
            cursors.add(cursor)
        raise SyncError('pagination_limit')

    def sync(self, desired):
        desired = addresses(desired)
        self.verify()
        remote = self.remote_addresses()
        missing = sorted(desired-remote)
        for offset in range(0,len(missing),100):
            self.request('PATCH','update-webhook-addresses',json={
                'webhook_id':self.cfg.alchemy_webhook_id,
                'addresses_to_add':missing[offset:offset+100],'addresses_to_remove':[]})
        # Success is based on a fresh read, never merely a successful PATCH.
        after = self.remote_addresses() if missing else remote
        if not desired <= after: raise SyncError('verification_incomplete')
        return {'status':'OK','checked_at':int(time.time()),'desired_count':len(desired),
                'remote_count':len(after),'added_count':len(missing)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args(argv)
    if not SETTINGS.alchemy_sync_enabled:
        print('Alchemy address sync disabled'); return 0
    with ProcessLock('data/alchemy_sync.lock'):
        client = NotifyClient(SETTINGS)
        try:
            while True:
                try:
                    result = client.sync(watch_addresses(SETTINGS))
                    write_json(STATUS,result)
                    print('Alchemy sync OK | paper='+str(result['desired_count'])+
                          ' | remote='+str(result['remote_count'])+' | added='+str(result['added_count']),flush=True)
                    code = 0
                except Exception as error:
                    category = str(error) if isinstance(error,SyncError) else type(error).__name__
                    write_json(STATUS,{'status':'ERROR','checked_at':int(time.time()),'category':category})
                    print('Alchemy sync deferred | category='+category,flush=True)
                    code = 1
                if args.once: return code
                time.sleep(SETTINGS.alchemy_sync_seconds)
        except KeyboardInterrupt: return 0
        finally: client.session.close()


if __name__=='__main__': raise SystemExit(main())
