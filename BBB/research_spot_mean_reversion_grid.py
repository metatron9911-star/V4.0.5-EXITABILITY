#!/usr/bin/env python3
# Research-only 5m/15m mean-reversion + dynamic DCA grid backtest from feature_samples.
# Does NOT trade or modify the running strategy.
import sqlite3, statistics, math, collections

DB="/data/bbb.sqlite3"
cx=sqlite3.connect(DB); cx.row_factory=sqlite3.Row

def bars(symbol, tf_ms):
    rs=cx.execute("""SELECT ts_ms,mid FROM feature_samples
                     WHERE symbol=? AND mid IS NOT NULL
                     ORDER BY ts_ms""",(symbol,)).fetchall()
    out=[]; cur=None; arr=[]
    for r in rs:
        k=(int(r["ts_ms"])//tf_ms)*tf_ms
        if cur is None: cur=k
        if k!=cur:
            if arr:
                vals=[float(x["mid"]) for x in arr]
                out.append((cur,vals[0],max(vals),min(vals),vals[-1]))
            cur=k; arr=[]
        arr.append(r)
    if arr:
        vals=[float(x["mid"]) for x in arr]
        out.append((cur,vals[0],max(vals),min(vals),vals[-1]))
    return out

def rsi(closes,n=14):
    if len(closes)<n+1:return None
    gains=[]; losses=[]
    for a,b in zip(closes[-n-1:-1],closes[-n:]):
        d=b-a; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    if al==0:return 100.0
    rs=ag/al
    return 100-100/(1+rs)

def atr(bs,n=14):
    if len(bs)<n+1:return None
    trs=[]
    for i in range(-n,0):
        _,o,h,l,c=bs[i]
        pc=bs[i-1][4]
        trs.append(max(h-l,abs(h-pc),abs(l-pc))/pc)
    return sum(trs)/n

def bb(closes,n=20,k=2):
    if len(closes)<n:return None
    z=closes[-n:]; m=sum(z)/n; sd=statistics.pstdev(z)
    return m-k*sd,m+k*sd

symbols=[r[0] for r in cx.execute("SELECT DISTINCT symbol FROM feature_samples ORDER BY symbol")]

# Precompute bars ONCE. The original version rebuilt bars inside every config,
# causing thousands of full DB scans and making the research job look hung.
BAR_CACHE={}
for sym in symbols:
    for tf in (300000,900000):
        BAR_CACHE[(sym,tf)]=bars(sym,tf)
print("PRECOMPUTED",len(BAR_CACHE),"symbol/timeframe series")

configs=[]
for tf in (300000,900000):
  for rsi_th in (25,30,35):
    for atr_min in (0.002,0.004,0.006):
      for step_mult in (1.0,1.5,2.0):
        for tp_mult in (0.6,0.8,1.0):
          configs.append((tf,rsi_th,atr_min,step_mult,tp_mult))

def run(cfg):
    tf,rsi_th,atr_min,step_mult,tp_mult=cfg
    pnl=[]; trades=0; wins=0
    for sym in symbols:
        bs=BAR_CACHE.get((sym,tf),[])
        if len(bs)<40: continue
        pos=None
        for i in range(25,len(bs)):
            t,o,h,l,c=bs[i]
            hist=bs[:i+1]
            closes=[x[4] for x in hist]
            rv=rsi(closes); av=atr(hist); bbv=bb(closes)
            if pos is None:
                if rv is None or av is None or bbv is None: continue
                lo,hi=bbv
                if av>=atr_min and (rv<=rsi_th or c<=lo):
                    pos={"avg":c,"qty":1.0,"spent":c,"levels":1,"base_atr":av}
                continue
            avg=pos["spent"]/pos["qty"]
            tp=avg*(1+tp_mult*max(pos["base_atr"],0.002))
            if h>=tp:
                ret=(tp*pos["qty"]-pos["spent"])/pos["spent"] - 0.002 # 0.1% in + 0.1% out
                pnl.append(ret); trades+=1; wins+=ret>0; pos=None; continue
            # Safety orders: max 3 levels, size 1,2,4; step scaled by ATR.
            if pos["levels"]<3:
                step=step_mult*max(pos["base_atr"],0.002)
                trigger=avg*(1-step)
                if l<=trigger:
                    addqty=2**pos["levels"]; pos["spent"]+=trigger*addqty; pos["qty"]+=addqty; pos["levels"]+=1
                    avg=pos["spent"]/pos["qty"]
            # hard basket stop at 12% from avg
            avg=pos["spent"]/pos["qty"]
            if l<=avg*0.88:
                px=avg*0.88
                ret=(px*pos["qty"]-pos["spent"])/pos["spent"] - 0.002
                pnl.append(ret); trades+=1; pos=None
    if not pnl:return None
    gp=sum(max(0,x) for x in pnl); gl=-sum(min(0,x) for x in pnl)
    return dict(N=trades,WR=100*wins/trades if trades else 0,NET=sum(pnl),EXP=statistics.mean(pnl),PF=gp/gl if gl else 999,MED=statistics.median(pnl),WORST=min(pnl))

out=[]
for i,cfg in enumerate(configs,1):
    s=run(cfg)
    if s and s["N"]>=20:
        out.append((s["EXP"],s["PF"],s["WR"],cfg,s))
    if i%25==0 or i==len(configs):
        print("PROGRESS",i,"/",len(configs),"candidates",len(out),flush=True)
out.sort(reverse=True)

print("=== TOP 5m/15m MEAN-REVERSION GRID CONFIGS ===")
for _,_,_,cfg,s in out[:25]:
    tf,rsi_th,atr_min,step_mult,tp_mult=cfg
    print("TF",tf//60000,"m RSI<=",rsi_th,"ATR>=",atr_min,"STEPxATR",step_mult,"TPxATR",tp_mult,
          "N",s["N"],"WR",round(s["WR"],1),"NET",round(s["NET"],4),"EXP",round(s["EXP"],5),
          "PF",round(s["PF"],2),"MED",round(s["MED"],5),"WORST",round(s["WORST"],4))
print("=== BEST ===")
print(out[0][3],out[0][4] if out else "NONE")
