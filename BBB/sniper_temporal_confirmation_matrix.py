#!/usr/bin/env python3
import sqlite3,json,statistics,itertools,math

DB="/data/bbb.sqlite3"
cx=sqlite3.connect(DB); cx.row_factory=sqlite3.Row

row=cx.execute("""SELECT session_id,COUNT(*) n FROM trades
WHERE status='CLOSED' AND session_id LIKE 'PAPER-BBB-%'
GROUP BY session_id ORDER BY n DESC,MAX(closed_ms) DESC LIMIT 1""").fetchone()
if not row: raise SystemExit("No CLOSED PAPER trades")
sid=row["session_id"]

trs=cx.execute("""SELECT position_id,symbol,side,opened_ms,net_pnl_usd,exit_reason
FROM trades WHERE session_id=? AND status='CLOSED' ORDER BY opened_ms""",(sid,)).fetchall()

def side_sign(s): return 1.0 if s=="BUY" else -1.0
def is_tail(r): return str(r["exit_reason"] or "").startswith("RIGHT_TAIL")
def is_win(r): return float(r["net_pnl_usd"] or 0)>0

def nearest(symbol,ts,window=350):
    return cx.execute("""SELECT ts_ms,buy_ratio_1s,book_imbalance_5,ret_250ms_bps,ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,spread_bps
      FROM feature_samples WHERE symbol=? AND ts_ms BETWEEN ? AND ?
      ORDER BY ABS(ts_ms-?) LIMIT 1""",(symbol,ts-window,ts+window,ts)).fetchone()

OFFSETS=(250,500,750,1000,1250,1500,2000,2500,3000)
D=[]
for t in trs:
    sg=side_sign(t["side"]); snaps=[]
    for off in OFFSETS:
        s=nearest(t["symbol"],int(t["opened_ms"])+off)
        if not s: continue
        flow=(float(s["buy_ratio_1s"] or .5)-.5)*2*sg
        imb=float(s["book_imbalance_5"] or 0)*sg
        r250=sg*float(s["ret_250ms_bps"] or 0)
        r1=sg*float(s["ret_1s_bps"] or 0)
        r5=sg*float(s["ret_5s_bps"] or 0)
        votes=sum([r250>0,r1>0,r5>0,flow>-0.05,imb>-0.20])
        strong=sum([r250>0,r1>0,flow>0,imb>0])
        snaps.append(dict(off=off,votes=votes,strong=strong,r250=r250,r1=r1,r5=r5,flow=flow,imb=imb,spread=float(s["spread_bps"] or 0),rv=float(s["realized_vol_5s_bps"] or 0)))
    if len(snaps)<4: continue
    D.append(dict(pid=t["position_id"],symbol=t["symbol"],side=t["side"],net=float(t["net_pnl_usd"] or 0),tail=is_tail(t),win=is_win(t),snaps=snaps))

print("SESSION",sid,"TRADES",len(trs),"USABLE",len(D))

def metric(x,horizon):
    z=[s for s in x["snaps"] if s["off"]<=horizon]
    if not z:return None
    return dict(
      n=len(z),
      good3=sum(s["votes"]>=3 for s in z),
      good4=sum(s["votes"]>=4 for s in z),
      strong3=sum(s["strong"]>=3 for s in z),
      last_votes=z[-1]["votes"],
      min_votes=min(s["votes"] for s in z),
      avg_votes=sum(s["votes"] for s in z)/len(z),
      pos_r1=sum(s["r1"]>0 for s in z),
      pos_flow=sum(s["flow"]>0 for s in z),
      pos_imb=sum(s["imb"]>0 for s in z),
    )

def stats(xs):
    n=len(xs); gp=sum(max(0,x["net"]) for x in xs); gl=-sum(min(0,x["net"]) for x in xs)
    eq=pk=dd=0.0
    for x in xs:
        eq+=x["net"]; pk=max(pk,eq); dd=max(dd,pk-eq)
    return dict(N=n,WR=100*sum(x["win"] for x in xs)/n if n else 0,TAIL=100*sum(x["tail"] for x in xs)/n if n else 0,
                NET=sum(x["net"] for x in xs),EXP=statistics.mean([x["net"] for x in xs]) if xs else 0,
                PF=gp/gl if gl else 999.0,DD=dd)

def folds():
    n=len(D)
    for a,b in ((.4,.6),(.6,.8),(.8,1.0)):
        yield D[int(n*a):int(n*b)],f"{int(a*100)}-{int(b*100)}"

rules=[]
for h in (750,1000,1500,2000,2500,3000):
  for k in (2,3,4):
    rules.append((f"h={h} good3>={k}",lambda x,h=h,k=k:(lambda m:m and m["good3"]>=k)(metric(x,h))))
    rules.append((f"h={h} good4>={k}",lambda x,h=h,k=k:(lambda m:m and m["good4"]>=k)(metric(x,h))))
    rules.append((f"h={h} strong3>={k}",lambda x,h=h,k=k:(lambda m:m and m["strong3"]>=k)(metric(x,h))))
  for v in (3,4,5):
    rules.append((f"h={h} last_votes>={v}",lambda x,h=h,v=v:(lambda m:m and m["last_votes"]>=v)(metric(x,h))))
  for av in (3.0,3.5,4.0):
    rules.append((f"h={h} avg_votes>={av}",lambda x,h=h,av=av:(lambda m:m and m["avg_votes"]>=av)(metric(x,h))))

out=[]
for name,p in rules:
    acc=[x for x in D if p(x)]; s=stats(acc)
    rate=s["N"]/len(D) if D else 0
    if s["N"]<15 or rate>0.25 or s["NET"]<=0: continue
    fr=[]; ok=True
    for te,label in folds():
        z=[x for x in te if p(x)]; q=stats(z)
        if q["N"]<4 or q["NET"]<=0: ok=False
        fr.append((label,q))
    if ok: out.append((s["TAIL"],s["PF"],s["EXP"],s["WR"],-s["DD"],name,p,s,fr))

# pairwise AND among survivors
seed=sorted(out,reverse=True)[:20]
for (_,_,_,_,_,n1,p1,_,_),(_,_,_,_,_,n2,p2,_,_) in itertools.combinations(seed,2):
    name=f"({n1}) AND ({n2})"; p=lambda x,p1=p1,p2=p2:p1(x) and p2(x)
    acc=[x for x in D if p(x)]; s=stats(acc); rate=s["N"]/len(D) if D else 0
    if s["N"]<12 or rate>0.20 or s["NET"]<=0: continue
    fr=[];ok=True
    for te,label in folds():
        z=[x for x in te if p(x)];q=stats(z)
        if q["N"]<4 or q["NET"]<=0:ok=False
        fr.append((label,q))
    if ok:out.append((s["TAIL"],s["PF"],s["EXP"],s["WR"],-s["DD"],name,p,s,fr))

out=sorted(out,reverse=True)
print("=== TOP TEMPORAL SNIPER POLICIES ===")
for r in out[:25]:
    s=r[7]
    print("\nPOLICY",r[5],"N",s["N"],"RATE",round(100*s["N"]/len(D),1),"WR",round(s["WR"],1),"TAIL",round(s["TAIL"],1),"NET",round(s["NET"],4),"EXP",round(s["EXP"],5),"PF",round(s["PF"],2),"DD",round(s["DD"],4))
    for label,q in r[8]:
        print(" ",label,"N",q["N"],"WR",round(q["WR"],1),"TAIL",round(q["TAIL"],1),"NET",round(q["NET"],4),"PF",round(q["PF"],2))
print("=== BEST ===")
if out: print(out[0][5],out[0][7])
else: print("NONE")
