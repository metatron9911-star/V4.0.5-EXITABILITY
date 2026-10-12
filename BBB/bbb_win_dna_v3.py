"""BBB Bybit WIN-DNA V3 — high precision / hard-negative research.

Goal: distinguish WIN from LOSS inside active market episodes, not merely enrich
a low base rate. One anchor per episode, chronological discovery/validation/blind
test, thresholds discovered on discovery only.

Research only. No trading.
"""
from __future__ import annotations
import json, math, os, sqlite3, itertools
from pathlib import Path
from collections import Counter

MAX_RAW_ROWS=300_000
HORIZON_MS=20_000
ANCHOR_GAP_MS=10_000
TP_BPS=80.0
SL_BPS=-30.0
QUANTS=(.10,.20,.30,.40,.50,.60,.70,.80,.90)

def num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception:return None

def qtile(a,q):
    a=sorted(a)
    if not a:return None
    z=(len(a)-1)*q; lo=int(z); hi=min(len(a)-1,lo+1); t=z-lo
    return a[lo]*(1-t)+a[hi]*t

def parse(z):
    try:p=json.loads(z[15] or "{}")
    except Exception:p={}
    db=num(z[5]); da=num(z[6]); br=num(z[10]); rv=abs(num(z[14]) or 0)
    r={
      "ts":int(z[0]),"mid":num(z[1]),"bid":num(z[2]),"ask":num(z[3]),
      "spread":num(z[4]),
      "depth_min":min(db,da) if db is not None and da is not None else None,
      "depth_total":db+da if db is not None and da is not None else None,
      "imb":num(z[7]),"t1":num(z[8]),"t5":num(z[9]),"buy_ratio":br,
      "buy_pressure":2*(br-.5) if br is not None else None,
      "r250":num(z[11]),"r1":num(z[12]),"r5":num(z[13]),"rv":rv,
      "range24":num(p.get("range24h_bps")),
      "turnover":num(p.get("turnover24h")),
      "activity":num(p.get("live_activity_score")),
    }
    if r["t1"] is not None and r["t5"] not in (None,0):
        r["tape_accel"]=r["t1"]/(r["t5"]/5.0+1e-9)
    if r["r1"] is not None and r["r5"] is not None:
        r["ret_accel"]=r["r1"]-r["r5"]/5.0
    if r["r250"] is not None and r["r1"] is not None:
        r["micro_accel"]=r["r250"]*4.0-r["r1"]
    if r["r1"] is not None and r["rv"] is not None:
        r["r1_over_rv"]=r["r1"]/(r["rv"]+.1)
    if r["depth_min"] is not None and r["spread"] is not None:
        r["depth_per_spread"]=r["depth_min"]/(r["spread"]+.1)
    return r

BASE=("spread","depth_min","depth_total","imb","t1","t5","buy_ratio","buy_pressure",
      "r250","r1","r5","rv","range24","turnover","activity","tape_accel",
      "ret_accel","micro_accel","r1_over_rv","depth_per_spread")

def load(cx,symbol,min_id):
    q="""SELECT ts_ms,mid,bid,ask,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                book_imbalance_5,trade_count_1s,trade_count_5s,buy_ratio_1s,
                ret_250ms_bps,ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,payload_json
         FROM feature_samples WHERE symbol=? AND id>=? ORDER BY ts_ms"""
    return [parse(z) for z in cx.execute(q,(symbol,min_id))]

def first_touch(rows,i):
    ask=rows[i]["ask"]
    if not ask or ask<=0:return None
    end=rows[i]["ts"]+HORIZON_MS
    for j in range(i+1,len(rows)):
        z=rows[j]
        if z["ts"]>end:break
        bid=z["bid"]
        if not bid or bid<=0:continue
        bps=(bid/ask-1)*10000
        if bps<=SL_BPS:return "LOSS"
        if bps>=TP_BPS:return "WIN"
    return None

def nearest_before(hist,ts):
    for x in reversed(hist):
        if x["ts"]<=ts:return x
    return None

def enrich(cur,hist):
    r={k:cur.get(k) for k in BASE}
    for sec in (2,5,10):
        prev=nearest_before(hist,cur["ts"]-sec*1000)
        if not prev:continue
        for k in ("spread","depth_min","imb","t1","t5","buy_pressure","r250","r1","r5","rv","activity","tape_accel"):
            a=num(cur.get(k)); b=num(prev.get(k))
            if a is not None and b is not None:r[f"d{sec}_{k}"]=a-b
    # directional consensus / shape
    vals=[num(cur.get("r250")),num(cur.get("r1")),num(cur.get("r5"))]
    r["positive_return_votes"]=sum(1 for v in vals if v is not None and v>0)
    r["negative_return_votes"]=sum(1 for v in vals if v is not None and v<0)
    return r

def dataset(cx):
    max_id=int(cx.execute("SELECT COALESCE(MAX(id),0) FROM feature_samples").fetchone()[0] or 0)
    min_id=max(0,max_id-MAX_RAW_ROWS+1)
    syms=[x[0] for x in cx.execute("SELECT DISTINCT symbol FROM feature_samples WHERE id>=? ORDER BY symbol",(min_id,))]
    ds=[]; raw=0
    for sym in syms:
        rows=load(cx,sym,min_id);raw+=len(rows);hist=[];last_anchor=-10**18
        for i,cur in enumerate(rows):
            # one anchor per 10s episode; no legacy ROCKET gate
            if cur["ts"]-last_anchor<ANCHOR_GAP_MS:
                hist.append(cur);continue
            y=first_touch(rows,i)
            if y is None:
                hist.append(cur);continue
            r=enrich(cur,hist);r.update({"symbol":sym,"ts":cur["ts"],"label":y})
            ds.append(r);last_anchor=cur["ts"];hist.append(cur)
    ds.sort(key=lambda x:x["ts"])
    return ds,raw,len(syms)

def metric(rows,pred):
    tp=fp=tn=fn=0
    for r in rows:
        p=bool(pred(r)); y=r["label"]=="WIN"
        if p and y:tp+=1
        elif p and not y:fp+=1
        elif not p and not y:tn+=1
        else:fn+=1
    n=tp+fp+tn+fn;base=(tp+fn)/n if n else 0
    prec=tp/(tp+fp) if tp+fp else 0;rec=tp/(tp+fn) if tp+fn else 0
    return {"n":n,"tp":tp,"fp":fp,"tn":tn,"fn":fn,"support":tp+fp,
            "precision":round(prec,6),"recall":round(rec,6),"base":round(base,6),
            "lift":round(prec/base,6) if base else 0}

def feats(rows):
    skip={"symbol","ts","label"}
    s=set()
    for r in rows:
        for k,v in r.items():
            if k not in skip and num(v) is not None:s.add(k)
    return sorted(s)

def thresholds(dev,fs):
    rules=[]
    min_support=max(30,len(dev)//250)
    for f in fs:
        vals=[num(r.get(f)) for r in dev];vals=[v for v in vals if v is not None]
        if len(vals)<200:continue
        for q in QUANTS:
            th=qtile(vals,q)
            for op in (">=","<="):
                def p(r,f=f,th=th,op=op):
                    v=num(r.get(f))
                    return False if v is None else (v>=th if op==">=" else v<=th)
                m=metric(dev,p)
                if m["support"]<min_support or m["tp"]<8:continue
                rules.append({"feature":f,"op":op,"threshold":th,"dev":m})
    # high precision first, then enough true positives
    rules.sort(key=lambda x:(x["dev"]["precision"],x["dev"]["tp"],x["dev"]["lift"]),reverse=True)
    return rules

def pred_rule(x):
    def p(r):
        v=num(r.get(x["feature"]))
        return False if v is None else (v>=x["threshold"] if x["op"]==">=" else v<=x["threshold"])
    return p

def select_on_validation(dev,val,rules):
    # evaluate only rules discovered on dev
    out=[]
    for x in rules[:250]:
        y=dict(x);y["val"]=metric(val,pred_rule(x));out.append(y)
    # rules that truly discriminate, not just activity
    good=[x for x in out if x["val"]["support"]>=15 and x["val"]["tp"]>=5 and x["val"]["precision"]>=0.20]
    good.sort(key=lambda x:(x["val"]["precision"],x["val"]["tp"],x["dev"]["precision"]),reverse=True)
    return out,good

def combo_search(dev,val,good):
    # de-duplicate similar feature thresholds, keep best per feature/op
    uniq=[];seen=set()
    for x in good:
        key=(x["feature"],x["op"])
        if key in seen:continue
        seen.add(key);uniq.append(x)
        if len(uniq)>=24:break
    combos=[]
    for size in (2,3,4):
        for rs in itertools.combinations(uniq,size):
            fs=[x["feature"] for x in rs]
            if len(set(fs))<size:continue
            ps=[pred_rule(x) for x in rs]
            pred=lambda r,ps=ps:all(p(r) for p in ps)
            dm=metric(dev,pred);vm=metric(val,pred)
            if dm["support"]<15 or vm["support"]<8 or vm["tp"]<3:continue
            combos.append({"rules":[{k:x[k] for k in ("feature","op","threshold")} for x in rs],
                           "dev":dm,"val":vm})
    combos.sort(key=lambda x:(x["val"]["precision"],x["val"]["tp"],x["dev"]["precision"]),reverse=True)
    return combos

def loss_veto(dev,val,base_combo,all_rules):
    if not base_combo:return None
    bps=[pred_rule(x) for x in base_combo["rules"]]
    def base(r):return all(p(r) for p in bps)
    # Search a single veto condition that removes more losses than wins from base candidates.
    candidates=[]
    for x in all_rules[:250]:
        p=pred_rule(x)
        for invert in (False,True):
            veto=lambda r,p=p,invert=invert: (not p(r)) if invert else p(r)
            pred=lambda r,base=base,veto=veto: base(r) and not veto(r)
            dm=metric(dev,pred);vm=metric(val,pred)
            if vm["support"]<6 or vm["tp"]<2:continue
            candidates.append({"rule":{k:x[k] for k in ("feature","op","threshold")},
                               "invert":invert,"dev":dm,"val":vm})
    candidates.sort(key=lambda x:(x["val"]["precision"],x["val"]["tp"]),reverse=True)
    return candidates[0] if candidates else None

def run(db):
    cx=sqlite3.connect(str(db));ds,raw,nsym=dataset(cx);cx.close()
    n=len(ds);a=int(n*.60);b=int(n*.80)
    dev,val,test=ds[:a],ds[a:b],ds[b:]
    fs=feats(ds);rules=thresholds(dev,fs);evaluated,good=select_on_validation(dev,val,rules)
    combos=combo_search(dev,val,good)
    chosen=combos[0] if combos else None
    veto=loss_veto(dev,val,chosen,evaluated) if chosen else None
    final_pred=None;final_desc=None
    if chosen:
        ps=[pred_rule(x) for x in chosen["rules"]]
        base=lambda r,ps=ps:all(p(r) for p in ps)
        if veto and veto["val"]["precision"]>chosen["val"]["precision"]:
            vr=pred_rule(veto["rule"]); inv=veto["invert"]
            vv=lambda r,vr=vr,inv=inv: (not vr(r)) if inv else vr(r)
            final_pred=lambda r,base=base,vv=vv: base(r) and not vv(r)
            final_desc={"combo":chosen,"veto":veto}
        else:
            final_pred=base;final_desc={"combo":chosen,"veto":None}
    final_test=metric(test,final_pred) if final_pred else None
    approved=bool(final_test and final_test["support"]>=10 and final_test["tp"]>=5
                  and final_test["precision"]>=0.45)
    report={
      "status":"BBB_BYBIT_WIN_DNA_V3_HIGH_PRECISION",
      "raw_rows":raw,"symbols":nsym,"episodes":n,"feature_count":len(fs),
      "split":{"discovery":len(dev),"validation":len(val),"blind_test":len(test)},
      "base_rates":{"discovery":metric(dev,lambda r:True)["base"],
                    "validation":metric(val,lambda r:True)["base"],
                    "blind_test":metric(test,lambda r:True)["base"]},
      "top_single_rules":evaluated[:25],
      "top_validated_rules":good[:20],
      "top_combos":combos[:20],
      "selected":final_desc,
      "blind_test":final_test,
      "approval_gate":"blind precision >=0.45, >=10 signals, >=5 WIN",
      "approved":approved,
      "decision":"APPROVE_HIGH_PRECISION_PAPER_SELECTOR" if approved else "NO_HIGH_PRECISION_SELECTOR_FOUND"
    }
    out=Path(db).parent/"bbb_win_dna_v3_report.json"
    out.write_text(json.dumps(report,indent=2),encoding="utf8")
    return report

if __name__=="__main__":
    db=Path(os.getenv("DATA_DIR",Path(__file__).resolve().parent/"data"))/"bbb.sqlite3"
    print(json.dumps(run(db),separators=(",",":")))
