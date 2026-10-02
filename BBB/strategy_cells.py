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
    """Directional edge only. Volatility is opportunity, never free edge."""
    sign=1.0 if side=="BUY" else -1.0
    d250=sign*float(f.get("ret_250ms_bps",0))
    d1=sign*float(f.get("ret_1s_bps",0))
    d5=sign*float(f.get("ret_5s_bps",0))
    dimb=sign*float(f.get("book_imbalance_5",0))
    dflow=sign*(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0
    # Require persistence/alignment: opposing flow/book subtract from expected edge.
    motion=max(0.0,0.9*d250 + 0.9*d1 + 0.20*d5)
    alignment=6.0*max(-1.0,min(1.0,dimb)) + 5.0*max(-1.0,min(1.0,dflow))
    score_margin=max(0.0,score-gate)
    return max(0.0,motion + alignment + score_margin*0.55)

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
    sign=1.0 if side=="BUY" else -1.0
    dimb=sign*float(f.get("book_imbalance_5",0))
    dflow=sign*(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0
    d250=sign*float(f.get("ret_250ms_bps",0))
    d1=sign*float(f.get("ret_1s_bps",0))
    aligned_count=sum(1 for x in (dimb,dflow,d250,d1) if x>0)
    opposing_count=sum(1 for x in (dimb,dflow,d250,d1) if x<0)
    regime_bonus=1.0 if regime=="IMPULSE" else (0.65 if regime=="TREND" else 0.0)

    confidence=max(0.0,min(100.0,
        30.0
        + min(22.0,score_margin*1.7)
        + min(22.0,edge_surplus*1.4)
        + 8.0*spread_quality
        + 7.0*max(-1.0,min(1.0,dimb))
        + 6.0*max(-1.0,min(1.0,dflow))
        + 3.0*regime_bonus
        + 2.0*aligned_count
        - 5.0*opposing_count
    ))

    # High size requires directional agreement, not merely a large absolute signal.
    if confidence>=84 and score_margin>=8 and edge_ratio>=1.70 and regime=="IMPULSE" and aligned_count>=3 and opposing_count==0:
        return "PRIME",2.00,confidence
    if confidence>=72 and score_margin>=5 and edge_ratio>=1.45 and aligned_count>=3 and opposing_count<=1:
        return "STRONG",1.50,confidence
    if confidence>=60 and edge_ratio>=1.25 and aligned_count>=2:
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
