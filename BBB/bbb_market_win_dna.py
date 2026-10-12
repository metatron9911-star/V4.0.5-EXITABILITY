"""BBB / Bybit market-wide WIN-DNA, efficient linear pass.

Uses retained feature_samples only. No legacy ROCKET selection filters.
Every sampled snapshot is a hypothetical LONG decision point:
entry = current ask, future outcome = observed bid first-touch.

Research only; no orders, wallets or LIVE execution.
"""
from __future__ import annotations
import json, math, os, sqlite3
from pathlib import Path
from collections import Counter

HORIZON_MS=20_000
SAMPLE_STEP_MS=2_000
MAX_RAW_ROWS=300_000
SCHEMES={
 "FAST":{"tp_bps":55.0,"sl_bps":-25.0},
 "QUALITY":{"tp_bps":80.0,"sl_bps":-30.0},
 "STRONG":{"tp_bps":100.0,"sl_bps":-35.0},
}
QUANTS=(.15,.25,.35,.50,.65,.75,.85)

def num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception:return None

def qtile(a,q):
    a=sorted(a)
    if not a:return None
    z=(len(a)-1)*q; lo=int(z); hi=min(len(a)-1,lo+1); t=z-lo
    return a[lo]*(1-t)+a[hi]*t

def metric(rows,pred,key):
    tp=fp=tn=fn=0
    for r in rows:
        y=r[key]
        if y not in ("WIN","LOSS"):continue
        p=bool(pred(r))
        if p and y=="WIN":tp+=1
        elif p and y=="LOSS":fp+=1
        elif not p and y=="LOSS":tn+=1
        else:fn+=1
    n=tp+fp+tn+fn; base=(tp+fn)/n if n else 0
    prec=tp/(tp+fp) if tp+fp else 0; rec=tp/(tp+fn) if tp+fn else 0
    return {"n":n,"tp":tp,"fp":fp,"tn":tn,"fn":fn,"support":tp+fp,
            "precision":round(prec,6),"recall":round(rec,6),
            "base":round(base,6),"lift":round(prec/base,6) if base else 0}

def parse_row(z):
    try:p=json.loads(z[15] or "{}")
    except Exception:p={}
    db=num(z[5]); da=num(z[6])
    return {
      "ts":int(z[0]),"mid":num(z[1]),"bid":num(z[2]),"ask":num(z[3]),
      "spread_bps":num(z[4]),
      "depth_min_usd":min(db,da) if db is not None and da is not None else None,
      "depth_total_usd":db+da if db is not None and da is not None else None,
      "imbalance":num(z[7]),"t1":num(z[8]),"t5":num(z[9]),"buy_ratio":num(z[10]),
      "ret250":num(z[11]),"ret1":num(z[12]),"ret5":num(z[13]),"rv5":abs(num(z[14]) or 0.0),
      "range24h_bps":num(p.get("range24h_bps")),
      "turnover24h":num(p.get("turnover24h")),
      "activity":num(p.get("live_activity_score")),
    }

DIRECT=("spread_bps","depth_min_usd","depth_total_usd","imbalance","t1","t5",
        "buy_ratio","ret250","ret1","ret5","rv5","range24h_bps","turnover24h","activity")

def load_symbol(cx,symbol,min_id):
    q="""SELECT ts_ms,mid,bid,ask,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                book_imbalance_5,trade_count_1s,trade_count_5s,buy_ratio_1s,
                ret_250ms_bps,ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,payload_json
         FROM feature_samples WHERE symbol=? AND id>=? ORDER BY ts_ms"""
    return [parse_row(z) for z in cx.execute(q,(symbol,min_id))]

def labels(rows,i):
    ask=rows[i]["ask"]
    if not ask or ask<=0:return None
    out={"label_"+k:"NEUTRAL" for k in SCHEMES}
    active=set(SCHEMES)
    end=rows[i]["ts"]+HORIZON_MS
    j=i+1
    while j<len(rows) and rows[j]["ts"]<=end and active:
        bid=rows[j]["bid"]; j+=1
        if not bid or bid<=0:continue
        bps=(bid/ask-1)*10000.0
        for name in list(active):
            c=SCHEMES[name]
            if bps<=c["sl_bps"]:
                out["label_"+name]="LOSS";active.remove(name)
            elif bps>=c["tp_bps"]:
                out["label_"+name]="WIN";active.remove(name)
    return out

def feature_point(cur,prev2,prev5,prev10):
    r={k:cur.get(k) for k in DIRECT}
    for tag,prev in (("d2",prev2),("d5",prev5),("d10",prev10)):
        if prev:
            for k in ("spread_bps","depth_min_usd","imbalance","t1","t5","buy_ratio",
                      "ret250","ret1","ret5","rv5","activity"):
                a=num(cur.get(k)); b=num(prev.get(k))
                if a is not None and b is not None:r[f"{tag}_{k}"]=a-b
    t1=num(cur.get("t1")); t5=num(cur.get("t5"))
    if t1 is not None and t5 not in (None,0):r["tape_accel_1v5"]=t1/(t5/5.0+1e-9)
    d=num(cur.get("depth_min_usd")); sp=num(cur.get("spread_bps"))
    if d is not None and sp is not None:r["depth_per_spread"]=d/(sp+0.1)
    rr=num(cur.get("ret1")); rv=num(cur.get("rv5"))
    if rr is not None and rv is not None:r["ret1_over_rv"]=rr/(rv+0.1)
    br=num(cur.get("buy_ratio"))
    if br is not None:r["buy_pressure"]=2*(br-.5)
    return r

def nearest_before(picks,ts):
    # picks is chronological list of raw rows already selected
    for x in reversed(picks):
        if x["ts"]<=ts:return x
    return None

def build(cx):
    max_id=int(cx.execute("SELECT COALESCE(MAX(id),0) FROM feature_samples").fetchone()[0] or 0)
    min_id=max(0,max_id-MAX_RAW_ROWS+1)
    symbols=[x[0] for x in cx.execute("SELECT DISTINCT symbol FROM feature_samples WHERE id>=? ORDER BY symbol",(min_id,))]
    dataset=[]; scanned=0
    for symbol in symbols:
        rows=load_symbol(cx,symbol,min_id); scanned+=len(rows)
        picks=[]; last=-10**18
        for i,cur in enumerate(rows):
            if cur["ts"]-last<SAMPLE_STEP_MS:continue
            lab=labels(rows,i)
            if not lab:continue
            if all(v=="NEUTRAL" for v in lab.values()):
                last=cur["ts"]; picks.append(cur); continue
            p2=nearest_before(picks,cur["ts"]-2000)
            p5=nearest_before(picks,cur["ts"]-5000)
            p10=nearest_before(picks,cur["ts"]-10000)
            r=feature_point(cur,p2,p5,p10);r.update(lab);r["ts"]=cur["ts"];r["symbol"]=symbol
            dataset.append(r);last=cur["ts"];picks.append(cur)
    dataset.sort(key=lambda x:x["ts"])
    return dataset,scanned,len(symbols)

def features(rows):
    skip={"ts","symbol"}|{"label_"+x for x in SCHEMES}
    s=set()
    for r in rows[:100000]:
        for k,v in r.items():
            if k not in skip and num(v) is not None:s.add(k)
    return sorted(s)

def discover(train,fs):
    rules=[]; min_support=max(80,min(1000,len(train)//100))
    for f in fs:
        vals=[num(r.get(f)) for r in train];vals=[v for v in vals if v is not None]
        if len(vals)<500:continue
        for q in QUANTS:
            th=qtile(vals,q)
            for op in (">=","<="):
                def pred(r,f=f,th=th,op=op):
                    v=num(r.get(f))
                    return False if v is None else (v>=th if op==">=" else v<=th)
                m=metric(train,pred,"label_QUALITY")
                if m["support"]<min_support or m["tp"]<30 or m["recall"]<.03:continue
                score=(m["lift"]-1)*math.sqrt(m["support"])*(.4+.6*m["recall"])
                rules.append({"feature":f,"op":op,"threshold":th,"train":m,"score":score})
    rules.sort(key=lambda x:(x["score"],x["train"]["lift"],x["train"]["support"]),reverse=True)
    return rules

def prule(x):
    def p(r):
        v=num(r.get(x["feature"]))
        return False if v is None else (v>=x["threshold"] if x["op"]==">=" else v<=x["threshold"])
    return p

def evaluate(train,test,fs):
    out=[]
    for x in discover(train,fs)[:200]:
        y=dict(x)
        for scheme in SCHEMES:
            y["train_"+scheme]=metric(train,prule(x),"label_"+scheme)
            y["test_"+scheme]=metric(test,prule(x),"label_"+scheme)
        tq=y["test_QUALITY"]
        y["stable"]=bool(tq["support"]>=80 and tq["tp"]>=20 and tq["lift"]>=1.10
                         and y["test_FAST"]["lift"]>=1.02 and y["test_STRONG"]["lift"]>=1.02)
        out.append(y)
    out.sort(key=lambda x:(x["stable"],x["test_QUALITY"]["lift"],x["test_QUALITY"]["support"]),reverse=True)
    return out

def pairs(train,test,rules):
    pool=[x for x in rules if x["stable"]][:12];out=[]
    for i,a in enumerate(pool):
        for b in pool[i+1:]:
            if a["feature"]==b["feature"]:continue
            pa,pb=prule(a),prule(b); pred=lambda r,pa=pa,pb=pb:pa(r) and pb(r)
            z={"a":{k:a[k] for k in ("feature","op","threshold")},
               "b":{k:b[k] for k in ("feature","op","threshold")}}
            for s in SCHEMES:
                z["train_"+s]=metric(train,pred,"label_"+s);z["test_"+s]=metric(test,pred,"label_"+s)
            tq=z["test_QUALITY"]
            z["stable"]=bool(tq["support"]>=60 and tq["tp"]>=15 and tq["lift"]>=1.15
                             and z["test_FAST"]["lift"]>=1.03 and z["test_STRONG"]["lift"]>=1.03)
            out.append(z)
    out.sort(key=lambda x:(x["stable"],x["test_QUALITY"]["lift"],x["test_QUALITY"]["support"]),reverse=True)
    return out

def vote(train,test,rules):
    pool=[x for x in rules if x["stable"]][:8]
    if len(pool)<3:return None
    best=None
    for n in range(3,min(7,len(pool))+1):
        rs=pool[:n];ps=[prule(x) for x in rs]
        for k in range(2,n+1):
            pred=lambda r,ps=ps,k=k:sum(1 for p in ps if p(r))>=k
            z={"rules":[{q:x[q] for q in ("feature","op","threshold")} for x in rs],"required_votes":k}
            for s in SCHEMES:
                z["train_"+s]=metric(train,pred,"label_"+s);z["test_"+s]=metric(test,pred,"label_"+s)
            tq=z["test_QUALITY"]
            z["stable"]=bool(tq["support"]>=80 and tq["tp"]>=20 and tq["lift"]>=1.18
                             and z["test_FAST"]["lift"]>=1.04 and z["test_STRONG"]["lift"]>=1.04)
            sc=(z["stable"],tq["lift"],z["test_STRONG"]["lift"],tq["support"])
            if best is None or sc>best["_score"]:z["_score"]=sc;best=z
    if best:best.pop("_score",None)
    return best

def run_analysis(db_path):
    cx=sqlite3.connect(str(db_path))
    ds,scanned,nsym=build(cx);cx.close()
    cut=int(len(ds)*.70);train,test=ds[:cut],ds[cut:]
    fs=features(ds);rules=evaluate(train,test,fs);ps=pairs(train,test,rules);vp=vote(train,test,rules)
    counts={}
    for part,name in ((train,"train"),(test,"holdout")):
        counts[name]={s:dict(Counter(r["label_"+s] for r in part)) for s in SCHEMES}
    approved=bool(vp and vp["stable"])
    report={"status":"BBB_BYBIT_MARKET_WIDE_WIN_DNA_V2","source":"BBB retained Bybit feature_samples; zero legacy ROCKET gates",
      "raw_rows":scanned,"raw_row_cap":MAX_RAW_ROWS,"symbols":nsym,"decision_rows":len(ds),"feature_count":len(fs),
      "split":"chronological_70_30","labels":counts,"schemes":SCHEMES,
      "stable_rules":[x for x in rules if x["stable"]][:30],
      "stable_pairs":[x for x in ps if x["stable"]][:20],"vote_portrait":vp,
      "approved":approved,"decision":"APPROVE_NEW_MARKET_DNA_PAPER_SELECTOR" if approved else "DO_NOT_APPROVE_YET"}
    Path(db_path).parent.joinpath("bbb_market_win_dna_report.json").write_text(json.dumps(report,indent=2),encoding="utf8")
    return report

if __name__=="__main__":
    db=Path(os.getenv("DATA_DIR",Path(__file__).resolve().parent/"data"))/"bbb.sqlite3"
    r=run_analysis(db);print(json.dumps(r,separators=(",",":")))
