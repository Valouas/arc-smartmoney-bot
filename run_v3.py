from __future__ import annotations
import subprocess, sys, time

COMMANDS = [
    [sys.executable, "launch_indexer.py"],
    [sys.executable, "wallet_profiler.py"],
    [sys.executable, "bot.py"],
]

def main():
    procs = []
    try:
        for cmd in COMMANDS:
            print("starting:", " ".join(cmd))
            procs.append(subprocess.Popen(cmd))
            time.sleep(1)
        while True:
            for p, cmd in zip(procs, COMMANDS):
                rc = p.poll()
                if rc is not None:
                    raise RuntimeError(f"{cmd[-1]} exited with code {rc}")
            time.sleep(2)
    except KeyboardInterrupt:
        print("Stopping V3...")
    finally:
        for p in procs:
            if p.poll() is None: p.terminate()
        for p in procs:
            try: p.wait(timeout=5)
            except Exception: p.kill()

if __name__ == "__main__":
    main()
