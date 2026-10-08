#!/usr/bin/env python3
# 90-day Bybit SPOT 15m walk-forward research.
# Public REST only. No API key, no orders, no trading-state mutation.
import sqlite3, urllib.request, urllib.parse, json, time, statistics, math, itertools, sys

BEST_ONLY="--best-only" in sys.argv

BASE="https://api.bybit.com"
CATEGORY="spot"
INTERVAL="15"
DAYS=90
LIMIT=1000
ROUNDTRIP_FEE=0.002  # 0.1% in + 0.1% out conservative baseline
CACHE="/data/bbb_spot90d_cache.json"

cx=sqlite3.connect("/data/bbb.sqlite3")

def api(path,params):
    url=BASE+path+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"BBB-research/1.0"})
    with urllib.request.urlopen(req,timeout=20) as resp:
        obj=json.loads(resp.read().decode())
    if int(obj.get("retCode",0))!=0:
        raise RuntimeError(obj)
    return obj

def fetch_spot_symbols():
    out=set(); cursor=""
    for _ in range(20):
        params={"category":"spot","limit":1000}
        if cursor: params["cursor"]=cursor
        obj=api("/v5/market/instruments-info",params)
        res=obj.get("result",{}) or {}
        for it in res.get("list",[]) or []:
            sym=str(it.get("symbol") or "")
            status=str(it.get("status") or "")
            quote=str(it.get("quoteCoin") or "")
            if sym and quote=="USDT" and status in ("Trading","Available",""):
                out.add(sym)
        cursor=str(res.get("nextPageCursor") or "")
        if not cursor: break
    return out

candidate_symbols=sorted(set(r[0] for r in cx.execute("SELECT DISTINCT symbol FROM feature_samples ORDER BY symbol")))
spot_symbols=fetch_spot_symbols()
symbols=[sym for sym in candidate_symbols if sym in spot_symbols]
unsupported=[sym for sym in candidate_symbols if sym not in spot_symbols]
if not BEST_ONLY: print("CANDIDATES",len(candidate_symbols),"EXACT_SPOT_MATCH",len(symbols),"UNSUPPORTED",len(unsupported),flush=True)
if unsupported and not BEST_ONLY:
    print("UNSUPPORTED_SAMPLE",unsupported[:25],flush=True)

def fetch_klines(symbol,days=DAYS):
    end=int(time.time()*1000)
    start=end-days*86400000
    rows={}
    cursor_end=end
    for _ in range(20):
        try:
            obj=api("/v5/market/kline",{"category":CATEGORY,"symbol":symbol,"interval":INTERVAL,"limit":LIMIT,"end":cursor_end})
        except Exception as e:
            print("FETCH_FAIL",symbol,str(e)[:160],flush=True); return []
        lst=obj.get("result",{}).get("list",[]) or []
        if not lst: break
        oldest=None
        for r in lst:
            ts=int(r[0]); oldest=ts if oldest is None else min(oldest,ts)
            if ts<start: continue
            rows[ts]=(ts,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[5]),float(r[6]))
        if oldest is None or oldest<=start: break
        cursor_end=oldest-1
        time.sleep(0.04)
    return [rows[k] for k in sorted(rows)]

def rsi(cl,n=14):
    if len(cl)<n+1:return None
    g=l=0.0
    for a,b in zip(cl[-n-1:-1],cl[-n:]):
        d=b-a;g+=max(d,0);l+=max(-d,0)
    g/=n;l/=n
    return 100.0 if l==0 else 100-100/(1+g/l)

def atr(bs,n=14):
    if len(bs)<n+1:return None
    z=[]
    for i in range(-n,0):
        _,o,h,l,c,v,turn=bs[i]; pc=bs[i-1][4]
        z.append(max(h-l,abs(h-pc),abs(l-pc))/pc)
    return sum(z)/n

def bb(cl,n=20,k=2):
    if len(cl)<n:return None
    z=cl[-n:];m=sum(z)/n;sd=statistics.pstdev(z)
    return m-k*sd,m+k*sd

def ema(cl,n):
    if len(cl)<n:return None
    a=2/(n+1);e=cl[-n]
    for v in cl[-n+1:]:e=a*v+(1-a)*e
    return e

BARS={}
try:
    with open(CACHE,"r") as fh:
        raw=json.load(fh)
    for sym,rows in raw.items():
        if sym in symbols:
            BARS[sym]=[tuple(x) for x in rows]
    if not BEST_ONLY: print("CACHE_LOADED",len(BARS),"symbols",flush=True)
except Exception:
    pass

missing=[s for s in symbols if s not in BARS]
if missing:
    if not BEST_ONLY: print("FETCHING",len(missing),"exact Bybit SPOT USDT symbols",flush=True)
    for i,sym in enumerate(missing,1):
        b=fetch_klines(sym)
        if len(b)>=500:
            BARS[sym]=b
            if not BEST_ONLY: print("SPOT_OK",i,"/",len(missing),sym,"bars",len(b),flush=True)
        else:
            if not BEST_ONLY: print("SPOT_SKIP",i,"/",len(missing),sym,"bars",len(b),flush=True)
    try:
        with open(CACHE,"w") as fh:
            json.dump({k:[list(x) for x in v] for k,v in BARS.items()},fh)
        if not BEST_ONLY: print("CACHE_SAVED",CACHE,flush=True)
    except Exception as e:
        print("CACHE_SAVE_FAIL",e,flush=True)

if not BEST_ONLY: print("USABLE_SPOT_SYMBOLS",len(BARS),sorted(BARS),flush=True)
if not BARS:
    raise SystemExit("No usable Bybit spot symbols")

# Precompute indicators once per bar so the matrix does not recalculate RSI/ATR/BB/EMA
# tens of thousands of times.
IND={}
for sym,bs in BARS.items():
    arr=[]
    closes=[]
    for i,row in enumerate(bs):
        closes.append(row[4])
        if i<30:
            arr.append(None); continue
        hist=bs[:i+1]
        arr.append((rsi(closes),atr(hist),bb(closes),ema(closes,8),ema(closes,21)))
    IND[sym]=arr
if not BEST_ONLY: print("INDICATORS_PRECOMPUTED",sum(len(v) for v in IND.values()),flush=True)

def simulate(sym,bs,cfg,lo_ts=None,hi_ts=None):
    rsi_th,atr_min,step_mult,tp_mult,stop_frac,max_levels,size_mode,trend_guard,max_age=cfg
    pos=None;out=[]
    for i in range(30,len(bs)):
        t,o,h,l,c,v,turn=bs[i]
        if lo_ts is not None and t<lo_ts: continue
        if hi_ts is not None and t>=hi_ts: break
        ind=IND[sym][i]
        if ind is None: continue
        rv,av,bands,e8,e21=ind
        cl=[x[4] for x in bs[max(0,i-4):i+1]]
        if pos is None:
            if rv is None or av is None or bands is None or e8 is None or e21 is None: continue
            blo,bhi=bands
            down=e8<e21 and c<e21
            steep=len(cl)>=4 and cl[-1]<cl[-4]*(1-0.008)
            if trend_guard==1 and down: continue
            if trend_guard==2 and (down or steep): continue
            if av>=atr_min and (rv<=rsi_th or c<=blo):
                pos={"entry_t":t,"entry_i":i,"qty":1.0,"spent":c,"levels":1,"atr":av}
            continue
        avg=pos["spent"]/pos["qty"];tp=avg*(1+tp_mult*max(pos["atr"],0.002))
        if h>=tp:
            ret=(tp*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append((pos["entry_t"],t,ret,"TP",pos["levels"],sym));pos=None;continue
        if i-pos["entry_i"]>=max_age:
            ret=(c*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append((pos["entry_t"],t,ret,"TIME",pos["levels"],sym));pos=None;continue
        if pos["levels"]<max_levels:
            trig=avg*(1-step_mult*max(pos["atr"],0.002))
            if l<=trig:
                add=1.0 if size_mode==0 else (1.5**pos["levels"] if size_mode==1 else 2.0**pos["levels"])
                pos["spent"]+=trig*add;pos["qty"]+=add;pos["levels"]+=1
        avg=pos["spent"]/pos["qty"]
        if l<=avg*(1-stop_frac):
            px=avg*(1-stop_frac)
            ret=(px*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append((pos["entry_t"],t,ret,"STOP",pos["levels"],sym));pos=None
    return out

def st(tr):
    tr=sorted(tr,key=lambda x:x[1]);rs=[x[2] for x in tr];n=len(rs)
    gp=sum(max(0,r) for r in rs);gl=-sum(min(0,r) for r in rs)
    eq=pk=dd=0.0
    for r in rs: eq+=r;pk=max(pk,eq);dd=max(dd,pk-eq)
    return {"N":n,"WR":100*sum(r>0 for r in rs)/n if n else 0,"NET":sum(rs),
            "EXP":statistics.mean(rs) if rs else 0,"PF":gp/gl if gl else 999.0,
            "DD":dd,"WORST":min(rs) if rs else 0,
            "STOP":sum(x[3]=="STOP" for x in tr),"TIME":sum(x[3]=="TIME" for x in tr)}

# Time boundaries from common union
allts=sorted({r[0] for b in BARS.values() for r in b})
q=[allts[int((len(allts)-1)*x)] for x in (0.0,0.5,0.7,0.85,1.0)]
# Train first 50%, validate next 20%, test next 15%, final holdout last 15%.

# Stage 1: compact coarse grid. Risk settings fixed conservatively.
base_cfgs=[]
for rsi_th in (25,30,35):
 for atr_min in (0.002,0.004,0.006,0.008):
  for step in (1.5,2.0,2.5,3.0):
   for tp in (0.8,1.0,1.2,1.5):
    for guard in (0,1,2):
     base_cfgs.append((rsi_th,atr_min,step,tp,0.08,2,0,guard,32))

coarse=[]
for i,cfg in enumerate(base_cfgs,1):
    train=[];val=[]
    for sym,br in BARS.items():
        train+=simulate(sym,br,cfg,q[0],q[1]); val+=simulate(sym,br,cfg,q[1],q[2])
    a=st(train);b=st(val)
    if a["N"]>=25 and b["N"]>=8 and a["NET"]>0 and b["NET"]>0 and a["PF"]>1.05 and b["PF"]>1.05:
        score=(min(a["PF"],b["PF"]),min(a["EXP"],b["EXP"]),-max(a["DD"],b["DD"]))
        coarse.append((score,cfg,a,b))
    if (not BEST_ONLY) and (i%100==0 or i==len(base_cfgs)):
        print("COARSE",i,"/",len(base_cfgs),"survivors",len(coarse),flush=True)

# Stage 2: refine only neighborhoods of the best 12 coarse entry configs.
seed=sorted(coarse,reverse=True)[:12]
refined=[]
seen=set()
for _,cfg0,_,_ in seed:
    rsi_th,atr_min,step,tp,_,_,_,guard,_=cfg0
    for stop in (0.06,0.08,0.10,0.12):
     for levels in (2,3):
      for size_mode in (0,1):
       for age in (16,32,48):
        cfg=(rsi_th,atr_min,step,tp,stop,levels,size_mode,guard,age)
        if cfg in seen: continue
        seen.add(cfg)
        train=[];val=[]
        for sym,br in BARS.items():
            train+=simulate(sym,br,cfg,q[0],q[1]); val+=simulate(sym,br,cfg,q[1],q[2])
        a=st(train);b=st(val)
        if a["N"]>=25 and b["N"]>=8 and a["NET"]>0 and b["NET"]>0 and a["PF"]>1.05 and b["PF"]>1.05:
            score=(min(a["PF"],b["PF"]),min(a["EXP"],b["EXP"]),-max(a["DD"],b["DD"]))
            refined.append((score,cfg,a,b))

coarse=sorted(refined,reverse=True)[:80]
if not BEST_ONLY: print("REFINED_TOP",len(coarse),flush=True)

final=[]
for score,cfg,a,b in coarse:
    test=[];hold=[]
    for s,br in BARS.items():
        test+=simulate(s,br,cfg,q[2],q[3]); hold+=simulate(s,br,cfg,q[3],q[4])
    c=st(test);d=st(hold)
    if c["N"]<8 or d["N"]<8 or c["NET"]<=0 or d["NET"]<=0 or c["PF"]<=1.05 or d["PF"]<=1.05:
        continue
    # stress +20bps on final holdout
    stressed=[(x[0],x[1],x[2]-0.002,x[3],x[4],x[5]) for x in hold]
    e=st(stressed)
    if e["NET"]<=0 or e["PF"]<=1.0: continue
    alltr=[]
    for s,br in BARS.items(): alltr+=simulate(s,br,cfg)
    whole=st(alltr)
    final.append(((min(a["PF"],b["PF"],c["PF"],d["PF"]),min(a["EXP"],b["EXP"],c["EXP"],d["EXP"]),-whole["DD"]),cfg,a,b,c,d,e,whole))

final.sort(reverse=True)
result_path="/data/spot_grid_90d_best.json"
if final:
    score,cfg,a,b,c,d,e,w=final[0]
    payload={"cfg":cfg,"train":a,"val":b,"test":c,"holdout":d,"holdout_stress_20bps":e,"all90d":w}
    try:
        with open(result_path,"w") as fh: json.dump(payload,fh,indent=2)
    except Exception:
        pass
else:
    payload=None
    try:
        with open(result_path,"w") as fh: json.dump({"best":None},fh,indent=2)
    except Exception:
        pass

if BEST_ONLY:
    print("=== BEST ===")
    if payload:
        print(json.dumps(payload,separators=(",",":")))
    else:
        print("NONE")
else:
    print("=== 90D WALK-FORWARD ROBUST SPOT CANDIDATES ===")
    for row in final[:20]:
        score,cfg,a,b,c,d,e,w=row
        print("\nCFG",cfg)
        print(" TRAIN",a);print(" VAL",b);print(" TEST",c);print(" HOLDOUT",d);print(" HOLDOUT_STRESS+20bps",e);print(" ALL90D",w)
    print("=== BEST ===")
    if final: print(final[0][1],final[0][-1])
    else: print("NONE")
    print("BEST_SAVED",result_path)
