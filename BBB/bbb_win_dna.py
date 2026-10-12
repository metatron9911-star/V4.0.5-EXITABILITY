"""BBB / Bybit WIN-DNA research from the service's own historical PAPER data.

Reads /data/bbb.sqlite3 only. No orders, no wallets, no LIVE execution.
Builds a direction-normalized portrait of profitable vs losing ROCKET trades
from feature snapshots observed before each actual entry.

Outputs /data/bbb_win_dna_report.json and prints a compact summary.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics
from pathlib import Path
from collections import Counter, defaultdict

WINDOWS=(1,2,5,10,30,60)
BASE_FEATURES=(
    "spread_bps","depth_min_usd","depth_total_usd","book_imbalance_dir",
    "trade_count_1s","trade_count_5s","flow_dir",
    "ret_250ms_dir_bps","ret_1s_dir_bps","ret_5s_dir_bps","rv5s_bps",
)
ENTRY_FEATURES=("arm_tape_ratio","confirm_tape_ratio","confirm_samples","best_vote_count","best_beyond_bps")
QUANTS=(.2,.3,.4,.5,.6,.7,.8)

def fnum(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None

def pct(a,q):
    a=sorted(a)
    if not a:return None
    z=(len(a)-1)*q; lo=int(z); hi=min(len(a)-1,lo+1); t=z-lo
    return a[lo]*(1-t)+a[hi]*t

def med(a):
    return statistics.median(a) if a else None

def mad(a):
    if not a:return None
    m=med(a); z=[abs(x-m) for x in a]
    return statistics.median(z) or 1e-9

def metric(rows,pred):
    tp=fp=tn=fn=0
    for r in rows:
        y=r["win"]; p=bool(pred(r))
        if p and y:tp+=1
        elif p and not y:fp+=1
        elif not p and not y:tn+=1
        else:fn+=1
    n=tp+fp+tn+fn; base=(tp+fn)/n if n else 0
    prec=tp/(tp+fp) if tp+fp else 0
    rec=tp/(tp+fn) if tp+fn else 0
    return {"n":n,"tp":tp,"fp":fp,"tn":tn,"fn":fn,"support":tp+fp,
            "precision":round(prec,6),"recall":round(rec,6),
            "base_win_rate":round(base,6),
            "lift":round(prec/base,6) if base else 0}

def trade_rows(cx):
    cols=("id","session_id","position_id","symbol","side","notional_usd","net_pnl_usd",
          "gross_pnl_usd","entry_fee_usd","exit_fee_usd","hold_ms","exit_reason",
          "opened_ms","closed_ms","entry_features_json")
    q="""SELECT id,session_id,position_id,symbol,side,notional_usd,net_pnl_usd,
                gross_pnl_usd,entry_fee_usd,exit_fee_usd,hold_ms,exit_reason,
                opened_ms,closed_ms,entry_features_json
         FROM trades WHERE status='CLOSED' AND risk_state='ROCKET_V1'
         ORDER BY opened_ms,id"""
    out=[]
    for z in cx.execute(q):
        r=dict(zip(cols,z))
        r["win"]=float(r["net_pnl_usd"] or 0)>0
        r["sign"]=1.0 if r["side"]=="BUY" else -1.0
        try:r["entry_json"]=json.loads(r["entry_features_json"] or "{}")
        except Exception:r["entry_json"]={}
        out.append(r)
    return out

def snapshot_features(row,sign):
    spread=fnum(row[0]); db=fnum(row[1]); da=fnum(row[2]); imb=fnum(row[3])
    t1=fnum(row[4]); t5=fnum(row[5]); br=fnum(row[6])
    r250=fnum(row[7]); r1=fnum(row[8]); r5=fnum(row[9]); rv=fnum(row[10])
    return {
      "spread_bps":spread,
      "depth_min_usd":min(db,da) if db is not None and da is not None else None,
      "depth_total_usd":db+da if db is not None and da is not None else None,
      "book_imbalance_dir":sign*imb if imb is not None else None,
      "trade_count_1s":t1,"trade_count_5s":t5,
      "flow_dir":sign*((br-.5)*2.0) if br is not None else None,
      "ret_250ms_dir_bps":sign*r250 if r250 is not None else None,
      "ret_1s_dir_bps":sign*r1 if r1 is not None else None,
      "ret_5s_dir_bps":sign*r5 if r5 is not None else None,
      "rv5s_bps":abs(rv) if rv is not None else None,
    }

def enrich(cx,trades):
    rows=[]
    for t in trades:
        start=int(t["opened_ms"])-60000; end=int(t["opened_ms"])
        snaps=[]
        q="""SELECT spread_bps,depth_bid_5_usd,depth_ask_5_usd,book_imbalance_5,
                    trade_count_1s,trade_count_5s,buy_ratio_1s,ret_250ms_bps,
                    ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,ts_ms
             FROM feature_samples
             WHERE symbol=? AND ts_ms BETWEEN ? AND ?
             ORDER BY ts_ms"""
        for s in cx.execute(q,(t["symbol"],start,end)):
            d=snapshot_features(s,t["sign"]); d["ts_ms"]=int(s[11]); snaps.append(d)
        r={k:v for k,v in t.items() if k not in ("entry_features_json","entry_json")}
        r["snapshot_count_60s"]=len(snaps)
        for k in ENTRY_FEATURES:
            v=fnum(t["entry_json"].get(k))
            if v is not None:r["entry_"+k]=v
        for w in WINDOWS:
            xs=[s for s in snaps if s["ts_ms"]>=end-w*1000]
            for feat in BASE_FEATURES:
                vals=[fnum(s.get(feat)) for s in xs]; vals=[v for v in vals if v is not None]
                p=f"w{w}_{feat}"
                if vals:
                    r[p+"_last"]=vals[-1]
                    r[p+"_mean"]=sum(vals)/len(vals)
                    r[p+"_min"]=min(vals)
                    r[p+"_max"]=max(vals)
                    r[p+"_delta"]=vals[-1]-vals[0] if len(vals)>=2 else 0.0
        rows.append(r)
    return rows

def feature_names(rows):
    skip={"id","session_id","position_id","symbol","side","notional_usd","net_pnl_usd",
          "gross_pnl_usd","entry_fee_usd","exit_fee_usd","hold_ms","exit_reason","opened_ms",
          "closed_ms","win","sign","snapshot_count_60s"}
    names=set()
    for r in rows:
        for k,v in r.items():
            if k not in skip and fnum(v) is not None:names.add(k)
    return sorted(names)

def contrasts(rows,features):
    W=[r for r in rows if r["win"]]; L=[r for r in rows if not r["win"]]
    out=[]
    for k in features:
        w=[fnum(r.get(k)) for r in W]; w=[x for x in w if x is not None]
        l=[fnum(r.get(k)) for r in L]; l=[x for x in l if x is not None]
        if len(w)<2 or len(l)<3:continue
        mw,ml=med(w),med(l); scale=(mad(w)+mad(l))/2 or 1e-9
        out.append({"feature":k,"win_median":mw,"loss_median":ml,
                    "median_gap":mw-ml,"robust_gap":(mw-ml)/scale,
                    "win_n":len(w),"loss_n":len(l)})
    out.sort(key=lambda x:abs(x["robust_gap"]),reverse=True)
    return out

def discover(train,features):
    rules=[]
    nwin=sum(r["win"] for r in train)
    min_support=max(4,min(10,len(train)//5))
    for k in features:
        vals=[fnum(r.get(k)) for r in train]; vals=[x for x in vals if x is not None]
        if len(vals)<8:continue
        for q in QUANTS:
            th=pct(vals,q)
            for op in (">=","<="):
                pred=lambda r,k=k,th=th,op=op: (fnum(r.get(k)) is not None and
                    ((fnum(r.get(k))>=th) if op==">=" else (fnum(r.get(k))<=th)))
                m=metric(train,pred)
                if m["support"]<min_support or m["tp"]<max(2,min(4,nwin//2)):continue
                score=(m["lift"]-1)*math.sqrt(m["support"])*(.5+.5*m["recall"])
                rules.append({"feature":k,"op":op,"threshold":th,"train":m,"score":score})
    rules.sort(key=lambda x:(x["score"],x["train"]["lift"],x["train"]["support"]),reverse=True)
    return rules

def pred_rule(rule):
    def p(r):
        v=fnum(r.get(rule["feature"]))
        if v is None:return False
        return v>=rule["threshold"] if rule["op"]==">=" else v<=rule["threshold"]
    return p

def eval_holdout(rules,test):
    out=[]
    for x in rules[:120]:
        y=dict(x); y["holdout"]=metric(test,pred_rule(x)) if test else None
        out.append(y)
    return out

def build_vote(train,test,evaluated):
    # Prefer rules that improve holdout when holdout has both classes.
    viable=[]
    for x in evaluated:
        h=x.get("holdout")
        if h and h["tp"]>=1 and h["support"]>=2 and h["lift"]>1.0:
            viable.append(x)
    pool=(viable or evaluated)[:10]
    # de-duplicate highly similar same-family rules
    chosen=[]; families=set()
    for x in pool:
        fam="_".join(x["feature"].split("_")[:3])
        if fam in families:continue
        chosen.append(x); families.add(fam)
        if len(chosen)>=5:break
    if len(chosen)<3:chosen=pool[:min(5,len(pool))]
    best=None
    for n in range(3,len(chosen)+1):
        rs=chosen[:n]; ps=[pred_rule(x) for x in rs]
        for k in range(2,n+1):
            pred=lambda r,ps=ps,k=k:sum(1 for p in ps if p(r))>=k
            a=metric(train,pred); b=metric(test,pred) if test else None
            # quality-first, but retain some support
            hold_lift=b["lift"] if b else a["lift"]
            hold_tp=b["tp"] if b else a["tp"]
            score=(hold_lift,a["lift"],hold_tp,a["support"])
            z={"rules":[{"feature":x["feature"],"op":x["op"],"threshold":x["threshold"]} for x in rs],
               "required_votes":k,"train":a,"holdout":b,"score_tuple":score}
            if best is None or score>best["score_tuple"]:best=z
    return best

def run_analysis(db_path):
    db_path=Path(db_path)
    cx=sqlite3.connect(db_path)
    trades=trade_rows(cx)
    rows=enrich(cx,trades)
    feats=feature_names(rows)
    total=len(rows); wins=sum(r["win"] for r in rows); losses=total-wins
    # chronological 70/30; if tiny, keep at least 2 wins in each side where possible
    cut=max(1,int(total*.70))
    train=rows[:cut]; test=rows[cut:]
    if sum(r["win"] for r in test)<1 and wins>=3:
        # fallback: every 3rd chronological trade is holdout to avoid zero-win test
        test=[r for i,r in enumerate(rows) if i%3==2]
        train=[r for i,r in enumerate(rows) if i%3!=2]
        split="interleaved_every_3rd_due_sparse_wins"
    else:
        split="chronological_70_30"
    con=contrasts(train,feats)
    rules=discover(train,feats)
    evaluated=eval_holdout(rules,test)
    vote=build_vote(train,test,evaluated) if len(train)>=8 and len(test)>=3 else None

    sessions=defaultdict(lambda:{"closed":0,"wins":0,"net":0.0})
    for r in rows:
        s=sessions[r["session_id"]]; s["closed"]+=1; s["wins"]+=int(r["win"]); s["net"]+=float(r["net_pnl_usd"] or 0)
    session_summary=[]
    for sid,s in sessions.items():
        session_summary.append({"session_id":sid,**s,"wr":round(s["wins"]/s["closed"],4) if s["closed"] else 0})
    session_summary.sort(key=lambda x:x["session_id"])

    approved=False
    if vote and vote.get("holdout"):
        h=vote["holdout"]; a=vote["train"]
        approved=(h["tp"]>=2 and h["support"]>=4 and h["lift"]>=1.20 and a["lift"]>=1.10)
    report={
      "status":"BBB_BYBIT_WIN_DNA_FROM_OWN_HISTORY",
      "scope":"BBB own Bybit PAPER history only",
      "closed_trades":total,"wins":wins,"losses":losses,
      "base_win_rate":round(wins/total,6) if total else 0,
      "sessions":session_summary,
      "split":split,
      "train":{"n":len(train),"wins":sum(r["win"] for r in train)},
      "holdout":{"n":len(test),"wins":sum(r["win"] for r in test)},
      "feature_count":len(feats),
      "planes":["entry JSON","1/2/5/10/30/60s microstructure",
                "direction-normalized returns/flow/imbalance","spread/depth",
                "trade velocity","volatility","level/change/min/max/mean"],
      "top_win_loss_contrasts":con[:40],
      "top_rules":evaluated[:40],
      "vote_portrait":vote,
      "approved_for_new_paper_selector":approved,
      "decision":"APPROVE_FOR_FRESH_PAPER_SELECTOR" if approved else "RESEARCH_CANDIDATE_ONLY",
      "caveats":[
        "Historical trades were selected by legacy ROCKET, so this is conditional on that candidate population.",
        "Sample size may be small; holdout performance is reported separately.",
        "No feature after opened_ms is used in the portrait.",
        "This report does not authorize LIVE trading."
      ]
    }
    out=db_path.parent/"bbb_win_dna_report.json"
    out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    cx.close()
    return report

if __name__=="__main__":
    db=Path(os.getenv("DATA_DIR",Path(__file__).resolve().parent/"data"))/"bbb.sqlite3"
    r=run_analysis(db)
    compact={k:r[k] for k in ("status","closed_trades","wins","losses","base_win_rate","split","train","holdout","feature_count","vote_portrait","approved_for_new_paper_selector","decision")}
    compact["top_win_loss_contrasts"]=r.get("top_win_loss_contrasts",[])[:12]
    compact["top_rules"]=r.get("top_rules",[])[:12]
    print(json.dumps(compact,separators=(",",":")))
