"""Railway supervisor: V7.5 worker runs even with no browser connected."""
import os
import signal
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler,HTTPServer
from engine import Store,now_ms

class StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/stats15'):
            db=Store(); cutoff=now_ms()-15*60*1000
            with db.lock:
                rows=db.cx.execute('SELECT id,ts_ms,mint,stage,reason,payload FROM events WHERE ts_ms>=? ORDER BY id DESC LIMIT 1000',(cutoff,)).fetchall()
            body=json.dumps({'generated_ms':now_ms(),'stats':db.stats(),'events':[dict(zip(['id','ts_ms','mint','stage','reason','payload'],x)) for x in rows],'positions':db.snapshot().get('positions',[])},default=str).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body);return
        self.send_response(404);self.end_headers()
    def log_message(self,*a):pass

def stats_api():
    HTTPServer(('0.0.0.0',8081),StatsHandler).serve_forever()

def main():
    children=[]; stopping=False
    threading.Thread(target=stats_api,daemon=True).start()
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    try:
        children.append(subprocess.Popen([sys.executable,'worker.py']))
        children.append(subprocess.Popen([sys.executable,'-m','streamlit','run','app75.py',
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
