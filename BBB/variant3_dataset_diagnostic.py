"""Read-only Variant 3 dataset diagnostics for BBB.

This script NEVER changes trading policy. It validates the training set for a
future probabilistic entry model.

Key rules:
- hard-veto episodes are excluded from probabilistic training;
- only exact soft rejects (committee approved=False, hard_veto=False) are used;
- NEUTRAL is kept as a separate class and never silently folded into LOSS;
- chronological train/test split only;
- matched LOSS uses regime + spread bucket + +/-30min where possible;
- both market-move labels and a fee-aware reachable-profit diagnostic are shown.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics
from collections import Counter
from pathlib import Path

DB=Path(os.getenv("DATA_DIR","/data"))/"bbb.sqlite3"
MIN_TS=int(os.getenv("BBB_V3_MIN_DECISION_MS","0") or 0)
TRAIN_FRAC=float(os.getenv("BBB_V3_TRAIN_FRAC","0.70"))
MATCH_WINDOW_MS=int(os.getenv("BBB_V3_MATCH_WINDOW_MS",str(30*60*1000)))

# Bybit INNOVATION taker fee in the current BBB engine is 11 bps each side.
ROUNDTRIP_FEE_BPS=float(os.getenv("BBB_V3_ROUNDTRIP_FEE_BPS","22"))
# Diagnostic only: how much NET favorable excursion we want after fees.
MIN_NET_EDGE_BPS=float(os.getenv("BBB_V3_MIN_NET_EDGE_BPS","50"))

FEATURES=[
  "impulse","absorption","tape","book","weighted","consensus","coordinator",
  "agreement","dispersion","ret250","ret1","flow250","flow1s","spread","imb",
  "shock_score","absorb_score","turn_score","dynamic_score"
]

def fnum(x):
    try:
        y=float(x)
        return y if math.isfinite(y) else None
    except Exception:
        return None

def q(xs,p):
    xs=sorted(float(x) for x in xs if x is not None)
    if not xs:return None
    return xs[min(len(xs)-1,max(0,round((len(xs)-1)*p)))]

def desc(xs):
    xs=[fnum(x) for x in xs]
    xs=[x for x in xs if x is not None]
    return {
      "n":len(xs),"p10":q(xs,.10),"p25":q(xs,.25),
      "median":statistics.median(xs) if xs else None,
      "p75":q(xs,.75),"p90":q(xs,.90),
      "mean":sum(xs)/len(xs) if xs else None
    }

def auc(rows,key):
    a=[(fnum(r.get(key)),1 if r["label"]=="WIN" else 0)
       for r in rows if r["label"] in ("WIN","LOSS")]
    a=[x for x in a if x[0] is not None]
    pos=sum(y for _,y in a); neg=len(a)-pos
    if not pos or not neg:return None
    a.sort(key=lambda x:x[0])
    ranks=[]; i=0
    while i<len(a):
        j=i+1
        while j<len(a) and a[j][0]==a[i][0]:j+=1
        av=((i+1)+j)/2.0
        ranks.extend([av]*(j-i)); i=j
    sp=sum(rank for rank,(_,y) in zip(ranks,a) if y)
    return (sp-pos*(pos+1)/2)/(pos*neg)

def pearson(rows,a,b):
    pairs=[(fnum(r.get(a)),fnum(r.get(b))) for r in rows]
    pairs=[(x,y) for x,y in pairs if x is not None and y is not None]
    if len(pairs)<3:return None
    xs=[x for x,_ in pairs]; ys=[y for _,y in pairs]
    mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
    sx=sum((x-mx)**2 for x in xs); sy=sum((y-my)**2 for y in ys)
    if sx<=0 or sy<=0:return None
    return sum((x-mx)*(y-my) for x,y in pairs)/math.sqrt(sx*sy)

def spread_bucket(x):
    x=fnum(x)
    if x is None:return "NA"
    if x<2:return "<2"
    if x<5:return "2-5"
    if x<10:return "5-10"
    return ">=10"

def parse(row):
    ts,sym,stage,label,mfe,mae,shock,absorb,turn,dynamic,pj=row
    try:p=json.loads(pj or "{}")
    except Exception:return None
    c=p.get("council") or {}
    committee=p.get("committee") or {}
    if not c:return None

    votes=c.get("votes") or {}
    frame=c.get("frame") or {}
    rv=c.get("risk_veto") or {}
    regime=c.get("regime")
    if isinstance(regime,dict):regime=regime.get("regime")

    def vote(n):return fnum((votes.get(n) or {}).get("score"))

    approved=committee.get("approved")
    detail=committee.get("detail") or {}
    committee_checks=detail.get("checks") or {}
    committee_strategy=committee.get("strategy")

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
      "regime":regime,
      "hard_veto":bool(rv.get("veto")),
      "veto_reasons":rv.get("reasons") or [],
      "council_decision":c.get("decision"),
      "committee_approved":approved if isinstance(approved,bool) else None,
      "committee_checks":committee_checks,
      "committee_strategy":committee_strategy,
      "execution_block":detail.get("execution_block"),
    }
    if None not in (out["impulse"],out["absorption"],out["tape"],out["book"]):
        out["weighted"]=.40*out["impulse"]+.30*out["absorption"]+.20*out["tape"]+.10*out["book"]
    else:
        out["weighted"]=None

    out["spread_bucket"]=spread_bucket(out["spread"])
    # Diagnostic only. max_favorable_bps is future BID vs decision entry ASK, so
    # spread is already embedded; subtract taker fees to estimate reachable net edge.
    out["reachable_net_mfe_bps"]=(
      out["mfe_bps"]-ROUNDTRIP_FEE_BPS if out["mfe_bps"] is not None else None
    )
    if out["reachable_net_mfe_bps"] is None:
        out["economic_label"]="UNKNOWN"
    elif out["reachable_net_mfe_bps"]>=MIN_NET_EDGE_BPS:
        out["economic_label"]="ECON_WIN"
    elif out["mae_bps"] is not None and out["mae_bps"]<=-(MIN_NET_EDGE_BPS+ROUNDTRIP_FEE_BPS):
        out["economic_label"]="ECON_LOSS"
    else:
        out["economic_label"]="ECON_NEUTRAL"
    return out

def match_losses(win,losses,k=3):
    # First choice: same regime + same spread bucket + +/-30m.
    levels=[
      lambda l: l["regime"]==win["regime"] and l["spread_bucket"]==win["spread_bucket"]
                and abs(l["decision_ms"]-win["decision_ms"])<=MATCH_WINDOW_MS,
      lambda l: l["regime"]==win["regime"] and l["spread_bucket"]==win["spread_bucket"],
      lambda l: l["regime"]==win["regime"],
    ]
    pool=[]
    for pred in levels:
        pool=[l for l in losses if pred(l)]
        if pool:break
    pool.sort(key=lambda l:(
      abs(l["decision_ms"]-win["decision_ms"]),
      abs((l.get("spread") or 0)-(win.get("spread") or 0))
    ))
    return pool[:k]

def main():
    out={
      "status":"BBB_VARIANT3_DATASET_DIAGNOSTIC",
      "min_decision_ms":MIN_TS,
      "roundtrip_fee_bps":ROUNDTRIP_FEE_BPS,
      "min_net_edge_bps":MIN_NET_EDGE_BPS,
    }
    if not DB.exists():
        out["error"]="DB_NOT_FOUND"
        print("BBB_V3_DIAG "+json.dumps(out,separators=(",",":"))); return

    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=10)
    rows=list(cx.execute("""SELECT decision_ms,symbol,decision_stage,outcome_label,
      max_favorable_bps,max_adverse_bps,shock_score,absorb_score,turn_score,dynamic_score,payload_json
      FROM microflow_episodes WHERE decision_ms>=? ORDER BY decision_ms""",(MIN_TS,)))
    cx.close()

    eps=[parse(r) for r in rows]
    eps=[x for x in eps if x is not None]
    valid=[x for x in eps if x["entry_ask"] is not None and x["decision_mid"] is not None]

    out["episodes_total"]=len(eps)
    out["episodes_valid"]=len(valid)
    out["labels"]=dict(Counter(x["label"] for x in valid))
    out["economic_labels"]=dict(Counter(x["economic_label"] for x in valid))
    out["regimes"]=dict(Counter(str(x["regime"]) for x in valid))

    rejects=[x for x in valid if x["committee_approved"] is False]
    hard=[x for x in rejects if x["hard_veto"]]
    soft_rejects=[x for x in rejects if not x["hard_veto"]]
    approved=[x for x in valid if x["committee_approved"] is True]

    out["committee_snapshot_coverage"]=(
      sum(x["committee_approved"] is not None for x in valid)/len(valid) if valid else None
    )
    out["approved_n"]=len(approved)
    out["rejected_n"]=len(rejects)
    out["hard_veto_n"]=len(hard)
    out["soft_reject_n"]=len(soft_rejects)
    out["hard_veto_share_of_rejects"]=len(hard)/len(rejects) if rejects else None
    out["veto_reasons"]=dict(Counter(r for x in hard for r in x["veto_reasons"]))
    out["soft_reject_labels"]=dict(Counter(x["label"] for x in soft_rejects))
    out["soft_reject_economic_labels"]=dict(Counter(x["economic_label"] for x in soft_rejects))

    # NEUTRAL remains visible but is excluded from binary WIN-vs-LOSS modelling.
    binary=[x for x in soft_rejects if x["label"] in ("WIN","LOSS")]
    wins=[x for x in binary if x["label"]=="WIN"]
    losses=[x for x in binary if x["label"]=="LOSS"]

    cut=max(1,min(len(binary)-1,int(len(binary)*TRAIN_FRAC))) if len(binary)>=2 else 0
    train=binary[:cut] if cut else []
    test=binary[cut:] if cut else []

    trw=sum(x["label"]=="WIN" for x in train); trl=len(train)-trw
    tew=sum(x["label"]=="WIN" for x in test); tel=len(test)-tew
    train_wr=trw/len(train) if train else None
    test_wr=tew/len(test) if test else None
    rel_shift=None
    abs_shift=None
    if train_wr is not None and test_wr is not None:
        abs_shift=abs(test_wr-train_wr)
        if train_wr>0: rel_shift=abs(test_wr-train_wr)/train_wr

    out["split"]={
      "train_fraction":TRAIN_FRAC,
      "train_n":len(train),"test_n":len(test),
      "train_labels":{"WIN":trw,"LOSS":trl},
      "test_labels":{"WIN":tew,"LOSS":tel},
      "train_win_rate":train_wr,"test_win_rate":test_wr,
      "absolute_win_rate_shift":abs_shift,
      "relative_win_rate_shift":rel_shift,
      "cut_decision_ms":test[0]["decision_ms"] if test else None,
    }

    # Missingness is checked on the actual soft-reject binary set.
    missing={}
    for k in FEATURES:
        miss=sum(x.get(k) is None for x in binary)
        missing[k]={"missing_n":miss,"missing_rate":miss/len(binary) if binary else None}
    out["missingness"]=missing

    aucs=[]
    for k in FEATURES:
        a=auc(train,k) if train else None
        aucs.append({
          "feature":k,
          "auc":a,
          "separability":max(a,1-a) if a is not None else None,
          "direction":"higher=>WIN" if a is not None and a>=.5 else ("lower=>WIN" if a is not None else None)
        })
    aucs.sort(key=lambda z:(z["separability"] is not None,z["separability"] or -1),reverse=True)
    out["feature_auc_train_sorted"]=aucs

    out["feature_distributions"]={k:{
      "WIN":desc([x.get(k) for x in binary if x["label"]=="WIN"]),
      "LOSS":desc([x.get(k) for x in binary if x["label"]=="LOSS"])
    } for k in FEATURES}

    # Pairwise Pearson correlation matrix on train only.
    corr={}
    for a in FEATURES:
        corr[a]={}
        for b in FEATURES:
            corr[a][b]=pearson(train,a,b) if train else None
    out["correlation_matrix_train"]=corr

    pairs=[]
    for w in wins:
        for l in match_losses(w,losses,3):
            pairs.append({
              "win_ts":w["decision_ms"],"win_symbol":w["symbol"],
              "loss_ts":l["decision_ms"],"loss_symbol":l["symbol"],
              "regime":w["regime"],"spread_bucket":w["spread_bucket"],
              "delta_minutes":round(abs(l["decision_ms"]-w["decision_ms"])/60000,2)
            })
    out["matched_pairs_n"]=len(pairs)
    out["matched_pairs_preview"]=pairs[:30]

    # Readiness checklist requested by the research plan.
    criteria={
      "episodes_ge_500":len(valid)>=500,
      "soft_reject_ge_300":len(soft_rejects)>=300,
      "rejected_win_ge_50":len(wins)>=50,
      "rejected_loss_ge_50":len(losses)>=50,
      "test_win_ge_20":tew>=20,
      "test_loss_ge_20":tel>=20,
      "stationarity_rel_shift_le_25pct":rel_shift is not None and rel_shift<=.25,
      "feature_missingness_le_30pct":all(
          v["missing_rate"] is not None and v["missing_rate"]<=.30 for v in missing.values()
      ) if binary else False,
      "committee_snapshot_coverage_ge_95pct":(
          out["committee_snapshot_coverage"] is not None and
          out["committee_snapshot_coverage"]>=.95
      ),
    }
    out["readiness_checks"]=criteria
    out["readiness"]="READY_FOR_PROBABILISTIC_V3" if all(criteria.values()) else "COLLECT_MORE_DATA"

    print("BBB_V3_DIAG "+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
