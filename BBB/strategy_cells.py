"""BBB strategy cells v1.

Six independent policy cells: product class × direction.
Shared market feed/execution/accounting; independent entry/exit/regime parameters.
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
    # Liquid emphasizes persistent order-flow; VOL emphasizes acceleration/tails;
    # Launch demands larger directional displacement and penalizes spread hardest.
    if head=="LIQUID":
        impulse=0.8*r250 + 1.2*r1 + 0.25*r5 + 10*imb + 10*flow
        penalty=min(12.0,spread*1.5)
    elif head=="VOL":
        impulse=1.2*r250 + 1.3*r1 + 0.35*r5 + 8*imb + 8*flow
        penalty=min(15.0,spread*1.2)
    else:
        impulse=1.0*r250 + 1.5*r1 + 0.5*r5 + 6*imb + 6*flow
        penalty=min(22.0,spread*1.5)
    impulse=max(-30.0,min(30.0,impulse))
    return 50.0+impulse-penalty, 50.0-impulse-penalty

def projected_edge_bps(side:str,score:float,gate:float,f:dict) -> float:
    sign=1.0 if side=="BUY" else -1.0
    aligned=max(0.0,
        sign*float(f.get("ret_250ms_bps",0)),
        sign*float(f.get("ret_1s_bps",0)),
        0.5*sign*float(f.get("ret_5s_bps",0)),
    )
    rv=max(0.0,float(f.get("realized_vol_5s_bps",0)))
    score_margin=max(0.0,score-gate)
    return aligned + min(20.0,rv*6.0) + score_margin*0.8

def sizing_tier(pol:CellPolicy,side:str,score:float,regime:str,f:dict,expected_cost_bps:float,projected_edge:float) -> tuple[str,float,float]:
    """Return (tier, multiplier, confidence 0..100).

    Sizing uses only information known before entry. "PRIME" means higher-confidence,
    never certainty. Weak-but-valid setups get smaller size instead of equal risk.
    """
    score_margin=max(0.0,score-pol.gate)
    edge_surplus=max(0.0,projected_edge-(expected_cost_bps*pol.cost_mult+pol.cost_buffer_bps))
    edge_ratio=projected_edge/max(1.0,expected_cost_bps)
    spread=float(f.get("spread_bps",0))
    spread_quality=max(0.0,1.0-spread/max(0.001,pol.max_spread_bps))
    imb=abs(float(f.get("book_imbalance_5",0)))
    flow=abs(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0
    regime_bonus=1.0 if regime=="IMPULSE" else (0.65 if regime=="TREND" else 0.0)

    confidence=min(100.0,
        35.0
        + min(25.0,score_margin*2.0)
        + min(20.0,edge_surplus*1.5)
        + 8.0*spread_quality
        + 6.0*min(1.0,imb)
        + 4.0*min(1.0,flow)
        + 2.0*regime_bonus
    )

    # Progressive paper sizing: doubtful-but-valid setups stay small; only strongly
    # overqualified setups receive the top multiplier.
    if confidence>=82 and score_margin>=8 and edge_ratio>=1.65 and regime=="IMPULSE":
        return "PRIME",2.00,confidence
    if confidence>=70 and score_margin>=5 and edge_ratio>=1.40:
        return "STRONG",1.50,confidence
    if confidence>=58 and edge_ratio>=1.20:
        return "BASE",1.00,confidence
    return "CAUTIOUS",0.50,confidence

def policies_json() -> str:
    return json.dumps({p.cell:{
        "gate":p.gate,"tp_bps":p.take_profit_bps,"sl_bps":p.stop_loss_bps,
        "hold_ms":p.max_hold_ms,"spread_max_bps":p.max_spread_bps,
        "cost_mult":p.cost_mult,"cost_buffer_bps":p.cost_buffer_bps,
        "regimes":list(p.allow_regimes),
        "sizing":"CAUTIOUS 0.5x / BASE 1x / STRONG 1.5x / PRIME 2x"
        } for p in POLICIES.values()},separators=(",",":"))
