"""Run BBB worker + dashboard in one Railway service."""
from __future__ import annotations
import os, signal, subprocess, sys, time

ROOT=os.path.dirname(os.path.abspath(__file__))
port=os.environ.get("PORT","8501")
worker=subprocess.Popen([sys.executable,os.path.join(ROOT,"worker.py")],cwd=ROOT)

def stop(*_):
    if worker.poll() is None:
        worker.terminate()
    try: worker.wait(timeout=10)
    except Exception: worker.kill()
    raise SystemExit(0)

signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)

cmd=[sys.executable,"-m","streamlit","run",os.path.join(ROOT,"dashboard.py"),
     "--server.address=0.0.0.0","--server.port="+port,
     "--server.headless=true","--browser.gatherUsageStats=false"]
try:
    raise SystemExit(subprocess.call(cmd,cwd=ROOT))
finally:
    stop()
