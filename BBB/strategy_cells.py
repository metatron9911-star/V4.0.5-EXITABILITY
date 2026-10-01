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
    ("LIQUID","BUY"): CellPolicy("LIQUID-LONG","LIQUID","BUY",62.0,0.020,2,20_000,34.0,22.0,150_000,1.5,3,1.20,2.0,("TREND","IMPULSE")),
    ("LIQUID","SELL"):CellPolicy("LIQUID-SHORT","LIQUID","SELL",62.0,0.020,2,20_000,34.0,22.0,150_000,1.5,3,1.20,2.0,("TREND","IMPULSE")),
    ("VOL","BUY"):    CellPolicy("VOL-LONG","VOL","BUY",67.0,0.015,3,25_000,80.0,48.0,150_000,10.0,1,1.15,3.0,("TREND","IMPULSE")),
    ("VOL","SELL"):   CellPolicy("VOL-SHORT","VOL","SELL",67.0,0.015,3,25_000,80.0,48.0,150_000,10.0,1,1.15,3.0,("TREND","IMPULSE")),
    ("LAUNCH","BUY"): CellPolicy("LAUNCH-LONG","LAUNCH","BUY",74.0,0.010,1,45_000,145.0,90.0,120_000,24.0,1,1.25,6.0,("IMPULSE",)),
    ("LAUNCH","SELL"):CellPolicy("LAUNCH-SHORT","LAUNCH","SELL",74.0,0.010,1,45_000,145.0,90.0,120_000,24.0,1,1.25,6.0,("IMPULSE",)),
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

def policies_json() -> str:
    return json.dumps({p.cell:{
        "gate":p.gate,"tp_bps":p.take_profit_bps,"sl_bps":p.stop_loss_bps,
        "hold_ms":p.max_hold_ms,"spread_max_bps":p.max_spread_bps,
        "cost_mult":p.cost_mult,"cost_buffer_bps":p.cost_buffer_bps,
        "regimes":list(p.allow_regimes)} for p in POLICIES.values()},separators=(",",":"))
