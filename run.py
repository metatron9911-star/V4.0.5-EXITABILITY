import os,signal,subprocess,sys,time,threading,json,asyncio
from aiohttp import web,ClientSession,WSMsgType
# Gateway reads the single V7.9 experiment directory. Child wrappers append /v79
# themselves, so pass them the base DATA_DIR to avoid /v79/v79 divergence.
BASE_DATA_DIR=os.getenv('DATA_DIR','/data')
os.environ['DATA_DIR']=os.path.join(BASE_DATA_DIR,'v79')
from engine import Store,now_ms
INTERNAL=8502
async def stats15(request):
 db=Store();cutoff=now_ms()-15*60*1000
 with db.lock:rows=db.cx.execute('SELECT id,ts_ms,mint,stage,reason,payload FROM events WHERE ts_ms>=? ORDER BY id DESC LIMIT 1500',(cutoff,)).fetchall()
 return web.json_response({'generated_ms':now_ms(),'stats':db.stats(),'events':[dict(zip(['id','ts_ms','mint','stage','reason','payload'],x)) for x in rows],'positions':db.snapshot().get('positions',[])})
async def proxy(request):
 target=f'http://127.0.0.1:{INTERNAL}{request.rel_url}'
 if request.headers.get('Upgrade','').lower()=='websocket':
  ws=web.WebSocketResponse(protocols=['streamlit']);await ws.prepare(request)
  async with ClientSession() as s:
   async with s.ws_connect(target,protocols=['streamlit']) as up:
    async def a():
     async for m in ws:
      if m.type==WSMsgType.TEXT:await up.send_str(m.data)
      elif m.type==WSMsgType.BINARY:await up.send_bytes(m.data)
    async def b():
     async for m in up:
      if m.type==WSMsgType.TEXT:await ws.send_str(m.data)
      elif m.type==WSMsgType.BINARY:await ws.send_bytes(m.data)
    await asyncio.gather(a(),b())
  return ws
 body=await request.read();headers={k:v for k,v in request.headers.items() if k.lower() not in ('host','content-length')}
 async with ClientSession() as s:
  async with s.request(request.method,target,data=body,headers=headers,allow_redirects=False) as x:
   data=await x.read();h={k:v for k,v in x.headers.items() if k.lower() not in ('content-length','transfer-encoding','connection','content-encoding')};return web.Response(body=data,status=x.status,headers=h)
def gateway():
 app=web.Application();app.router.add_get('/stats15',stats15);app.router.add_route('*','/{tail:.*}',proxy);web.run_app(app,host='0.0.0.0',port=int(os.getenv('PORT','8501')),handle_signals=False)
def main():
 children=[];stopping=False
 child_env=os.environ.copy();child_env['DATA_DIR']=BASE_DATA_DIR
 def stop(*_):
  nonlocal stopping;stopping=True
 signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
 try:
  children.append(subprocess.Popen([sys.executable,'worker.py'],env=child_env))
  children.append(subprocess.Popen([sys.executable,'-m','streamlit','run','app75.py','--server.address=127.0.0.1','--server.port='+str(INTERNAL),'--server.headless=true'],env=child_env))
  threading.Thread(target=gateway,daemon=True).start()
  while not stopping:
   if any(p.poll() is not None for p in children):return 1
   time.sleep(.5)
  return 0
 finally:
  for p in children:
   if p.poll() is None:p.terminate()
if __name__=='__main__':sys.exit(main())
