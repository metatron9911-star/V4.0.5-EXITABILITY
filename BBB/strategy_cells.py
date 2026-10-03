"""BBB VOL strategy cells H2.

Direction-aware entry scoring + winner/loser risk decomposition + progressive sizing.
PAPER only; no exchange execution path.
"""
from __future__ import annotations
from dataclasses import dataclass
import json

@dataclass(frozen=True)
class CellPolicy:
    cell: str
    head: str
    side: str
    gate: float
    margin_fraction: float
    max_open: int
    cooldown_ms: int
    take_profit_bps: float
    stop_loss_bps: float
    max_hold_ms: int
    max_spread_bps: float
    min_trades_1s: int
    cost_mult: float
    cost_buffer_bps: float
    allow_regimes: tuple[str,...]

POLICIES={
    ("CORE","BUY"):    CellPolicy("VOL-CORE-LONG","VOL","BUY",65.0,0.020,3,18_000,55.0,34.0,150_000,4.0,3,1.12,2.0,("TREND","IMPULSE")),
    ("CORE","SELL"):   CellPolicy("VOL-CORE-SHORT","VOL","SELL",65.0,0.020,3,18_000,55.0,34.0,150_000,4.0,3,1.12,2.0,("TREND","IMPULSE")),
    ("FAST","BUY"):    CellPolicy("VOL-FAST-LONG","VOL","BUY",67.0,0.015,3,22_000,85.0,50.0,150_000,10.0,1,1.15,3.0,("TREND","IMPULSE")),
    ("FAST","SELL"):   CellPolicy("VOL-FAST-SHORT","VOL","SELL",67.0,0.015,3,22_000,85.0,50.0,150_000,10.0,1,1.15,3.0,("TREND","IMPULSE")),
    ("EXTREME","BUY"): CellPolicy("VOL-EXTREME-LONG","VOL","BUY",70.0,0.010,2,30_000,130.0,75.0,120_000,18.0,1,1.20,5.0,("IMPULSE",)),
    ("EXTREME","SELL"):CellPolicy("VOL-EXTREME-SHORT","VOL","SELL",70.0,0.010,2,30_000,130.0,75.0,120_000,18.0,1,1.20,5.0,("IMPULSE",)),
}

TIER_MULTIPLIERS={
    "MICRO":0.20,
    "CAUTIOUS":0.35,
    "BASE":0.65,
    "GOOD":1.00,
    "STRONG":1.50,
    "PRIME":2.00,
    "ELITE":2.50,
}

def classify_regime(f:dict) -> str:
    r250=abs(float(f.get("ret_250ms_bps",0)))
    r1=abs(float(f.get("ret_1s_bps",0)))
    r5=abs(float(f.get("ret_5s_bps",0)))
    rv=abs(float(f.get("realized_vol_5s_bps",0)))
    tc=int(f.get("trade_count_1s",0) or 0)
    if r250>=4 or r1>=7 or (tc>=8 and r1>=3):
        return "IMPULSE"
    if r1>=2 or r5>=5 or rv>=0.35:
        return "TREND"
    if r5>=4 and r1<1:
        return "REVERSAL"
    return "CHOP"

def directional_scores(head:str,f:dict) -> tuple[float,float]:
    r250=float(f.get("ret_250ms_bps",0))
    r1=float(f.get("ret_1s_bps",0))
    r5=float(f.get("ret_5s_bps",0))
    imb=float(f.get("book_imbalance_5",0))
    flow=(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0
    spread=float(f.get("spread_bps",0))
    if head=="VOL":
        impulse=1.2*r250 + 1.3*r1 + 0.35*r5 + 8*imb + 8*flow
        penalty=min(15.0,spread*1.2)
    else:
        impulse=1.0*r250 + 1.0*r1 + 0.25*r5 + 7*imb + 7*flow
        penalty=min(18.0,spread*1.3)
    impulse=max(-30.0,min(30.0,impulse))
    return 50.0+impulse-penalty, 50.0-impulse-penalty

def directional_features(side:str,f:dict) -> dict:
    sign=1.0 if side=="BUY" else -1.0
    d250=sign*float(f.get("ret_250ms_bps",0))
    d1=sign*float(f.get("ret_1s_bps",0))
    d5=sign*float(f.get("ret_5s_bps",0))
    dimb=sign*float(f.get("book_imbalance_5",0))
    dflow=sign*(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0
    vals=(d250,d1,dflow,dimb)
    return {
        "dir_ret_250ms_bps":d250,
        "dir_ret_1s_bps":d1,
        "dir_ret_5s_bps":d5,
        "dir_flow":dflow,
        "dir_imbalance":dimb,
        "aligned_count":sum(1 for x in vals if x>0),
        "opposing_count":sum(1 for x in vals if x<0),
    }

def projected_edge_bps(side:str,score:float,gate:float,f:dict) -> float:
    """Directional edge only; realized volatility is opportunity, not edge."""
    d=directional_features(side,f)
    motion=max(0.0,
        0.95*d["dir_ret_250ms_bps"]
        + 0.90*d["dir_ret_1s_bps"]
        + 0.20*d["dir_ret_5s_bps"]
    )
    alignment=(
        6.0*max(-1.0,min(1.0,d["dir_imbalance"]))
        + 5.0*max(-1.0,min(1.0,d["dir_flow"]))
    )
    score_margin=max(0.0,score-gate)
    return max(0.0,motion+alignment+score_margin*0.55)

def winner_loser_scores(
    pol:CellPolicy,
    side:str,
    score:float,
    regime:str,
    f:dict,
    required_cost_edge_bps:float,
    projected_edge:float,
    history_penalty:float=0.0,
) -> dict:
    d=directional_features(side,f)
    score_margin=max(0.0,score-pol.gate)
    edge_ratio=projected_edge/max(1e-6,required_cost_edge_bps)
    spread=float(f.get("spread_bps",0) or 0)
    spread_ratio=spread/max(0.001,pol.max_spread_bps)
    spread_quality=1.0 if spread_ratio<=0.40 else (0.5 if spread_ratio<=0.70 else (0.0 if spread_ratio<=1.0 else -1.0))

    bid_depth=float(f.get("depth_bid_5_usd",0) or 0)
    ask_depth=float(f.get("depth_ask_5_usd",0) or 0)
    near_depth=min(bid_depth,ask_depth)
    depth_quality="GOOD" if near_depth>=20_000 else ("OK" if near_depth>=2_000 else "BAD")
    depth_score=8.0 if depth_quality=="GOOD" else (3.0 if depth_quality=="OK" else -6.0)

    motion_score=(
        1.0*max(0.0,d["dir_ret_250ms_bps"])
        + 1.0*max(0.0,d["dir_ret_1s_bps"])
        + 0.4*max(0.0,d["dir_ret_5s_bps"])
    )
    alignment_score=(
        8.0*max(-1.0,min(1.0,d["dir_flow"]))
        + 8.0*max(-1.0,min(1.0,d["dir_imbalance"]))
    )
    score_margin_score=min(20.0,score_margin*1.5)
    if edge_ratio>=2.0: cost_score=20.0
    elif edge_ratio>=1.6: cost_score=14.0
    elif edge_ratio>=1.3: cost_score=8.0
    elif edge_ratio>=1.1: cost_score=3.0
    else: cost_score=-8.0

    regime_score=10.0 if regime=="IMPULSE" else (6.0 if regime=="TREND" else -4.0)
    trade_now=bool(int(f.get("live_trade_ok",1) or 0))
    live_activity=float(f.get("live_activity_score",0) or 0)
    activity_score=8.0 if trade_now else -6.0

    winner_score=max(0.0,min(100.0,
        20.0 + motion_score + alignment_score + score_margin_score + cost_score
        + 5.0*spread_quality + depth_score + regime_score + activity_score
    ))

    mixed_penalty=d["opposing_count"]*10.0
    if edge_ratio<1.05: edge_risk=25.0
    elif edge_ratio<1.20: edge_risk=15.0
    elif edge_ratio<1.35: edge_risk=8.0
    else: edge_risk=0.0

    market_quality_risk=0.0
    if spread_ratio>1.0: market_quality_risk+=18.0
    elif spread_ratio>0.75: market_quality_risk+=8.0
    if depth_quality=="BAD": market_quality_risk+=14.0
    regime_risk=0.0 if regime in {"TREND","IMPULSE"} else 10.0
    activity_risk=0.0 if trade_now else 12.0

    loser_risk=max(0.0,min(100.0,
        mixed_penalty + edge_risk + market_quality_risk + regime_risk + activity_risk + max(0.0,history_penalty)
    ))
    sizing_score=winner_score-loser_risk

    return {
        **d,
        "edge_ratio":edge_ratio,
        "spread_quality":spread_quality,
        "depth_quality":depth_quality,
        "winner_score":winner_score,
        "loser_risk":loser_risk,
        "sizing_score":sizing_score,
        "history_penalty":history_penalty,
        "live_activity_score":live_activity,
        "trade_now":trade_now,
    }

def size_decision(
    pol:CellPolicy,
    side:str,
    score:float,
    regime:str,
    f:dict,
    required_cost_edge_bps:float,
    projected_edge:float,
    history_penalty:float=0.0,
) -> dict:
    q=winner_loser_scores(pol,side,score,regime,f,required_cost_edge_bps,projected_edge,history_penalty)
    s=q["sizing_score"]

    # H3T-C churn gate: compute quality violations before sizing so the same
    # evidence can reject weak trades instead of merely shrinking them.
    violations=0
    if q["opposing_count"]>0: violations+=1
    if not q["trade_now"]: violations+=1
    if q["edge_ratio"]<1.45: violations+=1
    if regime!="IMPULSE": violations+=1
    if q["spread_quality"]<0: violations+=1

    reject_reason=None

    # Existing H3 hard rejects.
    if q["edge_ratio"]<1.30:
        reject_reason="EDGE_RATIO_LT_1_30"
    elif q["opposing_count"]>=2:
        reject_reason="OPPOSING_GTE_2"
    elif q["loser_risk"]>=35.0:
        reject_reason="LOSER_RISK_GTE_35"
    elif s<5:
        reject_reason="SIZING_SCORE_LT_5"

    # H3T-C: EXTREME must actually be exceptional. It was dominating flow,
    # so require clean alignment and meaningfully stronger cost-adjusted edge.
    is_extreme="-EXTREME-" in pol.cell
    if reject_reason is None and is_extreme:
        if q["edge_ratio"]<1.60:
            reject_reason="EXTREME_EDGE_RATIO_LT_1_60"
        elif q["opposing_count"]>0:
            reject_reason="EXTREME_OPPOSING"
        elif violations>0:
            reject_reason="EXTREME_QUALITY_VIOLATION"

    if reject_reason:
        tier,mult,reject="REJECT",0.0,True
    elif s<15:
        tier,mult,reject="MICRO",TIER_MULTIPLIERS["MICRO"],False
    elif s<25:
        tier,mult,reject="CAUTIOUS",TIER_MULTIPLIERS["CAUTIOUS"],False
    elif s<40:
        tier,mult,reject="BASE",TIER_MULTIPLIERS["BASE"],False
    elif s<55:
        tier,mult,reject="GOOD",TIER_MULTIPLIERS["GOOD"],False
    elif s<70:
        tier,mult,reject="STRONG",TIER_MULTIPLIERS["STRONG"],False
    elif s<85:
        tier,mult,reject="PRIME",TIER_MULTIPLIERS["PRIME"],False
    else:
        tier,mult,reject="ELITE",TIER_MULTIPLIERS["ELITE"],False

    # Existing marginal-setup money caps.
    if not reject and q["edge_ratio"]<1.45 and mult>TIER_MULTIPLIERS["MICRO"]:
        tier,mult="MICRO",TIER_MULTIPLIERS["MICRO"]
    if not reject and q["loser_risk"]>=25.0 and mult>TIER_MULTIPLIERS["CAUTIOUS"]:
        tier,mult="CAUTIOUS",TIER_MULTIPLIERS["CAUTIOUS"]

    # H3T-C: one violation gets at most BASE; two or more do not pay fees at all.
    if not reject and violations>=2:
        tier,mult,reject="REJECT",0.0,True
        reject_reason="SIZING_VIOLATIONS_GTE_2"
    elif not reject and violations==1 and mult>TIER_MULTIPLIERS["BASE"]:
        tier,mult="BASE",TIER_MULTIPLIERS["BASE"]

    # H3T-C: low-edge / compromised MICRO is churn, not a small opportunity.
    if not reject and tier=="MICRO" and (q["edge_ratio"]<1.45 or violations>=1):
        tier,mult,reject="REJECT",0.0,True
        reject_reason="MICRO_CHURN_GATE"

    # H3T-C: pre-entry score has not earned leverage-like size amplification.
    # Preserve PRIME/ELITE labels for diagnostics, but cap pre-arm size at 1x.
    if not reject and tier in {"PRIME","ELITE"}:
        mult=min(mult,1.0)

    q.update({
        "tier":tier,
        "size_multiplier":mult,
        "reject":reject,
        "reject_reason":reject_reason,
        "sizing_violations":violations,
    })
    return q

def sniper_entry_quality(
    sizing:dict,
    regime:str,
    f:dict,
) -> dict:
    """H3T-D ultra-selective pre-entry quality gate.

    CLEAR requires full short-horizon directional agreement, positive 5s
    persistence, clean market quality, no historical penalty, and strong
    cost-adjusted edge. SUPER is a stricter subset eligible for larger size.
    """
    d=directional_features("BUY" if float(sizing.get("dir_ret_1s_bps",0))>=0 else "SELL", f)
    # Use the already direction-normalized values from sizing whenever present.
    d250=float(sizing.get("dir_ret_250ms_bps", d.get("dir_ret_250ms_bps",0)) or 0)
    d1=float(sizing.get("dir_ret_1s_bps", d.get("dir_ret_1s_bps",0)) or 0)
    d5=float(sizing.get("dir_ret_5s_bps", d.get("dir_ret_5s_bps",0)) or 0)
    dflow=float(sizing.get("dir_flow", d.get("dir_flow",0)) or 0)
    dimb=float(sizing.get("dir_imbalance", d.get("dir_imbalance",0)) or 0)

    clear=(
        regime=="IMPULSE"
        and bool(sizing.get("trade_now"))
        and int(sizing.get("opposing_count",99))==0
        and int(sizing.get("aligned_count",0))>=4
        and float(sizing.get("edge_ratio",0))>=1.80
        and float(sizing.get("winner_score",0))>=96.0
        and float(sizing.get("loser_risk",100))<=12.0
        and int(sizing.get("sizing_violations",99))==0
        and float(sizing.get("spread_quality",-1))>=0.5
        and float(sizing.get("history_penalty",0))<=0.0
        and d250>0.0 and d1>0.0 and d5>0.0
        and dflow>=0.05 and dimb>=0.05
    )
    super_clear=(
        clear
        and float(sizing.get("edge_ratio",0))>=2.20
        and float(sizing.get("winner_score",0))>=98.5
        and float(sizing.get("loser_risk",100))<=8.0
        and d1>=2.0 and d5>=4.0
        and dflow>=0.15 and dimb>=0.15
    )
    return {
        "sniper_clear":clear,
        "sniper_super":super_clear,
        "sniper_d250":d250,
        "sniper_d1":d1,
        "sniper_d5":d5,
        "sniper_dflow":dflow,
        "sniper_dimb":dimb,
    }

def policies_json() -> str:
    ladder="H3T-C live sizing; H3T-D shadow PASS/REJECT only (no entry blocking or resize)"
    return json.dumps({p.cell:{
        "gate":p.gate,"tp_bps":p.take_profit_bps,"sl_bps":p.stop_loss_bps,
        "hold_ms":p.max_hold_ms,"spread_max_bps":p.max_spread_bps,
        "cost_mult":p.cost_mult,"cost_buffer_bps":p.cost_buffer_bps,
        "regimes":list(p.allow_regimes),"sizing":ladder
    } for p in POLICIES.values()},separators=(",",":"))


# ---------- H3T-B: Right Tail 2.0 ----------
TAIL_MILESTONES = (
    (1.0, 0.45),   # MFE >= 1.0*TP -> floor 0.45*TP
    (1.5, 0.85),
    (2.0, 1.30),
    (3.0, 2.10),
)

TAIL_TRAIL_COEF = {"ACCEL": 0.40, "FLAT": 0.30, "DECEL": 0.19}
TAIL_BUCKET_SCALE = {"CORE": 0.85, "FAST": 1.00, "EXTREME": 1.20}
TAIL_TIMEOUT_MULT = {"ACCEL": 4.5, "FLAT": 3.0, "DECEL": 2.0}

# Weak/negative arm baselines are treated against a fixed positive floor.
# This intentionally allows genuine post-arm momentum expansion to classify as
# ACCEL even when the arm moment itself was weak.
TAIL_ARM_MOMENTUM_MIN = 0.75

def classify_tail_accel(m_now: float, m_arm: float) -> str:
    """Post-arm momentum classification.

    Baseline is floored at TAIL_ARM_MOMENTUM_MIN to prevent false ACCEL
    when arm momentum was weak or negative.
    """
    baseline = max(m_arm, TAIL_ARM_MOMENTUM_MIN)
    if m_now <= 0:
        return "DECEL"
    if m_now > baseline * 1.25:
        return "ACCEL"
    if m_now < baseline * 0.5:
        return "DECEL"
    return "FLAT"

def tail_milestones_hit(mfe_bps: float, tp_bps: float) -> list[int]:
    return [
        i for i, (mfe_mult, _) in enumerate(TAIL_MILESTONES)
        if mfe_bps >= mfe_mult * tp_bps
    ]

def tail_active_floor_bps(
    tp_bps: float,
    base_floor_bps: float,
    hit: list[int],
) -> float:
    """Max of base floor and all triggered milestone floors.

    `hit` must be computed via tail_milestones_hit() by caller.
    """
    floors = [TAIL_MILESTONES[i][1] * tp_bps for i in hit]
    return max([base_floor_bps] + floors)

def tail_trail_bps(
    mfe_bps: float,
    tp_bps: float,
    accel: str,
    bucket: str,
) -> float:
    base = TAIL_TRAIL_COEF[accel] * max(mfe_bps, tp_bps)
    return base * TAIL_BUCKET_SCALE.get(bucket, 1.00)

def tail_timeout_ms(max_hold_ms: int, accel: str) -> int:
    return int(max_hold_ms * TAIL_TIMEOUT_MULT[accel])
