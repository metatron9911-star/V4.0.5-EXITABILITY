#!/usr/bin/env python3
# Validation for selected 15m spot mean-reversion grid candidate.
# Research-only: no trading, no service/config mutation.
import sqlite3, statistics, math, collections

DB="/data/bbb.sqlite3"
cx=sqlite3.connect(DB); cx.row_factory=sqlite3.Row

TF=900000
RSI_TH=35
ATR_MIN=0.006
STEP_MULT=2.0
TP_MULT=1.0
ROUNDTRIP_FEE=0.002
STOP_FRAC=0.12
MAX_LEVELS=3

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

def simulate(symbol,bs,start_i=25,end_i=None):
    end_i=len(bs) if end_i is None else min(end_i,len(bs))
    pos=None; out=[]
    for i in range(max(25,start_i),end_i):
        t,o,h,l,c=bs[i]; hist=bs[:i+1]; cl=[x[4] for x in hist]
        rv=rsi(cl); av=atr(hist); bands=bb(cl)
        if pos is None:
            if rv is None or av is None or bands is None: continue
            lo,hi=bands
            if av>=ATR_MIN and (rv<=RSI_TH or c<=lo):
                pos={"entry_t":t,"avg":c,"qty":1.0,"spent":c,"levels":1,"base_atr":av,"entry_px":c}
            continue
        avg=pos["spent"]/pos["qty"]
        tp=avg*(1+TP_MULT*max(pos["base_atr"],0.002))
        if h>=tp:
            ret=(tp*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":t,"ret":ret,"reason":"TP","levels":pos["levels"]})
            pos=None; continue
        if pos["levels"]<MAX_LEVELS:
            step=STEP_MULT*max(pos["base_atr"],0.002)
            trig=avg*(1-step)
            if l<=trig:
                add=2**pos["levels"]; pos["spent"]+=trig*add; pos["qty"]+=add; pos["levels"]+=1
                avg=pos["spent"]/pos["qty"]
        avg=pos["spent"]/pos["qty"]
        if l<=avg*(1-STOP_FRAC):
            px=avg*(1-STOP_FRAC)
            ret=(px*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
            out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":t,"ret":ret,"reason":"STOP","levels":pos["levels"]})
            pos=None
    # Mark any still-open position to last close with an exit fee; do not silently drop it.
    if pos is not None and end_i>0:
        px=bs[end_i-1][4]
        ret=(px*pos["qty"]-pos["spent"])/pos["spent"]-ROUNDTRIP_FEE
        out.append({"symbol":symbol,"entry_t":pos["entry_t"],"exit_t":bs[end_i-1][0],"ret":ret,"reason":"MARK","levels":pos["levels"]})
    return out

def stats(xs):
    rs=[x["ret"] for x in xs]; n=len(rs)
    gp=sum(max(0,r) for r in rs); gl=-sum(min(0,r) for r in rs)
    eq=pk=dd=0.0
    for r in rs:
        eq+=r; pk=max(pk,eq); dd=max(dd,pk-eq)
    return {"N":n,"WR":100*sum(r>0 for r in rs)/n if n else 0,"NET":sum(rs),
            "EXP":statistics.mean(rs) if rs else 0,"MED":statistics.median(rs) if rs else 0,
            "PF":gp/gl if gl else 999.0,"DD":dd,"WORST":min(rs) if rs else 0,
            "STOP":sum(x["reason"]=="STOP" for x in xs),"MARK":sum(x["reason"]=="MARK" for x in xs)}

symbols=[r[0] for r in cx.execute("SELECT DISTINCT symbol FROM feature_samples ORDER BY symbol")]
bars={s:build_bars(s) for s in symbols}
bars={s:b for s,b in bars.items() if len(b)>=40}
print("CONFIG",dict(tf_min=15,rsi=RSI_TH,atr_min=ATR_MIN,step_mult=STEP_MULT,tp_mult=TP_MULT,fee_rt=ROUNDTRIP_FEE,stop=STOP_FRAC))
print("SYMBOLS",len(bars))

alltr=[]
for s,b in bars.items(): alltr += simulate(s,b)
alltr.sort(key=lambda x:x["exit_t"])
print("ALL",stats(alltr))

print("\n=== BY SYMBOL ===")
for s in sorted(bars):
    z=[x for x in alltr if x["symbol"]==s]
    if z: print(s,stats(z))

print("\n=== 4 TIME QUARTILES ===")
# Use global timestamp quartiles, then replay each symbol only inside the window.
allts=sorted({bar[0] for b in bars.values() for bar in b})
for qi in range(4):
    lo=allts[int(len(allts)*qi/4)]
    hi=allts[min(len(allts)-1,int(len(allts)*(qi+1)/4)-1)]
    z=[x for x in alltr if lo<=x["entry_t"]<=hi]
    print("Q"+str(qi+1),lo,hi,stats(z))

print("\n=== FIRST HALF / SECOND HALF ===")
mid=allts[len(allts)//2]
print("H1",stats([x for x in alltr if x["entry_t"]<mid]))
print("H2",stats([x for x in alltr if x["entry_t"]>=mid]))

print("\n=== SYMBOL HOLDOUTS ===")
# Deterministic 4-fold symbol holdout: every 4th symbol.
sy=sorted(bars)
for k in range(4):
    hold=set(sy[k::4])
    z=[x for x in alltr if x["symbol"] in hold]
    print("HOLDOUT"+str(k+1),"symbols",len(hold),stats(z))

print("\n=== STRESS COSTS ===")
for extra in (0.0,0.001,0.002,0.004):
    z=[]
    for x in alltr:
        y=dict(x); y["ret"]=x["ret"]-extra; z.append(y)
    print("extra_cost",extra,stats(z))

print("\n=== DECISION ===")
checks=[]
for label,z in [
    ("H1",[x for x in alltr if x["entry_t"]<mid]),
    ("H2",[x for x in alltr if x["entry_t"]>=mid]),
]:
    q=stats(z); checks.append(q["N"]>=8 and q["NET"]>0 and q["PF"]>1.2)
for qi in range(4):
    lo=allts[int(len(allts)*qi/4)]
    hi=allts[min(len(allts)-1,int(len(allts)*(qi+1)/4)-1)]
    q=stats([x for x in alltr if lo<=x["entry_t"]<=hi])
    checks.append(q["N"]>=3 and q["NET"]>0)
print("ROBUST_PASS",all(checks),"checks",checks)
