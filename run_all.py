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
import traceback
import urllib.request
from urllib.parse import urlsplit

from atomic_json import write_json
from process_lock import ProcessLock

ROOT=Path(__file__).resolve().parent
LOCK=Path('data/run_all.lock')
STATE=Path('data/run_all_state.json')
STOP=Path('data/run_all.stop')
LOGS=Path('data/launcher_logs')
ROLES=('launch_indexer','wallet_profiler','auto_discovery','bot','historical_analysis','alchemy_webhook','paper_copy','dashboard','automatic_backup','alchemy_sync')


def record_event(event, **details):
    """Best-effort operational journal, never raw exceptions, URLs or commands."""
    entry={'at':int(time.time()),'pid':os.getpid(),'event':event}
    allowed={'role','child_pid','exit_code','reason','category','operation','errno','winerror','site'}
    entry.update({k:v for k,v in details.items() if k in allowed and isinstance(v,(str,int,type(None)))})
    try:
        LOGS.mkdir(parents=True,exist_ok=True)
        with (LOGS/'supervisor.jsonl').open('a',encoding='utf-8') as target:
            target.write(json.dumps(entry,ensure_ascii=True)+'\n')
    except OSError:
        # Losing observability must never be turned into losing the scanners.
        try: print('Journal superviseur indisponible | event='+event,flush=True)
        except OSError: pass


def error_fields(error):
    frames=traceback.extract_tb(error.__traceback__)
    return {'category':type(error).__name__,'errno':getattr(error,'errno',None),
            'winerror':getattr(error,'winerror',None),
            'site':Path(frames[-1].filename).name+':'+str(frames[-1].lineno) if frames else None}


class StatePublisher:
    """Dashboard heartbeat only: bounded retries, then retry on the next cycle."""
    def __init__(self):
        self.degraded=False
        self.next_warning=0.0

    def publish(self, children, url, status):
        for attempt in range(3):
            try:
                save_state(children,url,status)
                if self.degraded: record_event('state_write_recovered',operation='publish_state')
                self.degraded=False
                return True
            except OSError as error:
                if attempt<2:
                    time.sleep(.05*(2**attempt))
                    continue
                if not self.degraded or time.monotonic()>=self.next_warning:
                    record_event('state_write_failed',operation='publish_state',**error_fields(error))
                    try: print('Etat du dashboard indisponible temporairement ; bots maintenus.',flush=True)
                    except OSError: pass
                    self.next_warning=time.monotonic()+60
                self.degraded=True
                return False


def roles_for(cfg):
    roles=list(ROLES[:4])
    if getattr(cfg,'research_paper_only',False):
        roles=['launch_indexer','auto_discovery']
    if cfg.historical_analysis_enabled: roles.append('historical_analysis')
    if cfg.alchemy_webhook_enabled: roles.append('alchemy_webhook')
    if cfg.alchemy_webhook_enabled and getattr(cfg,'alchemy_sync_enabled',False): roles.append('alchemy_sync')
    roles += ['paper_copy','dashboard']
    if getattr(cfg,'automatic_backup_enabled',False): roles.append('automatic_backup')
    return roles


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


def launch_worker(role):
    # Native crashes otherwise bypass every Python exception handler. The
    # diagnostic contains stacks, not locals, request bodies or credentials.
    return launch([sys.executable,'-X','faulthandler','-u',str(ROOT/'run_all.py'),
                   '--worker',role],LOGS/(role+'.log'))


class HistoricalRecovery:
    """Bounded recovery of the independent research worker, never of paper.

    No preparation/export/reset runs during recovery. The worker resumes its
    persisted queue under the same locks. Three launches per supervisor run
    prevents an unattended crash/reconstruction loop from consuming credit.
    """
    delays=(5,30,120)

    def __init__(self):
        self.attempts=0
        self.due=None
        self.disabled=False

    def tick(self, children):
        role='historical_analysis'
        p=children.get(role)
        if p is None or p.poll() is None or self.disabled: return
        if self.due is None:
            record_event('child_exited',role=role,child_pid=p.pid,exit_code=p.poll())
            self.schedule()
        if self.disabled or time.monotonic()<self.due or STOP.exists(): return
        # A redirector may have exited while an actual worker still owns the
        # database. Never launch over that owner or kill an unowned process.
        self.attempts+=1
        try:
            if busy('data/historical_analysis.worker.lock') or busy('data/arc_bot.sqlite3.historical.lock'):
                self.disable('ownership_busy')
                return
            children[role]=launch_worker(role)
        except Exception as error:
            record_event('historical_restart_failed',role=role,**error_fields(error))
            self.schedule()
            return
        self.due=None
        record_event('historical_restarted',role=role,child_pid=children[role].pid,
                     reason='attempt_'+str(self.attempts))
        print('Analyse historique relancee depuis ses checkpoints | tentative='+str(self.attempts),flush=True)

    def schedule(self):
        if self.attempts>=len(self.delays):
            self.disable('restart_limit')
            return
        delay=self.delays[self.attempts]
        self.due=time.monotonic()+delay
        record_event('historical_restart_scheduled',role='historical_analysis',reason='delay_'+str(delay))
        print('Analyse historique arretee ; reprise dans '+str(delay)+' s. Paper live maintenu.',flush=True)

    def disable(self, reason):
        self.disabled=True
        record_event('historical_recovery_stopped',role='historical_analysis',reason=reason)
        print('Analyse historique arretee : intervention requise | reason='+reason+'. Paper live maintenu.',flush=True)


def shutdown(children,grace=25):
    """Stop only children we created; a reused tunnel is never in this list."""
    alive=[p for p in children.values() if p.poll() is None]
    names={id(p):role for role,p in children.items()}
    for p in alive:
        record_event('child_stop_signal',role=names[id(p)],child_pid=p.pid)
        try: p.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGTERM)
        except OSError: pass
    deadline=time.monotonic()+grace
    while any(p.poll() is None for p in alive) and time.monotonic()<deadline:
        time.sleep(.2)
    for p in alive:
        if p.poll() is None:
            record_event('child_force_stop',role=names[id(p)],child_pid=p.pid,reason='grace_timeout')
            if os.name=='nt':
                # The venv redirector and its real Python child form one owned tree.
                subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else: p.kill()
        try: p.wait(timeout=5)
        except subprocess.TimeoutExpired: pass


def prepare(cfg):
    """Regenerate exports only; never run a second historical collection worker."""
    if not Path('data/arc_bot.sqlite3').exists(): return
    from historical_report import build_report,publish,ReportExportError
    from paper_copy.selection import load_selection
    from paper_admission import DEFAULT_SELECTION
    from paper_copy.webhook import watch_addresses
    from watchlist_manager import WatchlistManager,effective_watchlist
    import sqlite3
    rows=build_report('data/arc_bot.sqlite3',cfg)
    try:
        with (LOGS/'preparation.log').open('a',encoding='utf-8') as log,contextlib.redirect_stdout(log):
            publish(rows,cfg,top=4)
    except ReportExportError as error:
        print('Export historique differe ; collecte maintenue | '+error.safe_details,flush=True)
    selected=load_selection(DEFAULT_SELECTION,cfg,int(time.time()))
    print('Wallets admis en paper : '+str(len(selected)),flush=True)
    path=Path('data/arc_bot.sqlite3').resolve()
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        addresses=set(effective_watchlist(WatchlistManager(),db,cfg))|watch_addresses(cfg)
    write_json('data/alchemy_addresses.json',{'addresses':sorted(addresses)})
    mode='ajout automatique des wallets paper' if getattr(cfg,'alchemy_sync_enabled',False) else 'synchronisation distante manuelle'
    print('Liste Alchemy : data/alchemy_addresses.json ('+mode+')',flush=True)


def save_state(children,url,status):
    write_json(STATE,{'pid':os.getpid(),'updated_at':int(time.time()),'status':status,'webhook_url':url,
                     'processes':{role:{'pid':p.pid,'exit_code':p.poll()} for role,p in children.items()}})


def supervise(cfg):
    record_event('supervisor_start')
    preflight(cfg)
    LOGS.mkdir(parents=True,exist_ok=True)
    STOP.unlink(missing_ok=True)
    if getattr(cfg,'automatic_backup_enabled',False):
        from automatic_backup import create_backup
        print('Sauvegarde de securite avant demarrage...',flush=True)
        generation=create_backup(cfg)
        record_event('startup_backup_verified' if generation else 'startup_backup_no_existing_database')
        if generation: print('Sauvegarde SQLite verifiee avant demarrage.',flush=True)
    prepare(cfg)
    if getattr(cfg,'research_paper_only',False):
        print('Mode RECHERCHE + PAPER : scanner V4 et profiler forward suspendus ; checkpoints conserves.',flush=True)
    children={}; url=None; warned=set(); publisher=StatePublisher(); recovery=HistoricalRecovery()
    reason='startup_failure'; operation='launch_child'
    try:
        for role in roles_for(cfg):
            children[role]=launch_worker(role)
            record_event('child_started',role=role,child_pid=children[role].pid)
            print('Demarre : '+role,flush=True)
        operation='start_tunnel'
        if cfg.alchemy_webhook_enabled:
            url=tunnel(cfg.alchemy_webhook_port)
            if url: print('Tunnel ngrok existant conserve : '+url,flush=True)
            else:
                binary=shutil.which('ngrok')
                if binary:
                    children['ngrok']=launch([binary,'http','http://127.0.0.1:'+str(cfg.alchemy_webhook_port)],LOGS/'ngrok.log')
                    record_event('child_started',role='ngrok',child_pid=children['ngrok'].pid)
                else: print('Ngrok introuvable. Les scanners RPC restent actifs.',flush=True)
        print('Ensemble lance. Ctrl+C ou ARRETER_BOT.bat pour arreter.',flush=True)
        print('Tableau de bord : http://127.0.0.1:8001 | TABLEAU_DE_BORD.bat',flush=True)
        print('Journaux : data/launcher_logs | Etat : ETAT_BOT.bat',flush=True)
        record_event('supervisor_running')
        reason='unexpected_error'
        while not STOP.exists():
            operation='check_children'
            recovery.tick(children)
            for role,p in children.items():
                if role=='historical_analysis': continue
                if p.poll() is None or role in warned: continue
                warned.add(role)
                record_event('child_exited',role=role,child_pid=p.pid,exit_code=p.poll())
                if role in ('ngrok','alchemy_webhook','dashboard','alchemy_sync'):
                    print(role+' indisponible ; scanners RPC maintenus. Consulter son journal.',flush=True)
                else:
                    reason='core_worker_exit'
                    raise RuntimeError(role+' est arrete. Consulter data/launcher_logs/'+role+'.log')
            operation='check_tunnel'
            if cfg.alchemy_webhook_enabled and not url:
                url=tunnel(cfg.alchemy_webhook_port)
                if url: print('URL Alchemy : '+url,flush=True)
            operation='publish_state'
            publisher.publish(children,url,'RUNNING')
            operation='wait'
            time.sleep(1)
        reason='stop_file'
        record_event('stop_requested',reason=reason)
    except KeyboardInterrupt:
        reason='keyboard_interrupt'
        record_event('stop_requested',reason=reason)
        print('Arret en cours...',flush=True)
    except Exception as error:
        record_event('supervisor_error',operation=operation,reason=reason,**error_fields(error))
        raise
    finally:
        record_event('shutdown_started',reason=reason)
        shutdown(children)
        publisher.publish(children,url,'STOPPED')
        try: STOP.unlink(missing_ok=True)
        except OSError as error: record_event('stop_file_cleanup_failed',**error_fields(error))
        record_event('supervisor_stopped',reason=reason)


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
        record_event('stop_command_issued',reason='cli_stop')
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
        record_event('launcher_failed',**error_fields(error))
        print(str(error),flush=True); return 1
    except Exception as error:
        record_event('launcher_failed',**error_fields(error))
        print('Lancement impossible | category='+type(error).__name__,flush=True); return 1


if __name__=='__main__': raise SystemExit(main())
