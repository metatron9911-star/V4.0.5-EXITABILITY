"""BBB Bybit WIN-DNA V4 — trajectory archetypes and matched hard negatives.

Purpose:
1) reconstruct 20s pre-entry trajectories for historical WIN/LOSS episodes,
2) cluster WIN trajectories into recurring archetypes,
3) match each WIN archetype against similar LOSS contexts,
4) identify where the sequence diverges,
5) validate archetype signatures on later chronological data.

Research only. No trading.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics
from pathlib import Path
from collections import Counter, defaultdict

MAX_RAW_ROWS=300_000
HORIZON_MS=20_000
ANCHOR_GAP_MS=10_000
TP_BPS=80.0
SL_BPS=-30.0
OFFSETS=(-20,-15,-10,-5,-2,0)
FEATURES=("r250","r1","r5","buy_pressure","imb","t1","t5","rv","spread","depth_min","activity","tape_accel")
K=4

def num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except Exception:return None

def median(a):
    a=[x for x in a if x is not None]
    return statistics.median(a) if a else None

def mad(a):
    a=[x for x in a if x is not None]
    if not a:return 1.0
    m=statistics.median(a)
    d=statistics.median([abs(x-m) for x in a])
    return d if d>1e-9 else 1.0

def parse(z):
    try:p=json.loads(z[15] or "{}")
    except Exception:p={}
    db=num(z[5]); da=num(z[6]); br=num(z[10])
    r={
      "ts":int(z[0]),"mid":num(z[1]),"bid":num(z[2]),"ask":num(z[3]),
      "spread":num(z[4]),
      "depth_min":min(db,da) if db is not None and da is not None else None,
      "imb":num(z[7]),"t1":num(z[8]),"t5":num(z[9]),"buy_ratio":br,
      "buy_pressure":2*(br-.5) if br is not None else None,
      "r250":num(z[11]),"r1":num(z[12]),"r5":num(z[13]),"rv":abs(num(z[14]) or 0.0),
      "activity":num(p.get("live_activity_score")),
    }
    if r["t1"] is not None and r["t5"] not in (None,0):
        r["tape_accel"]=r["t1"]/(r["t5"]/5.0+1e-9)
    else:r["tape_accel"]=None
    return r

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

def nearest(rows,i,target_ts,tol_ms=3500):
    best=None; bestd=10**18
    j=i
    while j>=0 and rows[j]["ts"]>=target_ts-tol_ms:
        d=abs(rows[j]["ts"]-target_ts)
        if d<bestd:
            bestd=d;best=rows[j]
        j-=1
    return best if best is not None and bestd<=tol_ms else None

def trajectory(rows,i):
    cur=rows[i]; out={}
    for off in OFFSETS:
        x=cur if off==0 else nearest(rows,i-1,cur["ts"]+off*1000)
        for f in FEATURES:
            out[f"{f}@{off}"]=num(x.get(f)) if x else None
    # path descriptors
    for f in FEATURES:
        vals=[out.get(f"{f}@{o}") for o in OFFSETS]
        if vals[-1] is not None and vals[0] is not None:
            out[f"{f}_d20"]=vals[-1]-vals[0]
        if out.get(f"{f}@0") is not None and out.get(f"{f}@-5") is not None:
            out[f"{f}_d5"]=out[f"{f}@0"]-out[f"{f}@-5"]
        if out.get(f"{f}@0") is not None and out.get(f"{f}@-2") is not None:
            out[f"{f}_d2"]=out[f"{f}@0"]-out[f"{f}@-2"]
    # specific sequential shapes
    r5=[out.get(f"r5@{o}") for o in OFFSETS]
    r1=[out.get(f"r1@{o}") for o in OFFSETS]
    bp=[out.get(f"buy_pressure@{o}") for o in OFFSETS]
    act=[out.get(f"activity@{o}") for o in OFFSETS]
    def sign(v): return 1 if v is not None and v>0 else (-1 if v is not None and v<0 else 0)
    out["r5_sign_changes"]=sum(1 for a,b in zip(r5,r5[1:]) if a is not None and b is not None and sign(a)!=sign(b))
    out["r1_sign_changes"]=sum(1 for a,b in zip(r1,r1[1:]) if a is not None and b is not None and sign(a)!=sign(b))
    out["flow_sign_changes"]=sum(1 for a,b in zip(bp,bp[1:]) if a is not None and b is not None and sign(a)!=sign(b))
    if r5[-1] is not None and r5[-2] is not None: out["r5_recovery_2s"]=r5[-1]-r5[-2]
    if r1[-1] is not None and r1[-2] is not None: out["r1_recovery_2s"]=r1[-1]-r1[-2]
    if bp[-1] is not None and bp[-2] is not None: out["flow_recovery_2s"]=bp[-1]-bp[-2]
    if act[-1] is not None and act[-2] is not None: out["activity_change_2s"]=act[-1]-act[-2]
    return out

def build(cx):
    max_id=int(cx.execute("SELECT COALESCE(MAX(id),0) FROM feature_samples").fetchone()[0] or 0)
    min_id=max(0,max_id-MAX_RAW_ROWS+1)
    syms=[x[0] for x in cx.execute("SELECT DISTINCT symbol FROM feature_samples WHERE id>=? ORDER BY symbol",(min_id,))]
    ds=[];raw=0
    for sym in syms:
        rows=load(cx,sym,min_id);raw+=len(rows);last=-10**18
        for i,cur in enumerate(rows):
            if cur["ts"]-last<ANCHOR_GAP_MS:continue
            y=first_touch(rows,i)
            if y is None:continue
            tr=trajectory(rows,i)
            enough=sum(1 for f in FEATURES for o in OFFSETS if tr.get(f"{f}@{o}") is not None)
            if enough < len(FEATURES)*4:continue
            tr.update({"symbol":sym,"ts":cur["ts"],"label":y})
            ds.append(tr);last=cur["ts"]
    ds.sort(key=lambda x:x["ts"])
    return ds,raw,len(syms)

def feature_names(rows):
    skip={"symbol","ts","label"}
    s=set()
    for r in rows:
        for k,v in r.items():
            if k not in skip and num(v) is not None:s.add(k)
    return sorted(s)

def scales(rows,fs):
    out={}
    for f in fs:
        vals=[num(r.get(f)) for r in rows];vals=[v for v in vals if v is not None]
        out[f]=(median(vals) or 0.0,mad(vals))
    return out

def vec(r,fs,sc):
    z=[]
    for f in fs:
        v=num(r.get(f));m,d=sc[f]
        z.append(0.0 if v is None else max(-8,min(8,(v-m)/d)))
    return z

def dist(a,b):
    return sum((x-y)*(x-y) for x,y in zip(a,b))/max(1,len(a))

def farthest_init(vs,k):
    centers=[0]
    while len(centers)<min(k,len(vs)):
        best_i=None;best_d=-1
        for i,v in enumerate(vs):
            if i in centers:continue
            d=min(dist(v,vs[c]) for c in centers)
            if d>best_d:best_d=d;best_i=i
        centers.append(best_i)
    return centers

def kmedoids(rows,fs,sc,k=K,iters=6):
    vs=[vec(r,fs,sc) for r in rows]
    med=farthest_init(vs,k)
    for _ in range(iters):
        groups=[[] for _ in med]
        for i,v in enumerate(vs):
            g=min(range(len(med)),key=lambda j:dist(v,vs[med[j]]));groups[g].append(i)
        new=[]
        for inds in groups:
            if not inds:new.append(med[len(new)]);continue
            sample=inds if len(inds)<=80 else inds[::max(1,len(inds)//80)]
            best=min(sample,key=lambda c:sum(dist(vs[c],vs[j]) for j in sample))
            new.append(best)
        if new==med:break
        med=new
    groups=[[] for _ in med]
    for i,v in enumerate(vs):
        g=min(range(len(med)),key=lambda j:dist(v,vs[med[j]]));groups[g].append(i)
    return [{"medoid":rows[m],"center_vec":vs[m],"members":[rows[i] for i in inds]} for m,inds in zip(med,groups)]

def coarse_features():
    # context matching: same broad market regime at t0, not the full trajectory
    return ["activity@0","t5@0","t1@0","rv@0","spread@0","depth_min@0"]

def matched_losses(wins,losses,sc,n_per=4):
    cfs=coarse_features()
    def cv(r):
        z=[]
        for f in cfs:
            m,d=sc.get(f,(0,1));v=num(r.get(f));z.append(0 if v is None else (v-m)/d)
        return z
    lv=[(x,cv(x)) for x in losses]
    out=[]
    for w in wins:
        wv=cv(w)
        nearests=sorted(lv,key=lambda q:dist(wv,q[1]))[:n_per]
        out.extend(x for x,_ in nearests)
    # unique by symbol+ts
    d={(x["symbol"],x["ts"]):x for x in out}
    return list(d.values())

def contrasts(wins,losses,fs):
    out=[]
    for f in fs:
        w=[num(r.get(f)) for r in wins];w=[x for x in w if x is not None]
        l=[num(r.get(f)) for r in losses];l=[x for x in l if x is not None]
        if len(w)<5 or len(l)<10:continue
        mw=median(w);ml=median(l);scale=(mad(w)+mad(l))/2
        gap=(mw-ml)/(scale if scale>1e-9 else 1)
        out.append({"feature":f,"win_median":mw,"matched_loss_median":ml,"robust_gap":gap,
                    "win_n":len(w),"loss_n":len(l)})
    out.sort(key=lambda x:abs(x["robust_gap"]),reverse=True)
    return out

def threshold_rules(wins,losses,fs):
    allrows=[(r,1) for r in wins]+[(r,0) for r in losses]
    rules=[]
    for f in fs:
        vals=[num(r.get(f)) for r,_ in allrows];vals=[v for v in vals if v is not None]
        if len(vals)<30:continue
        for q in (.2,.35,.5,.65,.8):
            th=sorted(vals)[int((len(vals)-1)*q)]
            for op in (">=","<="):
                tp=fp=0
                for r,y in allrows:
                    v=num(r.get(f))
                    p=False if v is None else (v>=th if op==">=" else v<=th)
                    if p and y:tp+=1
                    elif p and not y:fp+=1
                if tp<4:continue
                prec=tp/(tp+fp) if tp+fp else 0
                rec=tp/len(wins) if wins else 0
                rules.append({"feature":f,"op":op,"threshold":th,"precision":prec,"recall":rec,"tp":tp,"fp":fp})
    rules.sort(key=lambda x:(x["precision"],x["tp"],x["recall"]),reverse=True)
    return rules

def apply_rule(r,x):
    v=num(r.get(x["feature"]))
    return False if v is None else (v>=x["threshold"] if x["op"]==">=" else v<=x["threshold"])

def signature_from_rules(rules):
    # greedily select up to 3 different families with best precision
    chosen=[];seen=set()
    for x in rules:
        fam=x["feature"].split("@")[0].split("_")[0]
        if fam in seen:continue
        chosen.append(x);seen.add(fam)
        if len(chosen)>=3:break
    return chosen

def eval_signature(rows,center_vec,cluster_fs,sc,signature,dist_limit):
    out=[]
    for r in rows:
        d=dist(vec(r,cluster_fs,sc),center_vec)
        if d>dist_limit:continue
        if all(apply_rule(r,x) for x in signature):out.append(r)
    wins=sum(1 for r in out if r["label"]=="WIN")
    losses=len(out)-wins
    return {"signals":len(out),"wins":wins,"losses":losses,
            "precision":round(wins/len(out),6) if out else 0}

def run(db):
    cx=sqlite3.connect(str(db));ds,raw,nsym=build(cx);cx.close()
    n=len(ds);a=int(n*.60);b=int(n*.80)
    dev,val,test=ds[:a],ds[a:b],ds[b:]
    cluster_fs=[f"{f}@{o}" for f in FEATURES for o in OFFSETS]
    sc=scales(dev,feature_names(dev))
    dev_w=[r for r in dev if r["label"]=="WIN"]
    dev_l=[r for r in dev if r["label"]=="LOSS"]
    clusters=kmedoids(dev_w,cluster_fs,sc,K)
    archetypes=[]
    for idx,c in enumerate(clusters):
        wins=c["members"]
        losses=matched_losses(wins,dev_l,sc,4)
        cts=contrasts(wins,losses,feature_names(dev))
        rules=threshold_rules(wins,losses,[x["feature"] for x in cts[:30]])
        sig=signature_from_rules(rules)
        # radius = 80th percentile winner distance to medoid
        ds_w=sorted(dist(vec(r,cluster_fs,sc),c["center_vec"]) for r in wins)
        radius=ds_w[min(len(ds_w)-1,int(.8*(len(ds_w)-1)))] if ds_w else 999
        val_eval=eval_signature(val,c["center_vec"],cluster_fs,sc,sig,radius)
        test_eval=eval_signature(test,c["center_vec"],cluster_fs,sc,sig,radius)
        archetypes.append({
          "id":idx+1,"dev_wins":len(wins),"matched_dev_losses":len(losses),
          "medoid_symbol":c["medoid"]["symbol"],"medoid_ts":c["medoid"]["ts"],
          "radius_p80":radius,
          "top_sequence_differences":cts[:15],
          "signature_rules":sig,
          "validation":val_eval,"blind_test":test_eval
        })
    archetypes.sort(key=lambda x:(x["blind_test"]["precision"],x["blind_test"]["wins"],x["validation"]["precision"]),reverse=True)
    total_test_signals=sum(x["blind_test"]["signals"] for x in archetypes)
    total_test_wins=sum(x["blind_test"]["wins"] for x in archetypes)
    total_test_losses=sum(x["blind_test"]["losses"] for x in archetypes)
    report={
      "status":"BBB_BYBIT_WIN_DNA_V4_TRAJECTORY_ARCHETYPES",
      "raw_rows":raw,"symbols":nsym,"episodes":n,
      "split":{"discovery":len(dev),"validation":len(val),"blind_test":len(test)},
      "wins":{"discovery":len(dev_w),"validation":sum(r["label"]=="WIN" for r in val),
              "blind_test":sum(r["label"]=="WIN" for r in test)},
      "offsets_sec":OFFSETS,"trajectory_features":FEATURES,
      "archetypes":archetypes,
      "combined_blind":{"signals":total_test_signals,"wins":total_test_wins,"losses":total_test_losses,
                        "precision":round(total_test_wins/total_test_signals,6) if total_test_signals else 0},
      "decision":"RESEARCH_COMPLETE_DO_NOT_TRADE_AUTOMATICALLY"
    }
    out=Path(db).parent/"bbb_win_dna_v4_archetypes.json"
    out.write_text(json.dumps(report,indent=2),encoding="utf8")
    return report

if __name__=="__main__":
    db=Path(os.getenv("DATA_DIR",Path(__file__).resolve().parent/"data"))/"bbb.sqlite3"
    print(json.dumps(run(db),separators=(",",":")))
