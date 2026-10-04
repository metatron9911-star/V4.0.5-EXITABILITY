#!/usr/bin/env python3
import json, sqlite3, statistics
from strategy_cells import (
    POLICIES, classify_tail_accel_delta, tail_milestones_hit,
    tail_active_floor_bps, tail_trail_bps, tail_trail_bps_binding,
    tail_ratchet_stop_bps, tail_timeout_ms,
)

DB="/data/bbb.sqlite3"
CUTOFF=1791120262000
MAX_GAP_MS=5000

cx=sqlite3.connect(DB)
cx.row_factory=sqlite3.Row

pairs=[]
for r in cx.execute("SELECT ts_ms,payload_json FROM events WHERE ts_ms>? AND stage='B2_SHADOW_PAIR' ORDER BY ts_ms",(CUTOFF,)):
    p=json.loads(r["payload_json"])
    if p.get("impl")!="b2-shadow-v3":
        continue
    pairs.append((r["ts_ms"],p))

def arm_for(pid):
    rows=cx.execute("SELECT ts_ms,payload_json FROM events WHERE ts_ms>? AND stage='B2_SHADOW_ARM' AND payload_json LIKE ? ORDER BY ts_ms",(CUTOFF,"%"+pid+"%")).fetchall()
    for r in rows:
        p=json.loads(r["payload_json"])
        if p.get("position_id")==pid and p.get("impl")=="b2-shadow-v3":
            return r["ts_ms"],p
    return None,None

def trade_features(pid):
    r=cx.execute("SELECT entry_features_json FROM trades WHERE position_id=?",(pid,)).fetchone()
    if not r: return {}
    try:return json.loads(r[0] or "{}")
    except:return {}

def replay(pair):
    pid=pair["position_id"]; symbol=pair["symbol"]; side=pair["side"]; bucket=pair["bucket"]
    arm_ts,arm=arm_for(pid)
    if not arm:
        return {"position_id":pid,"symbol":symbol,"bucket":bucket,"status":"NO_ARM"}
    pol=POLICIES[(bucket,side)]
    ef=trade_features(pid)
    required=float(ef.get("required_cost_edge_bps",0) or 0)
    base_floor=max(0.18*float(pol.take_profit_bps),0.65*required)
    entry=float(arm["entry_vwap"])
    peak=float(arm.get("arm_mfe_bps",0) or 0)
    m_prev=float(arm.get("arm_momentum",0) or 0)
    accel="FLAT"; warm=0; decel_streak=0; stop=None
    hardcap=int(pol.max_hold_ms)*6
    rows=cx.execute("""SELECT ts_ms,mid,ret_250ms_bps,ret_1s_bps
                       FROM feature_samples
                       WHERE symbol=? AND ts_ms>? AND ts_ms<=?
                       ORDER BY ts_ms""",(symbol,arm_ts,arm_ts+hardcap+60000)).fetchall()
    prev_ts=arm_ts; max_gap=0; contaminated=False
    out=None
    sign=1.0 if side=="BUY" else -1.0
    for r in rows:
        ts=int(r["ts_ms"]); gap=ts-prev_ts; prev_ts=ts; max_gap=max(max_gap,gap)
        if gap>MAX_GAP_MS:
            contaminated=True
            break
        mid=float(r["mid"] or 0)
        if mid<=0: continue
        move=sign*(mid/entry-1.0)*10000.0
        peak=max(peak,move)
        m_now=sign*float(r["ret_1s_bps"] or 0)+0.5*sign*float(r["ret_250ms_bps"] or 0)
        raw=classify_tail_accel_delta(m_now,m_prev)
        if warm<3:
            accel="FLAT"; warm+=1; decel_streak=0
        else:
            if raw=="DECEL": decel_streak+=1
            else: decel_streak=0
            if raw=="ACCEL":
                accel="ACCEL"
            elif raw=="DECEL" and decel_streak>=2:
                accel="DECEL"
            else:
                accel=accel if accel in {"ACCEL","FLAT"} else "FLAT"
        m_prev=m_now
        tp=float(pol.take_profit_bps)
        hit=tail_milestones_hit(peak,tp)
        floor=tail_active_floor_bps(tp,base_floor,hit)
        raw_trail=tail_trail_bps(peak,tp,accel,bucket)
        trail=tail_trail_bps_binding(raw_trail,peak,floor,hit)
        cand,stop=tail_ratchet_stop_bps(stop,peak,trail,floor)
        since=ts-arm_ts
        timeout=tail_timeout_ms(int(pol.max_hold_ms),accel)
        reason=None
        if move<=-float(pol.stop_loss_bps):
            reason="SL"
        elif move<=stop:
            reason=accel+"_RATCHET"
        elif since>=timeout:
            reason="TIME_"+accel
        elif since>=hardcap:
            reason="HARD_CAP"
        if reason:
            out={"reason":reason,"move_bps_mid":move,"peak_bps":peak,"ratchet_stop_bps":stop,"exit_ts":ts}
            break
    old_b2=float(pair.get("b2_exit_move_bps") or 0)
    a=float(pair.get("a_exit_move_bps") or 0)
    d={
        "position_id":pid,"symbol":symbol,"bucket":bucket,
        "old_b2_delta_vs_a":old_b2-a,
        "a_exit_move_bps":a,
        "old_b2_move_bps":old_b2,
        "max_gap_ms":max_gap,
        "status":"CONTAMINATED_BY_FEED_GAP" if contaminated else ("OK" if out else "NO_EXIT"),
    }
    if out:
        d.update(out)
        d["replay_delta_vs_a"]=out["move_bps_mid"]-a
    return d

results=[replay(p) for _,p in pairs]
print("B2-v4 offline replay on historical v3.1 pairs")
print("NOTE: replay exit is MID-based, while A/B stored exits are VWAP-based; use for structural comparison, not exact pennies.")
print("CUTOFF",CUTOFF,"pairs",len(results),"max_gap_clean_ms",MAX_GAP_MS)
print()
for x in results:
    print(
      x.get("symbol"),x.get("bucket"),x.get("status"),
      "oldDelta=%.2f"%x.get("old_b2_delta_vs_a",0),
      ("v4="+x.get("reason","") if x.get("reason") else ""),
      ("v4Move=%.2f"%x["move_bps_mid"] if "move_bps_mid" in x else ""),
      ("v4Delta=%.2f"%x["replay_delta_vs_a"] if "replay_delta_vs_a" in x else ""),
      "gap=%d"%x.get("max_gap_ms",0),
      ("peak=%.2f stop=%.2f"%(x["peak_bps"],x["ratchet_stop_bps"]) if "peak_bps" in x else "")
    )
clean=[x for x in results if x.get("status")=="OK" and "replay_delta_vs_a" in x]
ds=[x["replay_delta_vs_a"] for x in clean]
print("\n--- CLEAN AGGREGATE ---")
print("N",len(clean),"wins",sum(d>0 for d in ds),"losses",sum(d<0 for d in ds),
      "mean",round(statistics.mean(ds),2) if ds else None,
      "median",round(statistics.median(ds),2) if ds else None,
      "delta_lt_-10",sum(d<-10 for d in ds))
for b in sorted(set(x["bucket"] for x in clean)):
    z=[x["replay_delta_vs_a"] for x in clean if x["bucket"]==b]
    print(b,"N",len(z),"mean",round(statistics.mean(z),2),"median",round(statistics.median(z),2))
print("\n--- OLD WORST CASES ---")
for x in sorted(results,key=lambda q:q.get("old_b2_delta_vs_a",999))[:5]:
    print(x.get("symbol"),x.get("bucket"),x.get("status"),
          "oldDelta=%.2f"%x.get("old_b2_delta_vs_a",0),
          ("v4Delta=%.2f"%x["replay_delta_vs_a"] if "replay_delta_vs_a" in x else "v4Delta=NA"),
          ("reason="+x.get("reason","") if x.get("reason") else ""),
          "gap=%d"%x.get("max_gap_ms",0))
