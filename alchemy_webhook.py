"""Bounded local HTTP ingress. RPC, classification and paper execution stay in bot.py."""
import argparse
import hashlib
import hmac
import json
import re
import socket
import sqlite3
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import SETTINGS
from storage import Storage
from watchlist_manager import WatchlistManager, effective_watchlist
from webhook_queue import SCHEMA, MAX_BODY, accept, InvalidWebhook
from paper_copy.webhook import watch_addresses as paper_watch_addresses
from paper_admission import DEFAULT_SELECTION


class WebhookServer(ThreadingHTTPServer):
    daemon_threads=False
    def __init__(self,address,handler):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(address,handler)

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        try: super().process_request(request,address)
        except BaseException:
            self.slots.release(); raise

    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.slots.release()

    def handle_error(self,request,address):
        # Never dump a request, signature or body in an HTTP exception log.
        print('Alchemy webhook request failed',flush=True)


def create_server(cfg=SETTINGS,db_path='data/arc_bot.sqlite3',watch_factory=WatchlistManager,port=None,
                  paper_watch_factory=None):
    if cfg.chain_id!=5042: raise ValueError('Alchemy webhook requires Arc chain 5042')
    storage=Storage(db_path)
    storage.db.executescript(SCHEMA); storage.db.close()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self,*args): pass

        def reply(self,status,payload):
            body=json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Connection','close')
            self.end_headers(); self.wfile.write(body); self.close_connection=True

        def do_GET(self):
            if self.path!='/health': return self.reply(404,{'error':'not_found'})
            self.reply(200,{'status':'ok','ingestion_ready':bool(cfg.alchemy_webhook_enabled and cfg.alchemy_webhook_signing_key)})

        def do_POST(self):
            if self.path!='/webhook/alchemy': return self.reply(404,{'error':'not_found'})
            if not cfg.alchemy_webhook_enabled or not cfg.alchemy_webhook_signing_key:
                return self.reply(503,{'error':'webhook_not_configured'})
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Encoding'):
                return self.reply(400,{'error':'unsupported_encoding'})
            lengths=self.headers.get_all('Content-Length',[])
            if len(lengths)!=1 or not re.fullmatch(r'[0-9]{1,10}',lengths[0]):
                return self.reply(411,{'error':'content_length_required'})
            length=int(lengths[0])
            if not 0 < length <= MAX_BODY: return self.reply(413,{'error':'payload_size'})
            signatures=self.headers.get_all('X-Alchemy-Signature',[])
            if len(signatures)!=1 or not re.fullmatch('[0-9a-fA-F]{64}',signatures[0]):
                return self.reply(401,{'error':'invalid_signature'})
            if self.headers.get_content_type()!='application/json': return self.reply(415,{'error':'content_type'})
            try:
                body=self.rfile.read(length)
                if len(body)!=length: return self.reply(400,{'error':'incomplete_body'})
                expected=hmac.new(cfg.alchemy_webhook_signing_key.encode(),body,hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected,signatures[0].lower()):
                    return self.reply(401,{'error':'invalid_signature'})
                db=sqlite3.connect(db_path,timeout=1)
                try:
                    watch=effective_watchlist(watch_factory(),db,cfg)
                    paper_watch=(paper_watch_factory() if paper_watch_factory else paper_watch_addresses(cfg))
                    result=accept(db,body,watch,paper_watch=paper_watch)
                finally: db.close()
                self.reply(200,result)  # Durable acceptance, not synchronous RPC work.
            except InvalidWebhook:
                self.reply(400,{'error':'invalid_payload'})
            except (socket.timeout,TimeoutError):
                self.close_connection=True
            except Exception:
                self.reply(503,{'error':'acceptance_unavailable'})
    return WebhookServer((cfg.alchemy_webhook_host,cfg.alchemy_webhook_port if port is None else port),Handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default='data/arc_bot.sqlite3')
    parser.add_argument('--addresses',action='store_true',help='Print effective wallet addresses for the Alchemy dashboard, without network calls')
    parser.add_argument('--paper-addresses',action='store_true',help='Print only admitted paper wallets and existing exits')
    parser.add_argument('--paper-db',default='data/paper_copy.sqlite3')
    parser.add_argument('--paper-selection',default=DEFAULT_SELECTION)
    args=parser.parse_args()
    paper_watch=lambda:paper_watch_addresses(SETTINGS,args.paper_selection,args.paper_db)
    if args.paper_addresses:
        print(json.dumps({'addresses':sorted(paper_watch())},indent=2)); return
    if args.addresses:
        db=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try: print(json.dumps({'addresses':sorted(set(effective_watchlist(WatchlistManager(),db,SETTINGS))|paper_watch())},indent=2))
        finally: db.close()
        return
    server=create_server(db_path=args.db,paper_watch_factory=paper_watch)
    print('Alchemy webhook listening | port='+str(SETTINGS.alchemy_webhook_port),flush=True)
    try: server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
