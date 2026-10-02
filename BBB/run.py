"""Run BBB worker + dashboard in one Railway service with worker supervision."""
from __future__ import annotations
import os, signal, subprocess, sys, time

ROOT=os.path.dirname(os.path.abspath(__file__))
port=os.environ.get("PORT","8501")

worker=None
dashboard=None

def terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()

def stop(*_):
    terminate(worker)
    terminate(dashboard)
    raise SystemExit(0)

signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)

worker=subprocess.Popen([sys.executable,os.path.join(ROOT,"worker.py")],cwd=ROOT)
cmd=[sys.executable,"-m","streamlit","run",os.path.join(ROOT,"dashboard.py"),
     "--server.address=0.0.0.0","--server.port="+port,
     "--server.headless=true","--browser.gatherUsageStats=false"]
dashboard=subprocess.Popen(cmd,cwd=ROOT)

try:
    while True:
        wrc=worker.poll()
        drc=dashboard.poll()
        if wrc is not None:
            print(f"BBB worker exited rc={wrc}; terminating dashboard so Railway restarts the service",flush=True)
            terminate(dashboard)
            raise SystemExit(wrc if wrc else 1)
        if drc is not None:
            print(f"BBB dashboard exited rc={drc}; terminating worker",flush=True)
            terminate(worker)
            raise SystemExit(drc if drc else 1)
        time.sleep(1)
finally:
    terminate(worker)
    terminate(dashboard)
