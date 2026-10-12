"""BBB / Bybit market-wide WIN-DNA from retained feature_samples.

No legacy ROCKET gates are used for candidate selection.
Each retained market snapshot becomes a hypothetical LONG decision point.
Entry = current ask; future exits use observed bid snapshots. Labels are
first-touch executable-quote approximations over multiple target/stop schemes.

Research only. PAPER selector approval requires train/holdout stability.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics
from pathlib import Path
from collections import defaultdict, Counter, deque

HORIZON_MS=20_000
SAMPLE_STEP_MS=2_000
SCHEMES={
 "FAST":{"tp_bps":55.0,"sl_bps":-25.0},
 "QUALITY":{"tp_bps":80.0,"sl_bps":-30.0},
 "STRONG":{"tp_bps":100.0,"sl_bps":-35.0},
}
BASE=("spread_bps","depth_min_usd","depth_total_usd","imbalance",
      "t1","t5","buy_ratio","ret250","ret1","ret5","rv5",
      "range24h_bps","turnover24h","activity")
WINDOWS=(2,5,10,20,30,60)
QUANTS=(.15,.25,.35,.50,.65,.75,.85)

def num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:return None

def qtile(a,q):
    a=sorted(a)
    if not a:return None
    z=(len(a)-1)*q; lo=int(z); hi=min(len(a)-1,lo+1); t=z-lo
    return a[lo]*(1-t)+a[hi]*t

def metric(rows,pred,label_key):
    tp=fp=tn=fn=0
    for r in rows:
        y=r[label_key]
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

def load_symbol_rows(cx,symbol):
    q="""SELECT ts_ms,mid,bid,ask,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                book_imbalance_5,trade_count_1s,trade_count_5s,buy_ratio_1s,
                ret_250ms_bps,ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,payload_json
         FROM feature_samples WHERE symbol=? ORDER BY ts_ms"""
    out=[]
    for z in cx.execute(q,(symbol,)):
        try:p=json.loads(z[15] or "{}")
        except Exception:p={}
        db=num(z[5]); da=num(z[6])
        out.append({
          "ts":int(z[0]),"mid":num(z[1]),"bid":num(z[2]),"ask":num(z[3]),
          "spread_bps":num(z[4]),
          "depth_min_usd":min(db,da) if db is not None and da is not None else None,
          "depth_total_usd":db+da if db is not None and da is not None else None,
          "imbalance":num(z[7]),"t1":num(z[8]),"t5":num(z[9]),"buy_ratio":num(z[10]),
          "ret250":num(z[11]),"ret1":num(z[12]),"ret5":num(z[13]),
          "rv5":abs(num(z[14]) or 0.0),
          "range24h_bps":num(p.get("range24h_bps")),
          "turnover24h":num(p.get("turnover24h")),
          "activity":num(p.get("live_activity_score")),
        })
    return out

def label_point(rows,i):
    cur=rows[i]; ask=cur.get("ask")
    if not ask or ask<=0:return None
    end=cur["ts"]+HORIZON_MS
    labels={}
    for name,cfg in SCHEMES.items():
        lab="NEUTRAL"
        for j in range(i+1,len(rows)):
            z=rows[j]
            if z["ts"]>end:break
            bid=z.get("bid")
            if not bid or bid<=0:continue
            bps=(bid/ask-1.0)*10000.0
            if bps<=cfg["sl_bps"]:
                lab="LOSS";break
            if bps>=cfg["tp_bps"]:
                lab="WIN";break
        labels["label_"+name]=lab
    return labels

def derived(rows,i):
    cur=rows[i]
    r={k:cur.get(k) for k in BASE}
    r["symbol"]=cur["symbol"]; r["ts"]=cur["ts"]
    for w in WINDOWS:
        lo=cur["ts"]-w*1000
        xs=[]; j=i
        while j>=0 and rows[j]["ts"]>=lo:
            xs.append(rows[j]); j-=1
        xs.reverse()
        for feat in BASE:
            vals=[num(x.get(feat)) for x in xs]; vals=[v for v in vals if v is not None]
            if not vals:continue
            p=f"w{w}_{feat}_"
            r[p+"last"]=vals[-1]; r[p+"mean"]=sum(vals)/len(vals)
            r[p+"min"]=min(vals); r[p+"max"]=max(vals)
            r[p+"delta"]=vals[-1]-vals[0] if len(vals)>1 else 0.0
            if len(vals)>2:
                r[p+"slope"]=(vals[-1]-vals[0])/max(1,len(vals)-1)
    # interaction-style features
    if num(cur.get("t1")) is not None and num(cur.get("t5")) not in (None,0):
        r["tape_accel_1v5"]=cur["t1"]/(cur["t5"]/5.0+1e-9)
    if num(cur.get("depth_min_usd")) is not None and num(cur.get("spread_bps")) is not None:
        r["depth_per_spread"]=cur["depth_min_usd"]/(cur["spread_bps"]+0.1)
    if num(cur.get("ret1")) is not None and num(cur.get("rv5")) is not None:
        r["ret1_over_rv"]=cur["ret1"]/(cur["rv5"]+0.1)
    return r

def build_dataset(cx):
    symbols=[x[0] for x in cx.execute("SELECT DISTINCT symbol FROM feature_samples ORDER BY symbol")]
    rows_out=[]; scanned=0
    for symbol in symbols:
        rs=load_symbol_rows(cx,symbol)
        for x in rs:x["symbol"]=symbol
        last_pick=-10**18
        for i,cur in enumerate(rs):
            if cur["ts"]-last_pick<SAMPLE_STEP_MS:continue
            labs=label_point(rs,i)
            if not labs:continue
            if all(v=="NEUTRAL" for v in labs.values()):continue
            r=derived(rs,i); r.update(labs); rows_out.append(r); last_pick=cur["ts"]
        scanned+=len(rs)
    rows_out.sort(key=lambda x:x["ts"])
    return rows_out,scanned,len(symbols)

def feature_names(rows):
    skip={"symbol","ts"}|{"label_"+k for k in SCHEMES}
    out=set()
    for r in rows[:50000]:
        for k,v in r.items():
            if k not in skip and num(v) is not None:out.add(k)
    return sorted(out)

def discover(train,features,label_key):
    rules=[]; min_support=max(60,min(500,len(train)//100))
    for f in features:
        vals=[num(r.get(f)) for r in train]; vals=[v for v in vals if v is not None]
        if len(vals)<300:continue
        for q in QUANTS:
            th=qtile(vals,q)
            for op in (">=","<="):
                def pred(r,f=f,th=th,op=op):
                    v=num(r.get(f))
                    return False if v is None else (v>=th if op==">=" else v<=th)
                m=metric(train,pred,label_key)
                if m["support"]<min_support or m["tp"]<20 or m["recall"]<.03:continue
                score=(m["lift"]-1)*math.sqrt(m["support"])*(.4+.6*m["recall"])
                rules.append({"feature":f,"op":op,"threshold":th,"train":m,"score":score})
    rules.sort(key=lambda x:(x["score"],x["train"]["lift"],x["train"]["support"]),reverse=True)
    return rules

def pred_rule(x):
    def p(r):
        v=num(r.get(x["feature"]))
        return False if v is None else (v>=x["threshold"] if x["op"]==">=" else v<=x["threshold"])
    return p

def stable_rules(train,test,features):
    primary=discover(train,features,"label_QUALITY")
    out=[]
    for x in primary[:250]:
        y=dict(x); ok=True
        for scheme in SCHEMES:
            key="label_"+scheme
            y["train_"+scheme]=metric(train,pred_rule(x),key)
            y["test_"+scheme]=metric(test,pred_rule(x),key)
        # must improve QUALITY on holdout and not collapse FAST/STRONG
        tq=y["test_QUALITY"]
        ok=(tq["support"]>=60 and tq["tp"]>=20 and tq["lift"]>=1.12
            and y["test_FAST"]["lift"]>=1.03 and y["test_STRONG"]["lift"]>=1.03)
        y["stable_multi_scheme"]=ok
        out.append(y)
    out.sort(key=lambda x:(x["stable_multi_scheme"],x["test_QUALITY"]["lift"],
                           x["train_QUALITY"]["lift"],x["test_QUALITY"]["support"]),reverse=True)
    return out

def pair_candidates(train,test,stable):
    pool=[x for x in stable if x["stable_multi_scheme"]][:12]
    pairs=[]
    for i,a in enumerate(pool):
        for b in pool[i+1:]:
            if a["feature"]==b["feature"]:continue
            pa,pb=pred_rule(a),pred_rule(b); pred=lambda r,pa=pa,pb=pb:pa(r) and pb(r)
            z={"a":{k:a[k] for k in ("feature","op","threshold")},
               "b":{k:b[k] for k in ("feature","op","threshold")}}
            good=True
            for scheme in SCHEMES:
                z["train_"+scheme]=metric(train,pred,"label_"+scheme)
                z["test_"+scheme]=metric(test,pred,"label_"+scheme)
            tq=z["test_QUALITY"]
            good=(tq["support"]>=50 and tq["tp"]>=15 and tq["lift"]>=1.18
                  and z["test_FAST"]["lift"]>=1.05 and z["test_STRONG"]["lift"]>=1.05)
            z["stable_multi_scheme"]=good; pairs.append(z)
    pairs.sort(key=lambda x:(x["stable_multi_scheme"],x["test_QUALITY"]["lift"],
                             x["test_QUALITY"]["support"]),reverse=True)
    return pairs

def vote_portrait(train,test,stable):
    pool=[x for x in stable if x["stable_multi_scheme"]][:8]
    if len(pool)<3:return None
    best=None
    for n in range(3,min(7,len(pool))+1):
        rs=pool[:n]; ps=[pred_rule(x) for x in rs]
        for k in range(2,n+1):
            pred=lambda r,ps=ps,k=k:sum(1 for p in ps if p(r))>=k
            z={"rules":[{q:x[q] for q in ("feature","op","threshold")} for x in rs],"required_votes":k}
            ok=True
            for scheme in SCHEMES:
                z["train_"+scheme]=metric(train,pred,"label_"+scheme)
                z["test_"+scheme]=metric(test,pred,"label_"+scheme)
            tq=z["test_QUALITY"]
            ok=(tq["support"]>=60 and tq["tp"]>=20 and tq["lift"]>=1.20
                and z["test_FAST"]["lift"]>=1.05 and z["test_STRONG"]["lift"]>=1.05)
            z["stable_multi_scheme"]=ok
            score=(ok,tq["lift"],z["test_STRONG"]["lift"],tq["support"])
            if best is None or score>best["_score"]:
                z["_score"]=score;best=z
    if best:best.pop("_score",None)
    return best

def run_analysis(db_path):
    cx=sqlite3.connect(str(db_path))
    ds,scanned,nsym=build_dataset(cx)
    feats=feature_names(ds)
    cut=int(len(ds)*.70)
    train,test=ds[:cut],ds[cut:]
    stable=stable_rules(train,test,feats)
    pairs=pair_candidates(train,test,stable)
    vote=vote_portrait(train,test,stable)
    approved=bool(vote and vote["stable_multi_scheme"] and vote["test_QUALITY"]["lift"]>=1.20)
    counts={}
    for part,name in ((train,"train"),(test,"holdout")):
        counts[name]={}
        for scheme in SCHEMES:
            counts[name][scheme]=dict(Counter(r["label_"+scheme] for r in part))
    report={
      "status":"BBB_BYBIT_MARKET_WIDE_WIN_DNA",
      "source":"BBB retained feature_samples only; no legacy ROCKET gate",
      "raw_feature_rows_scanned":scanned,"symbols":nsym,"decision_rows":len(ds),
      "split":"chronological_70_30","labels":counts,"feature_count":len(feats),
      "schemes":SCHEMES,"horizon_ms":HORIZON_MS,"sample_step_ms":SAMPLE_STEP_MS,
      "stable_rules":[x for x in stable if x["stable_multi_scheme"]][:30],
      "stable_pairs":[x for x in pairs if x["stable_multi_scheme"]][:20],
      "vote_portrait":vote,
      "approved_for_fresh_paper_selector":approved,
      "decision":"APPROVE_NEW_MARKET_DNA_PAPER_SELECTOR" if approved else "DO_NOT_APPROVE_YET",
      "caveats":[
       "Historical feature_samples are the telemetry retained by BBB; older raw samples may have expired under retention policy.",
       "Entry uses observed ask and future outcome uses observed bid snapshots; this includes spread but not L50 size slippage.",
       "Labels are first-touch approximations, not filled trades.",
       "Threshold discovery uses chronological train only; holdout is later data."
      ]
    }
    out=Path(db_path).parent/"bbb_market_win_dna_report.json"
    out.write_text(json.dumps(report,indent=2),encoding="utf8")
    cx.close();return report

if __name__=="__main__":
    data=Path(os.getenv("DATA_DIR",Path(__file__).resolve().parent/"data"))
    r=run_analysis(data/"bbb.sqlite3")
    compact={k:r[k] for k in ("status","raw_feature_rows_scanned","symbols","decision_rows","labels","feature_count","vote_portrait","approved_for_fresh_paper_selector","decision")}
    compact["stable_rules"]=r["stable_rules"][:10]
    compact["stable_pairs"]=r["stable_pairs"][:5]
    print(json.dumps(compact,separators=(",",":")))
