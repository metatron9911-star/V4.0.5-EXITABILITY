#!/usr/bin/env python3
# Robustness repair matrix for 15m spot mean-reversion grid.
# Research-only. No orders, no config changes.
import sqlite3, statistics, math, itertools, collections

DB="/data/bbb.sqlite3"
cx=sqlite3.connect(DB); cx.row_factory=sqlite3.Row
TF=900000; RSI_TH=35; ATR_MIN=0.006; ROUNDTRIP_FEE=0.002

def build_bars(symbol):
    rs=cx.execute("""SELECT ts_ms,mid FROM feature_samples
                     WHERE symbol=? AND mid IS NOT NULL ORDER BY ts_ms""",(symbol,)).fetchall()
    out=[]; cur=None; vals=[]
    for r in rs:
        k=(int(r["ts_ms"])//TF)*TF
        if cur is None: cur=k
        if k!=cur:
            if vals: out.append((cur,vals[0],max(vals),min(vals),vals[-1]))
            cur=k; vals=[]
        vals.append(float(r["mid"]))
    if vals: out.append((cur,vals[0],max(vals),min(vals),vals[-1]))
    return out

def rsi(cl,n=14):
    if len(cl)<n+1:return None
    g=l=0.0
    for a,b in zip(cl[-n-1:-1],cl[-n:]):
        d=b-a; g+=max(d,0); l+=max(-d,0)
    g/=n; l/=n
    if l==0:return 100.0
    return 100-100/(1+g/l)

def atr(bs,n=14):
    if len(bs)<n+1:return None
    ts=[]
    for i in range(-n,0):
        _,o,h,l,c=bs[i]; pc=bs[i-1][4]
        ts.append(max(h-l,abs(h-pc),abs(l-pc))/pc)
    return sum(ts)/n

def bb(cl,n=20,k=2):
    if len(cl)<n:return None
    z=cl[-n:]; m=sum(z)/n; sd=statistics.pstdev(z)
    return m-k*sd,m+k*sd

def ema(vals,n):
    if len(vals)<n:return None
    a=2/(n+1); e=vals[-n]
    for v in vals[-n+1:]: e=a*v+(1-a)*e
    return e

def simulate(symbol,bs,cfg):
    step_mult,tp_mult,stop_frac,max_levels,size_mode,trend_guard,max_age_bars=cfg
    pos=None; out=[]
    for i in range(30,len(bs)):
        t,o,h,l,c=bs[i]; hist=bs[:i+1]; cl=[x[4] for x in hist]
        rv=rsi(cl); av=atr(hist); bands=bb(cl)
        e8=ema(cl,8); e21=ema(cl,21)
        if pos is None:
            if rv is None or av is None or bands is None or e8 is None or e21 is None: continue
            lo,hi=bands
            downtrend=(e8<e21 and c<e21)
            steep=(len(cl)>=4 and cl[-1] < cl[-4]*(1-0.008))
            if trend_guard==1 and downtrend: continue
            if trend_guard==2 and (downtrend or steep): continue
            if av>=ATR_MIN and (rv<=RSI_TH or c<=lo):
                pos={"entry_t":t,"entry_i":i,"qty":1.0,"spent":c,"levels":1,"base_atr":av}
            continue
        avg=pos["spent"]/pos["qty"]
        tp=avg*(1+tp_mult*max(pos["base_atr"],0.002))
        if h>=tp:
            ret=(tp*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":t,"ret":ret,"reason":"TP","levels":pos["levels"]})
            pos=None; continue
        # time stop / stale basket exit at close
        if i-pos["entry_i"]>=max_age_bars:
            ret=(c*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":t,"ret":ret,"reason":"TIME","levels":pos["levels"]})
            pos=None; continue
        if pos["levels"]<max_levels:
            step=step_mult*max(pos["base_atr"],0.002)
            trig=avg*(1-step)
            if l<=trig:
                if size_mode==0: add=1.0
                elif size_mode==1: add=1.5**pos["levels"]
                else: add=2.0**pos["levels"]
                pos["spent"]+=trig*add; pos["qty"]+=add; pos["levels"]+=1
                avg=pos["spent"]/pos["qty"]
        avg=pos["spent"]/pos["qty"]
        if l<=avg*(1-stop_frac):
            px=avg*(1-stop_frac)
            ret=(px*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":t,"ret":ret,"reason":"STOP","levels":pos["levels"]})
            pos=None
    if pos is not None:
        px=bs[-1][4]
        ret=(px*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
        out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":bs[-1][0],"ret":ret,"reason":"MARK","levels":pos["levels"]})
    return out

def stats(xs):
    rs=[x["ret"] for x in xs]; n=len(rs)
    gp=sum(max(0,r) for r in rs); gl=-sum(min(0,r) for r in rs)
    eq=pk=dd=0.0
    for r in rs:
        eq+=r; pk=max(pk,eq); dd=max(dd,pk-eq)
    return {"N":n,"WR":100*sum(r>0 for r in rs)/n if n else 0,"NET":sum(rs),
            "EXP":statistics.mean(rs) if rs else 0,"PF":gp/gl if gl else 999.0,
            "DD":dd,"WORST":min(rs) if rs else 0,
            "STOP":sum(x["reason"]=="STOP" for x in xs),
            "TIME":sum(x["reason"]=="TIME" for x in xs),
            "MARK":sum(x["reason"]=="MARK" for x in xs)}

symbols=[r[0] for r in cx.execute("SELECT DISTINCT symbol FROM feature_samples ORDER BY symbol")]
bars={s:build_bars(s) for s in symbols}; bars={s:b for s,b in bars.items() if len(b)>=40}
allts=sorted({x[0] for b in bars.values() for x in b}); mid=allts[len(allts)//2]

cfgs=[]
for step in (1.5,2.0,2.5):
 for tp in (0.8,1.0,1.2):
  for stop in (0.06,0.08,0.10,0.12):
   for levels in (2,3):
    for size_mode in (0,1,2):
     for trend_guard in (0,1,2):
      for max_age in (16,32,48): # 4h/8h/12h on 15m
       cfgs.append((step,tp,stop,levels,size_mode,trend_guard,max_age))

out=[]
for idx,cfg in enumerate(cfgs,1):
    tr=[]
    for s,b in bars.items(): tr += simulate(s,b,cfg)
    tr.sort(key=lambda x:x["exit_t"])
    st=stats(tr)
    if st["N"]<20 or st["NET"]<=0: continue
    h1=stats([x for x in tr if x["entry_t"]<mid]); h2=stats([x for x in tr if x["entry_t"]>=mid])
    qok=True; qstats=[]
    for qi in range(4):
        lo=allts[int(len(allts)*qi/4)]
        hi=allts[min(len(allts)-1,int(len(allts)*(qi+1)/4)-1)]
        q=stats([x for x in tr if lo<=x["entry_t"]<=hi]); qstats.append(q)
        if q["N"]>=3 and q["NET"]<=0:qok=False
    # deterministic symbol holdouts
    hold=[]; holdok=True; sy=sorted(bars)
    for k in range(4):
        hs=set(sy[k::4]); q=stats([x for x in tr if x["symbol"] in hs]); hold.append(q)
        if q["N"]>=4 and q["NET"]<=0:holdok=False
    stress=stats([{**x,"ret":x["ret"]-0.002} for x in tr])
    robust=(h1["N"]>=5 and h2["N"]>=5 and h1["NET"]>0 and h2["NET"]>0 and qok and holdok and stress["NET"]>0 and stress["PF"]>1.1)
    if robust:
        score=(st["PF"],st["EXP"],-st["DD"],st["NET"])
        out.append((score,cfg,st,h1,h2,qstats,hold,stress))
    if idx%500==0: print("PROGRESS",idx,"/",len(cfgs),"robust",len(out),flush=True)

out.sort(reverse=True)
print("=== ROBUST GRID V2 CANDIDATES ===")
for row in out[:25]:
    score,cfg,st,h1,h2,qs,hs,stress=row
    print("\nCFG",cfg,"ALL",st,"H1",h1,"H2",h2,"STRESS+20bps",stress)
    print(" Q",qs)
    print(" HOLD",hs)
print("=== BEST ===")
if out: print(out[0][1],out[0][2])
else: print("NONE")
