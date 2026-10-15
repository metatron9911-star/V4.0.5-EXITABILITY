"""Analyze COMMITTEE_V1 PAPER winners vs losers without mutating trading state."""
from __future__ import annotations
import json, os, sqlite3, statistics, itertools
from pathlib import Path
from collections import Counter, defaultdict

DB=Path(os.getenv("DATA_DIR","/data"))/"bbb.sqlite3"

def med(xs):
    xs=[float(x) for x in xs if x is not None]
    return statistics.median(xs) if xs else None

def q(xs,p):
    xs=sorted(float(x) for x in xs if x is not None)
    if not xs:return None
    return xs[min(len(xs)-1,max(0,round((len(xs)-1)*p)))]

def main():
    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=5)
    rows=list(cx.execute("""SELECT id,symbol,opened_ms,closed_ms,entry_vwap,exit_vwap,
        net_pnl_usd,gross_pnl_usd,entry_fee_usd,exit_fee_usd,exit_reason,hold_ms,
        notional_usd,entry_features_json
        FROM trades WHERE risk_state='COMMITTEE_V1' AND status='CLOSED'
        ORDER BY opened_ms"""))
    rec=[]
    for r in rows:
        try:p=json.loads(r[13] or "{}")
        except Exception:p={}
        d=p.get("committee_v1") or {}; m=p.get("microflow") or {}
        rec.append({
          "id":r[0],"symbol":r[1],"opened_ms":r[2],"closed_ms":r[3],
          "net":float(r[6] or 0),"gross":float(r[7] or 0),"entry_fee":float(r[8] or 0),
          "exit_fee":float(r[9] or 0),"reason":r[10],"hold_ms":r[11],"notional":float(r[12] or 0),
          "label":"WIN" if float(r[6] or 0)>0 else "LOSS",
          "regime":d.get("regime"),"weighted":d.get("weighted"),"impulse":d.get("impulse"),
          "absorption":d.get("absorption"),"tape":d.get("tape"),"book":d.get("book"),
          "coord":d.get("coordinator_score"),"agreement":d.get("agreement"),
          "dispersion":d.get("dispersion"),"mid":m.get("mid"),"ret250":m.get("ret250"),
          "ret1":m.get("ret1"),"flow250":m.get("flow250"),"flow1s":m.get("flow1s"),
          "imb":m.get("imb"),"spread":m.get("spread")
        })
    out={"status":"COMMITTEE_TRADE_REVIEW","closed":len(rec),
         "wins":sum(x["label"]=="WIN" for x in rec),"losses":sum(x["label"]=="LOSS" for x in rec)}
    out["exit_reasons"]={k:dict(v) for k,v in defaultdict(Counter).items()}
    er=defaultdict(Counter)
    for x in rec: er[x["reason"]][x["label"]]+=1
    out["exit_reasons"]={k:dict(v) for k,v in er.items()}
    out["regimes"]={}
    for reg in sorted({str(x["regime"]) for x in rec}):
        a=[x for x in rec if str(x["regime"])==reg]
        out["regimes"][reg]={"n":len(a),"WIN":sum(x["label"]=="WIN" for x in a),"LOSS":sum(x["label"]=="LOSS" for x in a),
                             "net":round(sum(x["net"] for x in a),6)}
    out["symbols"]={}
    for sym in sorted({x["symbol"] for x in rec}):
        a=[x for x in rec if x["symbol"]==sym]
        out["symbols"][sym]={"n":len(a),"WIN":sum(x["label"]=="WIN" for x in a),"LOSS":sum(x["label"]=="LOSS" for x in a),
                             "net":round(sum(x["net"] for x in a),6)}
    feats=["weighted","impulse","absorption","tape","book","coord","agreement","dispersion",
           "ret250","ret1","flow250","flow1s","imb","spread"]
    out["feature_compare"]={}
    for k in feats:
        w=[x[k] for x in rec if x["label"]=="WIN" and x[k] is not None]
        l=[x[k] for x in rec if x["label"]=="LOSS" and x[k] is not None]
        out["feature_compare"][k]={"win_n":len(w),"loss_n":len(l),"win_p25":q(w,.25),"win_med":med(w),"win_p75":q(w,.75),
                                  "loss_p25":q(l,.25),"loss_med":med(l),"loss_p75":q(l,.75)}
    # Enumerate simple one-dimensional cuts and a few 2-cut conjunctions.
    candidates=[]
    for k in feats:
        vals=sorted({float(x[k]) for x in rec if x[k] is not None})
        if len(vals)<4:continue
        qs=sorted(set(q(vals,p) for p in (.2,.3,.4,.5,.6,.7,.8)))
        for t in qs:
            for op in ("ge","le"):
                passed=[x for x in rec if x[k] is not None and ((float(x[k])>=t) if op=="ge" else (float(x[k])<=t))]
                if len(passed)<3:continue
                w=sum(x["label"]=="WIN" for x in passed); l=len(passed)-w
                candidates.append({"rules":[[k,op,t]],"n":len(passed),"w":w,"l":l,"precision":w/len(passed)})
    top1=sorted(candidates,key=lambda z:(z["precision"],z["w"],-z["n"]),reverse=True)[:20]
    # Pair only top single rules to contain search size.
    pairs=[]
    rules=[x["rules"][0] for x in top1[:12]]
    for a,b in itertools.combinations(rules,2):
        def ok(x,r):
            k,op,t=r
            return x[k] is not None and ((float(x[k])>=t) if op=="ge" else (float(x[k])<=t))
        passed=[x for x in rec if ok(x,a) and ok(x,b)]
        if len(passed)<3:continue
        w=sum(x["label"]=="WIN" for x in passed); l=len(passed)-w
        pairs.append({"rules":[a,b],"n":len(passed),"w":w,"l":l,"precision":w/len(passed)})
    out["best_single_rules"]=top1[:10]
    out["best_pair_rules"]=sorted(pairs,key=lambda z:(z["precision"],z["w"],-z["n"]),reverse=True)[:10]

    # Chronological 60/40: derive candidates only on first 60%, score on last 40%.
    cut=max(1,int(len(rec)*.6)); train=rec[:cut]; test=rec[cut:]
    trules=[]
    for k in feats:
        vals=sorted({float(x[k]) for x in train if x[k] is not None})
        if len(vals)<4:continue
        for t in sorted(set(q(vals,p) for p in (.25,.4,.5,.6,.75))):
            for op in ("ge","le"):
                p=[x for x in train if x[k] is not None and ((float(x[k])>=t) if op=="ge" else float(x[k])<=t)]
                if len(p)<3:continue
                w=sum(x["label"]=="WIN" for x in p)
                trules.append((w/len(p),w,len(p),[k,op,t]))
    trules=sorted(trules,reverse=True)[:20]
    chrono=[]
    for prec,w,n,r in trules:
        k,op,t=r
        te=[x for x in test if x[k] is not None and ((float(x[k])>=t) if op=="ge" else float(x[k])<=t)]
        tw=sum(x["label"]=="WIN" for x in te)
        chrono.append({"rule":r,"train_n":n,"train_w":w,"train_precision":prec,
                       "test_n":len(te),"test_w":tw,"test_l":len(te)-tw,
                       "test_precision":tw/len(te) if te else None})
    out["chronological_single_rules"]=chrono
    # WIN-envelope search: ranges that retain every observed WIN, ranked by LOSS rejection.
    wins_all=[x for x in rec if x["label"]=="WIN"]
    losses_all=[x for x in rec if x["label"]=="LOSS"]
    envelope_rules=[]
    for k in feats:
        wv=[float(x[k]) for x in wins_all if x[k] is not None]
        if len(wv)!=len(wins_all) or not wv:continue
        lo,hi=min(wv),max(wv)
        surv=[x for x in losses_all if x[k] is not None and lo<=float(x[k])<=hi]
        envelope_rules.append({"feature":k,"lo":lo,"hi":hi,
                               "wins_kept":len(wins_all),"losses_kept":len(surv),
                               "losses_rejected":len(losses_all)-len(surv)})
    envelope_rules=sorted(envelope_rules,key=lambda z:(z["losses_rejected"],-(z["hi"]-z["lo"])),reverse=True)
    out["win_envelope_single"]=envelope_rules
    envpairs=[]
    for a,b in itertools.combinations(envelope_rules[:10],2):
        surv=[x for x in losses_all
              if x[a["feature"]] is not None and a["lo"]<=float(x[a["feature"]])<=a["hi"]
              and x[b["feature"]] is not None and b["lo"]<=float(x[b["feature"]])<=b["hi"]]
        envpairs.append({"rules":[[a["feature"],a["lo"],a["hi"]],[b["feature"],b["lo"],b["hi"]]],
                         "wins_kept":len(wins_all),"losses_kept":len(surv),
                         "losses_rejected":len(losses_all)-len(surv)})
    out["win_envelope_pairs"]=sorted(envpairs,key=lambda z:(z["losses_rejected"],-z["losses_kept"]),reverse=True)[:15]
    envtrip=[]
    for combo in itertools.combinations(envelope_rules[:8],3):
        surv=[]
        for x in losses_all:
            ok=True
            for a in combo:
                if x[a["feature"]] is None or not (a["lo"]<=float(x[a["feature"]])<=a["hi"]):
                    ok=False;break
            if ok:surv.append(x)
        envtrip.append({"rules":[[a["feature"],a["lo"],a["hi"]] for a in combo],
                        "wins_kept":len(wins_all),"losses_kept":len(surv),
                        "losses_rejected":len(losses_all)-len(surv)})
    out["win_envelope_triples"]=sorted(envtrip,key=lambda z:(z["losses_rejected"],-z["losses_kept"]),reverse=True)[:15]

    # Full rows compactly for exact diagnosis.
    out["trades"]=rec
    cx.close()
    print("BBB_COMMITTEE_TRADE_REVIEW "+json.dumps(out,separators=(",",":")),flush=True)

if __name__=="__main__":main()
