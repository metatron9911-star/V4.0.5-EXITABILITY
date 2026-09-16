"""Railway supervisor: worker runs even with no browser connected."""
import os
import signal
import subprocess
import sys
import time

def main():
    children=[]; stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try:
        children.append(subprocess.Popen([sys.executable,'worker.py']))
        children.append(subprocess.Popen([sys.executable,'-m','streamlit','run','app.py',
            '--server.address=0.0.0.0','--server.port='+os.getenv('PORT','8501'),
            '--server.headless=true']))
        while not stopping:
            if any(p.poll() is not None for p in children): return 1
            time.sleep(.5)
        return 0
    finally:
        for p in children:
            if p.poll() is None:p.terminate()
        for p in children:
            try:p.wait(timeout=25)
            except subprocess.TimeoutExpired:p.kill();p.wait()
if __name__=='__main__':sys.exit(main())
