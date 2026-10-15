"""Daily BBB broker-council review.

Read-only analysis of bounded research tables. It never enables trading and never
changes weights automatically. It prints a compact JSON report for an operator
or a later tuning task.
"""
from __future__ import annotations
import json, os, sqlite3, statistics
from pathlib import Path
from collections import Counter, defaultdict

DATA=Path(os.getenv("DATA_DIR","/data"))
DB=DATA/"bbb.sqlite3"

def med(xs):
    xs=[float(x) for x in xs if x is not None]
    return statistics.median(xs) if xs else None

def analyze():
    out={"status":"BBB_COUNCIL_DAILY_REVIEW"}
    if not DB.exists():
        out["error"]="DB_NOT_FOUND"; return out
    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=5)
    eps=list(cx.execute("""SELECT symbol,decision_ms,decision_stage,outcome_label,
                                 shock_score,absorb_score,turn_score,dynamic_score,
                                 max_favorable_bps,max_adverse_bps
                          FROM microflow_episodes ORDER BY decision_ms"""))
    cds=list(cx.execute("""SELECT symbol,ts_ms,regime,coordinator_score,decision,veto,payload_json
                          FROM agent_council_decisions ORDER BY ts_ms"""))
    out["episodes"]=len(eps)
    out["episode_labels"]=dict(Counter((r[3] or "NULL") for r in eps))
    sig=[r for r in eps if r[2]=="SIGNAL" and r[3] in ("WIN","LOSS")]
    out["signals_labeled"]=len(sig)
    out["signal_outcomes"]=dict(Counter(r[3] for r in sig))
    wins=sum(r[3]=="WIN" for r in sig)
    out["signal_precision"]=round(wins/len(sig),6) if sig else None
    out["council_rows"]=len(cds)
    out["council_decisions"]=dict(Counter(r[4] for r in cds))
    out["council_regimes"]=dict(Counter(r[2] for r in cds))

    bysym=defaultdict(list)
    for r in cds:bysym[r[0]].append(r)
    matched=[]
    for e in sig:
        sym,ts=e[0],int(e[1])
        arr=bysym.get(sym,[])
        if not arr:continue
        c=min(arr,key=lambda r:abs(int(r[1])-ts))
        if abs(int(c[1])-ts)>2500:continue
        try:p=json.loads(c[6] or "{}")
        except Exception:p={}
        votes=p.get("votes") or {}
        matched.append({
          "label":e[3],"regime":c[2],"coordinator":c[3],"decision":c[4],"veto":bool(c[5]),
          "votes":{k:{"score":v.get("score"),"confidence":v.get("confidence")} for k,v in votes.items()}
        })
    out["matched"]=len(matched)
    out["decision_outcomes"]={}
    for dec in ("SUPPORT","WAIT","REJECT","VETO"):
        a=[x for x in matched if x["decision"]==dec]
        out["decision_outcomes"][dec]={"n":len(a),"WIN":sum(x["label"]=="WIN" for x in a),
                                      "LOSS":sum(x["label"]=="LOSS" for x in a)}
    agents=sorted({k for x in matched for k in x["votes"]})
    out["agent_separation"]={}
    for a in agents:
        w=[x["votes"].get(a,{}).get("score") for x in matched if x["label"]=="WIN"]
        l=[x["votes"].get(a,{}).get("score") for x in matched if x["label"]=="LOSS"]
        wm,lm=med(w),med(l)
        out["agent_separation"][a]={
          "win_median":wm,"loss_median":lm,
          "gap":round(wm-lm,6) if wm is not None and lm is not None else None,
          "win_n":sum(v is not None for v in w),"loss_n":sum(v is not None for v in l)
        }
    out["regime_outcomes"]={}
    for reg in sorted({x["regime"] for x in matched}):
        a=[x for x in matched if x["regime"]==reg]
        out["regime_outcomes"][reg]={"n":len(a),"WIN":sum(x["label"]=="WIN" for x in a),
                                     "LOSS":sum(x["label"]=="LOSS" for x in a)}
    # Tune only with real evidence; otherwise explicitly refuse.
    if len(matched)>=40 and wins>=8 and (len(sig)-wins)>=15:
        out["tuning_readiness"]="ENOUGH_FOR_WEIGHT_REVIEW"
    else:
        out["tuning_readiness"]="INSUFFICIENT_SAMPLE_DO_NOT_OVERFIT"
    cx.close()
    return out

def main():
    out=analyze()
    print("BBB_COUNCIL_REVIEW "+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":
    main()
