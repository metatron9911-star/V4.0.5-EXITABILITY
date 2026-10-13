"""Emergency read-only BBB research salvage.

Prints a compact research handoff from the full Railway volume without writing
to /data. It does NOT delete anything. Designed to run once before a separate
destructive cleanup.
"""
from __future__ import annotations
import json, os, sqlite3, statistics
from pathlib import Path
from collections import Counter, defaultdict

DATA=Path(os.getenv("DATA_DIR","/data"))
DB=DATA/"bbb.sqlite3"

def qtile(a,q):
    a=sorted(float(x) for x in a if x is not None)
    if not a:return None
    i=min(len(a)-1,max(0,int(round((len(a)-1)*q))))
    return a[i]

def main():
    out={"status":"BBB_EMERGENCY_RESEARCH_SALVAGE","db":str(DB)}
    if not DB.exists():
        out["error"]="DB_NOT_FOUND"; print("BBB_SALVAGE_JSON "+json.dumps(out,separators=(",",":"))); return
    # read-only, avoid journal writes on a full volume
    cx=sqlite3.connect(f"file:{DB}?mode=ro&immutable=1",uri=True,timeout=10)
    tabs={r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out["tables"]=sorted(tabs)
    out["db_bytes"]=DB.stat().st_size
    for suffix in ("-wal","-shm"):
        p=Path(str(DB)+suffix)
        out[suffix[1:]+"_bytes"]=p.stat().st_size if p.exists() else 0

    if "microflow_episodes" in tabs:
        rows=list(cx.execute("""SELECT symbol,decision_ms,decision_stage,outcome_label,
            shock_score,absorb_score,turn_score,dynamic_score,max_favorable_bps,max_adverse_bps,
            payload_json FROM microflow_episodes ORDER BY id"""))
        out["microflow_count"]=len(rows)
        out["microflow_stage_counts"]=dict(Counter(r[2] for r in rows))
        out["microflow_label_counts"]=dict(Counter(r[3] or "NULL" for r in rows))
        signal=[r for r in rows if r[2]=="SIGNAL"]
        out["signal_label_counts"]=dict(Counter(r[3] or "NULL" for r in signal))
        wl=[r for r in signal if r[3] in ("WIN","LOSS")]
        w=sum(r[3]=="WIN" for r in wl)
        out["signal_precision"]=w/len(wl) if wl else None
        for name,idx in (("shock",4),("absorb",5),("turn",6),("dynamic",7),("mfe",8),("mae",9)):
            out[name+"_by_label"]={}
            for lab in ("WIN","LOSS","NEUTRAL"):
                a=[r[idx] for r in signal if r[3]==lab and r[idx] is not None]
                out[name+"_by_label"][lab]={"n":len(a),"p10":qtile(a,.1),"p50":qtile(a,.5),"p90":qtile(a,.9)}
        # Compact sequence pack: all labeled SIGNAL episodes, capped at 300.
        # Preserve only 6 strategically spaced frames and the fields useful for later synthesis.
        pack=[]
        fields=("ts","ret250","ret1","250_flow","1s_flow","imb","spread","bid5_d250","ask5_d250",
                "250_total_usd","1s_total_usd","250_sell_share","1s_sell_share")
        for r in signal:
            if r[3] not in ("WIN","LOSS"):continue
            try:p=json.loads(r[10] or "{}"); frames=p.get("frames") or []
            except Exception:frames=[]
            if frames:
                idxs=sorted(set([0,len(frames)//5,2*len(frames)//5,3*len(frames)//5,4*len(frames)//5,len(frames)-1]))
                seq=[{k:frames[i].get(k) for k in fields if k in frames[i]} for i in idxs]
            else:seq=[]
            pack.append({"symbol":r[0],"decision_ms":r[1],"label":r[3],
                         "shock":r[4],"absorb":r[5],"turn":r[6],"dynamic":r[7],
                         "mfe":r[8],"mae":r[9],"seq":seq})
            if len(pack)>=300:break
        out["microflow_compact_episodes"]=pack

    if "agent_council_decisions" in tabs:
        crows=list(cx.execute("""SELECT symbol,ts_ms,regime,coordinator_score,decision,veto,payload_json
                                FROM agent_council_decisions ORDER BY id"""))
        out["council_count"]=len(crows)
        out["council_decisions"]=dict(Counter(r[4] for r in crows))
        out["council_regimes"]=dict(Counter(r[2] for r in crows))
        out["council_veto_count"]=sum(int(r[5] or 0) for r in crows)
        by_dec=defaultdict(list)
        for r in crows: by_dec[r[4]].append(r[3])
        out["council_score_by_decision"]={k:{"n":len(v),"p10":qtile(v,.1),"p50":qtile(v,.5),"p90":qtile(v,.9)} for k,v in by_dec.items()}

        # Match nearest council decision to each labeled microflow signal within +/-1s.
        if "microflow_episodes" in tabs:
            eps=list(cx.execute("""SELECT symbol,decision_ms,outcome_label,dynamic_score
                                  FROM microflow_episodes
                                  WHERE decision_stage='SIGNAL' AND outcome_label IN ('WIN','LOSS')"""))
            bysym=defaultdict(list)
            for r in crows:bysym[r[0]].append(r)
            matched=[]
            for sym,ts,lab,dyn in eps:
                arr=bysym.get(sym,[])
                if not arr:continue
                cand=min(arr,key=lambda z:abs(int(z[1])-int(ts)))
                if abs(int(cand[1])-int(ts))<=1000:
                    try:p=json.loads(cand[6] or "{}")
                    except Exception:p={}
                    matched.append({"symbol":sym,"ts":ts,"label":lab,"dynamic":dyn,
                                    "regime":cand[2],"coordinator":cand[3],
                                    "decision":cand[4],"veto":bool(cand[5]),
                                    "votes":{k:{"score":v.get("score"),"confidence":v.get("confidence")}
                                             for k,v in (p.get("votes") or {}).items()}})
            out["council_matched_signals"]=matched[:300]
            out["council_matched_count"]=len(matched)
            out["council_outcome_matrix"]={}
            for dec in ("SUPPORT","WAIT","REJECT","VETO"):
                a=[x for x in matched if x["decision"]==dec]
                out["council_outcome_matrix"][dec]=dict(Counter(x["label"] for x in a))

    cx.close()
    print("BBB_SALVAGE_JSON "+json.dumps(out,separators=(",",":")))

if __name__=="__main__":
    main()
