from __future__ import annotations
import subprocess,sys,time,os,signal

COMMANDS=[
    [sys.executable,"launch_indexer.py"],
    [sys.executable,"wallet_profiler.py"],
    [sys.executable,"auto_discovery.py"],
    [sys.executable,"bot.py"],
]

def commands_for(cfg):
    return (COMMANDS + ([[sys.executable,'historical_analysis.py']] if cfg.historical_analysis_enabled else [])
            + ([[sys.executable,'alchemy_webhook.py']] if cfg.alchemy_webhook_enabled else []))


def main():
    from config import SETTINGS
    commands = commands_for(SETTINGS)
    procs=[]
    disabled=set()
    try:
        for cmd in commands:
            print("starting:"," ".join(cmd))
            procs.append(subprocess.Popen(cmd))
            time.sleep(1)
        while True:
            for p,cmd in zip(procs,commands):
                if p in disabled: continue
                rc=p.poll()
                if rc is not None:
                    if cmd[-1]=='alchemy_webhook.py':
                        print('Webhook receiver unavailable; scanner/backfill continue.')
                        disabled.add(p); continue
                    raise RuntimeError(f"{cmd[-1]} exited with code {rc}")
            time.sleep(2)
    except KeyboardInterrupt:
        print("Stopping V4...")
    finally:
        # Ctrl+C also reaches child Python processes on Windows. Give handlers
        # time to close their HTTP sockets / finish SQLite transactions first.
        for p in procs:
            if p.poll() is not None: continue
            try:
                if os.name!='nt': p.send_signal(signal.SIGINT)
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:p.wait(timeout=2)
                except subprocess.TimeoutExpired:p.kill(); p.wait()

if __name__=="__main__":
    main()
