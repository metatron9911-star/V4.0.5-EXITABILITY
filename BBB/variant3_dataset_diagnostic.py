"""Read-only Variant 3 dataset diagnostics for BBB."""
from __future__ import annotations
import json, math, os, sqlite3, statistics
from collections import Counter
from pathlib import Path

DB=Path(os.getenv("DATA_DIR","/data"))/"bbb.sqlite3"
MIN_TS=int(os.getenv("BBB_V3_MIN_DECISION_MS","0") or 0)
FEATURES=["impulse","absorption","tape","book","weighted","consensus","coordinator",
          "agreement","dispersion","ret250","ret1","flow250","flow1s","spread","imb",
          "shock_score","absorb_score","turn_score","dynamic_score"]

def fnum(x):
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except Exception:return None

def q(xs,p):
    xs=sorted(float(x) for x in xs if x is not None)
    if not xs:return None
    return xs[min(len(xs)-1,max(0,round((len(xs)-1)*p)))]

def desc(xs):
    xs=[fnum(x) for x in xs]; xs=[x for x in xs if x is not None]
    return {"n":len(xs),"p10":q(xs,.1),"p25":q(xs,.25),
            "median":statistics.median(xs) if xs else None,
            "p75":q(xs,.75),"p90":q(xs,.9),
            "mean":sum(xs)/len(xs) if xs else None}

def auc(rows,key):
    a=[(fnum(r.get(key)),1 if r["label"]=="WIN" else 0) for r in rows if r["label"] in ("WIN","LOSS")]
    a=[x for x in a if x[0] is not None]
    pos=sum(y for _,y in a); neg=len(a)-pos
    if not pos or not neg:return None
    a.sort(key=lambda x:x[0])
    ranks=[]; i=0
    while i<len(a):
        j=i+1
        while j<len(a) and a[j][0]==a[i][0]:j+=1
        av=((i+1)+j)/2
        ranks.extend([av]*(j-i)); i=j
    sp=sum(r for r,(_,y) in zip(ranks,a) if y)
    return (sp-pos*(pos+1)/2)/(pos*neg)

def parse(row):
    ts,sym,stage,label,mfe,mae,shock,absorb,turn,dynamic,pj=row
    try:p=json.loads(pj or "{}")
    except Exception:return None
    c=p.get("council") or {}
    if not c:return None
    votes=c.get("votes") or {}; frame=c.get("frame") or {}; rv=c.get("risk_veto") or {}
    regime=c.get("regime")
    if isinstance(regime,dict):regime=regime.get("regime")
    def vote(n):return fnum((votes.get(n) or {}).get("score"))
    out={
      "decision_ms":int(ts),"symbol":sym,"stage":stage,"label":label or "NULL",
      "entry_ask":fnum(p.get("entry_ask")),"decision_mid":fnum(p.get("decision_mid")),
      "mfe_bps":fnum(mfe),"mae_bps":fnum(mae),
      "shock_score":fnum(shock),"absorb_score":fnum(absorb),
      "turn_score":fnum(turn),"dynamic_score":fnum(dynamic),
      "impulse":vote("IMPULSE"),"absorption":vote("ABSORPTION"),
      "tape":vote("TAPE"),"book":vote("BOOK"),
      "consensus":fnum(c.get("consensus")),"coordinator":fnum(c.get("coordinator_score")),
      "agreement":fnum(c.get("agreement")),"dispersion":fnum(c.get("dispersion")),
      "ret250":fnum(frame.get("ret250")),"ret1":fnum(frame.get("ret1")),
      "flow250":fnum(frame.get("flow250")),"flow1s":fnum(frame.get("flow1s")),
      "spread":fnum(frame.get("spread")),"imb":fnum(frame.get("imb")),
      "regime":regime,"hard_veto":bool(rv.get("veto")),
      "veto_reasons":rv.get("reasons") or [],"council_decision":c.get("decision"),
    }
    if None not in (out["impulse"],out["absorption"],out["tape"],out["book"]):
        out["weighted"]=.40*out["impulse"]+.30*out["absorption"]+.20*out["tape"]+.10*out["book"]
    else: out["weighted"]=None
    return out

def dist(a,b):
    dims=("spread","imb","ret1","flow1s","dynamic_score")
    ds=[]
    for d in dims:
        x,y=a.get(d),b.get(d)
        if x is None or y is None:continue
        s=max(abs(x),abs(y),1.0); ds.append(((x-y)/s)**2)
    return sum(ds)/len(ds) if ds else 1e9

def main():
    out={"status":"BBB_VARIANT3_DATASET_DIAGNOSTIC","min_decision_ms":MIN_TS}
    if not DB.exists():
        out["error"]="DB_NOT_FOUND"; print("BBB_V3_DIAG "+json.dumps(out,separators=(",",":"))); return
    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=10)
    rows=list(cx.execute("""SELECT decision_ms,symbol,decision_stage,outcome_label,
      max_favorable_bps,max_adverse_bps,shock_score,absorb_score,turn_score,dynamic_score,payload_json
      FROM microflow_episodes WHERE decision_ms>=? ORDER BY decision_ms""",(MIN_TS,)))
    eps=[parse(r) for r in rows]; eps=[x for x in eps if x and x["entry_ask"] is not None and x["decision_mid"] is not None]
    out["episodes_valid"]=len(eps)
    out["labels"]=dict(Counter(x["label"] for x in eps))
    out["regimes"]=dict(Counter(str(x["regime"]) for x in eps))
    out["hard_veto_n"]=sum(x["hard_veto"] for x in eps)
    out["hard_veto_rate"]=out["hard_veto_n"]/len(eps) if eps else None
    out["veto_reasons"]=dict(Counter(r for x in eps for r in x["veto_reasons"]))

    soft=[x for x in eps if not x["hard_veto"] and x["label"] in ("WIN","LOSS")]
    out["soft_n"]=len(soft); out["soft_labels"]=dict(Counter(x["label"] for x in soft))
    if len(soft)<2:
        out["readiness"]="INSUFFICIENT_SOFT_WIN_LOSS"
        print("BBB_V3_DIAG "+json.dumps(out,separators=(",",":"))); return

    cut=max(1,min(len(soft)-1,int(len(soft)*.70)))
    train,test=soft[:cut],soft[cut:]
    out["split"]={"train_n":len(train),"test_n":len(test),
      "train_labels":dict(Counter(x["label"] for x in train)),
      "test_labels":dict(Counter(x["label"] for x in test)),
      "cut_decision_ms":test[0]["decision_ms"] if test else None}

    out["feature_auc_train"]={k:auc(train,k) for k in FEATURES}
    out["feature_distributions"]={k:{
      "WIN":desc([x.get(k) for x in soft if x["label"]=="WIN"]),
      "LOSS":desc([x.get(k) for x in soft if x["label"]=="LOSS"])
    } for k in FEATURES}

    wins=[x for x in soft if x["label"]=="WIN"]; losses=[x for x in soft if x["label"]=="LOSS"]
    pairs=[]
    for w in wins:
        for l in sorted(losses,key=lambda z:dist(w,z))[:3]:
            pairs.append({"win_ts":w["decision_ms"],"win_symbol":w["symbol"],
                          "loss_ts":l["decision_ms"],"loss_symbol":l["symbol"]})
    out["matched_pairs_n"]=len(pairs); out["matched_pairs_preview"]=pairs[:20]

    test_win=sum(x["label"]=="WIN" for x in test)
    if len(eps)>=500 and len(wins)>=50 and len(losses)>=50 and test_win>=10:
        out["readiness"]="READY_FOR_PROBABILISTIC_V3"
    else:
        out["readiness"]="COLLECT_MORE_DATA"
    cx.close()
    print("BBB_V3_DIAG "+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__": main()
