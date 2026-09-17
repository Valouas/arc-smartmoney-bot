"""One local supervisor for V4, signed webhook, paper copy and optional ngrok."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from atomic_json import write_json
from process_lock import ProcessLock

ROOT=Path(__file__).resolve().parent
LOCK=Path('data/run_all.lock')
STATE=Path('data/run_all_state.json')
STOP=Path('data/run_all.stop')
LOGS=Path('data/launcher_logs')
ROLES=('launch_indexer','wallet_profiler','auto_discovery','bot','historical_analysis','alchemy_webhook','paper_copy')


def roles_for(cfg):
    roles=list(ROLES[:4])
    if cfg.historical_analysis_enabled: roles.append('historical_analysis')
    if cfg.alchemy_webhook_enabled: roles.append('alchemy_webhook')
    return roles+['paper_copy']


def busy(path):
    try:
        with ProcessLock(path): pass
        return False
    except RuntimeError: return True


def preflight(cfg):
    for path in ('data/arc_bot.sqlite3.scanner.lock','data/arc_bot.sqlite3.historical.lock','data/paper_copy.sqlite3.lock'):
        if busy(path): raise RuntimeError('Une ancienne instance du bot tourne encore. Arreter ses fenetres avec Ctrl+C.')
    if cfg.alchemy_webhook_enabled:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex((cfg.alchemy_webhook_host,cfg.alchemy_webhook_port))==0:
                raise RuntimeError('Le port webhook est deja utilise. Arreter son ancienne instance.')


def tunnel(port):
    """Read only the local ngrok API; never inspect/print the auth token."""
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:4040/api/tunnels',timeout=1) as response:
            data=json.load(response)
        for item in data.get('tunnels',[]):
            public=urlsplit(item.get('public_url',''))
            upstream=urlsplit(str(item.get('config',{}).get('addr','')))
            if (public.scheme=='https' and public.hostname and not public.username
                    and not public.query and not public.fragment
                    and upstream.hostname in ('127.0.0.1','localhost','::1') and upstream.port==port):
                return public.scheme+'://'+public.netloc+'/webhook/alchemy'
    except (OSError,ValueError,TypeError,AttributeError): pass
    return None


def worker(role):
    def interrupt(*_): raise KeyboardInterrupt()
    if hasattr(signal,'SIGBREAK'): signal.signal(signal.SIGBREAK,interrupt)
    signal.signal(signal.SIGTERM,interrupt)
    # Independent role locks also guard repeated launches before scanner startup.
    if role not in ROLES: raise ValueError('Unknown worker')
    with ProcessLock('data/'+role+'.worker.lock'):
        try:
            if role=='paper_copy':
                sys.argv=['paper_copy']
                runpy.run_module('paper_copy',run_name='__main__')
            else:
                sys.argv=[role+'.py']
                runpy.run_path(role+'.py',run_name='__main__')
        except KeyboardInterrupt: pass


def launch(command,logfile):
    flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0
    with logfile.open('ab',buffering=0) as output:
        return subprocess.Popen(command,stdout=output,stderr=output,cwd=ROOT,
                                creationflags=flags,start_new_session=os.name!='nt')


def shutdown(children,grace=25):
    """Stop only children we created; a reused tunnel is never in this list."""
    alive=[p for p in children.values() if p.poll() is None]
    for p in alive:
        try: p.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGTERM)
        except OSError: pass
    deadline=time.monotonic()+grace
    while any(p.poll() is None for p in alive) and time.monotonic()<deadline:
        time.sleep(.2)
    for p in alive:
        if p.poll() is None:
            if os.name=='nt':
                # The venv redirector and its real Python child form one owned tree.
                subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else: p.kill()
        try: p.wait(timeout=5)
        except subprocess.TimeoutExpired: pass


def prepare(cfg):
    """Regenerate exports only; never run a second historical collection worker."""
    if not Path('data/arc_bot.sqlite3').exists(): return
    from historical_report import build_report,publish
    from paper_copy.selection import load_selection
    from paper_admission import DEFAULT_SELECTION
    from paper_copy.webhook import watch_addresses
    from watchlist_manager import WatchlistManager,effective_watchlist
    import sqlite3
    rows=build_report('data/arc_bot.sqlite3',cfg)
    with (LOGS/'preparation.log').open('a',encoding='utf-8') as log,contextlib.redirect_stdout(log):
        publish(rows,cfg,top=4)
    selected=load_selection(DEFAULT_SELECTION,cfg,int(time.time()))
    print('Wallets admis en paper : '+str(len(selected)),flush=True)
    path=Path('data/arc_bot.sqlite3').resolve()
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        addresses=set(effective_watchlist(WatchlistManager(),db,cfg))|watch_addresses(cfg)
    write_json('data/alchemy_addresses.json',{'addresses':sorted(addresses)})
    print('Liste Alchemy : data/alchemy_addresses.json (synchronisation distante manuelle)',flush=True)


def save_state(children,url,status):
    write_json(STATE,{'pid':os.getpid(),'updated_at':int(time.time()),'status':status,'webhook_url':url,
                     'processes':{role:{'pid':p.pid,'exit_code':p.poll()} for role,p in children.items()}})


def supervise(cfg):
    preflight(cfg)
    LOGS.mkdir(parents=True,exist_ok=True)
    STOP.unlink(missing_ok=True)
    prepare(cfg)
    children={}; url=None; warned=set()
    try:
        for role in roles_for(cfg):
            children[role]=launch([sys.executable,'-u',str(ROOT/'run_all.py'),'--worker',role],LOGS/(role+'.log'))
            print('Demarre : '+role,flush=True)
        if cfg.alchemy_webhook_enabled:
            url=tunnel(cfg.alchemy_webhook_port)
            if url: print('Tunnel ngrok existant conserve : '+url,flush=True)
            else:
                binary=shutil.which('ngrok')
                if binary:
                    children['ngrok']=launch([binary,'http','http://127.0.0.1:'+str(cfg.alchemy_webhook_port)],LOGS/'ngrok.log')
                else: print('Ngrok introuvable. Les scanners RPC restent actifs.',flush=True)
        print('Ensemble lance. Ctrl+C ou ARRETER_BOT.bat pour arreter.',flush=True)
        print('Journaux : data/launcher_logs | Etat : ETAT_BOT.bat',flush=True)
        while not STOP.exists():
            for role,p in children.items():
                if p.poll() is None or role in warned: continue
                warned.add(role)
                if role in ('ngrok','alchemy_webhook'):
                    print(role+' indisponible ; scanners RPC maintenus. Consulter son journal.',flush=True)
                else: raise RuntimeError(role+' est arrete. Consulter data/launcher_logs/'+role+'.log')
            if cfg.alchemy_webhook_enabled and not url:
                url=tunnel(cfg.alchemy_webhook_port)
                if url: print('URL Alchemy : '+url,flush=True)
            save_state(children,url,'RUNNING')
            time.sleep(1)
    except KeyboardInterrupt: print('Arret en cours...',flush=True)
    finally:
        shutdown(children)
        save_state(children,url,'STOPPED')
        STOP.unlink(missing_ok=True)


def main(argv=None):
    os.chdir(ROOT)
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--worker',choices=ROLES,help=argparse.SUPPRESS)
    group.add_argument('--stop',action='store_true')
    group.add_argument('--status',action='store_true')
    args=parser.parse_args(argv)
    if args.worker: worker(args.worker); return 0
    if args.stop:
        if not busy(LOCK): print('Aucun ensemble gere par ce lanceur.'); return 0
        STOP.write_text('stop',encoding='ascii')
        print('Arret demande ; attente de fermeture des processus...',flush=True)
        deadline=time.monotonic()+40
        while busy(LOCK) and time.monotonic()<deadline: time.sleep(.5)
        if busy(LOCK): print('Arret encore en cours ; consulter la fenetre du lanceur.'); return 1
        print('Ensemble arrete. Donnees conservees.'); return 0
    if args.status:
        active=busy(LOCK)
        print('Lanceur : '+('ACTIF' if active else 'ARRETE'))
        if STATE.exists(): print(STATE.read_text(encoding='utf-8'))
        return 0
    if busy(LOCK): print('Le lanceur est deja actif. Utiliser ETAT_BOT.bat.'); return 0
    from config import SETTINGS
    try:
        with ProcessLock(LOCK): supervise(SETTINGS)
        return 0
    except RuntimeError as error:
        print(str(error),flush=True); return 1
    except Exception as error:
        print('Lancement impossible | category='+type(error).__name__,flush=True); return 1


if __name__=='__main__': raise SystemExit(main())
