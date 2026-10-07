#!/usr/bin/env python3
import sqlite3,json,statistics,itertools

DB="/data/bbb.sqlite3"
cx=sqlite3.connect(DB); cx.row_factory=sqlite3.Row
sid=cx.execute("SELECT value FROM meta WHERE key='session_id'").fetchone()[0]
rs=cx.execute("""SELECT side,net_pnl_usd,entry_fee_usd,exit_fee_usd,exit_reason,closed_ms,entry_features_json
FROM trades WHERE session_id=? AND status='CLOSED' ORDER BY closed_ms""",(sid,)).fetchall()

def J(s):
    try:return json.loads(s or "{}")
    except:return {}

D=[]
for r in rs:
    f=J(r["entry_features_json"])
    D.append({
      "side":r["side"],"net":float(r["net_pnl_usd"] or 0),
      "fees":float(r["entry_fee_usd"] or 0)+float(r["exit_fee_usd"] or 0),
      "reason":str(r["exit_reason"] or ""),
      "bucket":str(f.get("vol_bucket","?")),"regime":str(f.get("regime","?")),
      "tier":str(f.get("sizing_tier","?")),
      "winner_score":float(f.get("winner_score",f.get("confidence",0)) or 0),
      "loser_risk":float(f.get("loser_risk",0) or 0),
      "sizing_score":float(f.get("sizing_score",0) or 0),
      "edge_ratio":float(f.get("edge_ratio",0) or 0),
      "proj":float(f.get("projected_edge_bps",0) or 0),
      "req":float(f.get("required_cost_edge_bps",0) or 0),
      "spread":float(f.get("spread_bps",0) or 0),
      "rv5":float(f.get("realized_vol_5s_bps",0) or 0),
      "ret250":float(f.get("ret_250ms_bps",0) or 0),
      "ret1":float(f.get("ret_1s_bps",0) or 0),
      "ret5":float(f.get("ret_5s_bps",0) or 0),
      "imb":float(f.get("book_imbalance_5",0) or 0),
      "aligned":int(f.get("aligned_count",0) or 0),
      "opposed":int(f.get("opposing_count",0) or 0),
    })

def tail(x):return x["reason"].startswith("RIGHT_TAIL")
def S(xs):
    n=len(xs); gp=sum(max(0,x["net"]) for x in xs); gl=-sum(min(0,x["net"]) for x in xs)
    eq=pk=dd=0.0
    for x in xs:
        eq+=x["net"]; pk=max(pk,eq); dd=max(dd,pk-eq)
    return {"N":n,"WR":100*sum(x["net"]>0 for x in xs)/n if n else 0,
            "TAIL":100*sum(tail(x) for x in xs)/n if n else 0,
            "NET":sum(x["net"] for x in xs),
            "EXP":statistics.mean([x["net"] for x in xs]) if xs else 0,
            "PF":gp/gl if gl else 999.0,"DD":dd}

conds=[]
for k in ("bucket","regime","tier","side"):
    for v in sorted(set(x[k] for x in D)):
        conds.append((f"{k}=={v}",lambda x,k=k,v=v:x[k]==v))
conds += [("opposed==0",lambda x:x["opposed"]==0),("aligned>=4",lambda x:x["aligned"]>=4)]
for feat,hi in [("winner_score",1),("sizing_score",1),("edge_ratio",1),("proj",1),("loser_risk",0),("spread",0),("rv5",1),("ret250",1),("ret1",1),("ret5",1),("imb",1)]:
    vals=sorted(x[feat] for x in D); n=len(vals)
    if n<20: continue
    for q in (.5,.6,.7,.75,.8,.85,.9,.93,.95):
        th=vals[int((n-1)*(q if hi else 1-q))]
        if hi:
            conds.append((f"{feat}>={th:.4g}",lambda x,feat=feat,th=th:x[feat]>=th))
        else:
            conds.append((f"{feat}<={th:.4g}",lambda x,feat=feat,th=th:x[feat]<=th))

n=len(D)
folds=[]
for a,b in ((.4,.6),(.6,.8),(.8,1.0)):
    folds.append(D[int(n*a):int(n*b)])

def valid(name,p):
    z=[x for x in D if p(x)]; s=S(z)
    if s["N"]<12 or s["N"]>0.25*n or s["NET"]<=0:return None
    fr=[]
    for te in folds:
        q=S([x for x in te if p(x)])
        if q["N"]<4 or q["NET"]<=0:return None
        fr.append(q)
    return (s["TAIL"],s["PF"],s["EXP"],s["WR"],-s["DD"],name,p,s,fr)

sing=[]
for name,p in conds:
    r=valid(name,p)
    if r:sing.append(r)
seeds=[(r[5],r[6]) for r in sorted(sing,reverse=True)[:30]]
out=sing[:]
for (n1,p1),(n2,p2) in itertools.combinations(seeds,2):
    r=valid(f"({n1}) AND ({n2})",lambda x,p1=p1,p2=p2:p1(x) and p2(x))
    if r:out.append(r)
out=sorted(out,reverse=True)

print("SESSION",sid,"BASE",S(D))
print("=== TOP SNIPER POLICIES ===")
for r in out[:20]:
    s=r[7]
    print("\nPOLICY",r[5],"N",s["N"],"RATE",round(100*s["N"]/n,1),"WR",round(s["WR"],1),
          "TAIL",round(s["TAIL"],1),"NET",round(s["NET"],4),"EXP",round(s["EXP"],5),
          "PF",round(s["PF"],2),"DD",round(s["DD"],4))
    for i,q in enumerate(r[8],1):
        print(" FOLD",i,"N",q["N"],"WR",round(q["WR"],1),"TAIL",round(q["TAIL"],1),
              "NET",round(q["NET"],4),"PF",round(q["PF"],2),"DD",round(q["DD"],4))
print("=== BEST ===")
print(out[0][5],out[0][7] if out else "NONE")
