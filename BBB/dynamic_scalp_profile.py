"""Dynamic dual-module scalping profile for BBB PAPER research.

Routes instruments by *current* microstructure, not by a hardcoded symbol list:
- HYPE_TREND: tape/impulse breakout logic.
- LIQUID_FLAT: DOM wall / level-rejection logic.

This module contains settings and pure classification helpers only.
It does not enable entries and has no private exchange execution path.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json
from typing import Literal

Segment = Literal["HYPE_TREND","LIQUID_FLAT","NO_TRADE"]

@dataclass(frozen=True)
class RouterSettings:
    # HYPE / trend routing.
    hype_min_trade_count_1s: int = 3
    hype_min_trade_count_5s: int = 12
    hype_min_abs_ret_1s_bps: float = 3.0
    hype_min_abs_ret_5s_bps: float = 8.0
    hype_min_realized_vol_5s_bps: float = 0.35
    hype_max_spread_bps: float = 18.0

    # LIQUID / flat routing.
    liquid_max_spread_bps: float = 4.0
    liquid_min_near_depth_usd: float = 20_000.0
    liquid_max_abs_ret_1s_bps: float = 3.0
    liquid_max_realized_vol_5s_bps: float = 0.50

@dataclass(frozen=True)
class HypeSettings:
    # Breakout setup.
    local_level_lookback_sec: int = 60
    min_level_touches: int = 2
    touch_tolerance_bps: float = 4.0

    # "Tape speed +300%" expressed as current/slow baseline ratio.
    tape_speed_ratio_min: float = 3.0
    min_trade_count_1s: int = 4

    # Entry quality.
    min_directional_ret_250ms_bps: float = 0.5
    min_directional_ret_1s_bps: float = 2.0
    min_directional_flow: float = 0.10
    max_spread_bps: float = 15.0

    # PAPER risk profile. Keep 1x while validating; leverage can be studied later.
    paper_leverage: float = 1.0
    risk_per_trade_frac: float = 0.003
    stop_bps: float = 15.0
    timeout_ms: int = 12_000
    breakeven_arm_bps: float = 18.0

    # Do not clip the right tail too early.
    take_profit_bps: float = 150.0
    trail_arm_bps: float = 35.0
    trail_giveback_bps: float = 20.0

@dataclass(frozen=True)
class LiquidSettings:
    # DOM wall: compare candidate level notional to median top-level notional.
    # This is a live-book proxy for "very large density"; it is testable with L50.
    wall_ratio_min: float = 5.0
    wall_top_levels: int = 20
    wall_max_distance_bps: float = 12.0

    # Price touches wall but does not eat it during this interval.
    rejection_confirm_ms: int = 4_000
    max_wall_consumed_frac: float = 0.50
    min_book_imbalance_abs: float = 0.05

    # PAPER risk profile.
    paper_leverage: float = 1.0
    risk_per_trade_frac: float = 0.003
    stop_buffer_bps: float = 5.0
    take_profit_bps: float = 40.0
    timeout_ms: int = 180_000
    breakeven_arm_bps: float = 18.0

@dataclass(frozen=True)
class GlobalRiskSettings:
    max_total_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_daily_drawdown_frac: float = 0.03
    max_session_drawdown_frac: float = 0.05
    max_spread_shock_multiple: float = 2.0
    cooldown_after_loss_ms: int = 30_000
    max_slippage_bps_hype: float = 20.0
    max_slippage_bps_liquid: float = 8.0
    no_dca: bool = True
    max_rescue_buys: int = 1

ROUTER=RouterSettings()
HYPE=HypeSettings()
LIQUID=LiquidSettings()
RISK=GlobalRiskSettings()

def classify_segment(f: dict) -> Segment:
    tc1=int(f.get("trade_count_1s",0) or 0)
    tc5=int(f.get("trade_count_5s",0) or 0)
    r1=abs(float(f.get("ret_1s_bps",0) or 0))
    r5=abs(float(f.get("ret_5s_bps",0) or 0))
    rv=abs(float(f.get("realized_vol_5s_bps",0) or 0))
    spread=float(f.get("spread_bps",999) or 999)
    bid_depth=float(f.get("depth_bid_5_usd",0) or 0)
    ask_depth=float(f.get("depth_ask_5_usd",0) or 0)
    near_depth=min(bid_depth,ask_depth)

    hype_activity=(
        (tc1>=ROUTER.hype_min_trade_count_1s or tc5>=ROUTER.hype_min_trade_count_5s)
        and (r1>=ROUTER.hype_min_abs_ret_1s_bps or r5>=ROUTER.hype_min_abs_ret_5s_bps or rv>=ROUTER.hype_min_realized_vol_5s_bps)
        and spread<=ROUTER.hype_max_spread_bps
    )
    if hype_activity:
        return "HYPE_TREND"

    liquid_quality=(
        spread<=ROUTER.liquid_max_spread_bps
        and near_depth>=ROUTER.liquid_min_near_depth_usd
        and r1<=ROUTER.liquid_max_abs_ret_1s_bps
        and rv<=ROUTER.liquid_max_realized_vol_5s_bps
    )
    if liquid_quality:
        return "LIQUID_FLAT"

    return "NO_TRADE"

def directional_hype_trigger(side: str, f: dict) -> bool:
    sign=1.0 if side.upper()=="BUY" else -1.0
    d250=sign*float(f.get("ret_250ms_bps",0) or 0)
    d1=sign*float(f.get("ret_1s_bps",0) or 0)
    flow=sign*((float(f.get("buy_ratio_1s",0.5) or 0.5)-0.5)*2.0)
    tc1=int(f.get("trade_count_1s",0) or 0)
    spread=float(f.get("spread_bps",999) or 999)
    return (
        tc1>=HYPE.min_trade_count_1s
        and d250>=HYPE.min_directional_ret_250ms_bps
        and d1>=HYPE.min_directional_ret_1s_bps
        and flow>=HYPE.min_directional_flow
        and spread<=HYPE.max_spread_bps
    )

def settings_json() -> str:
    return json.dumps({
        "router":asdict(ROUTER),
        "hype":asdict(HYPE),
        "liquid":asdict(LIQUID),
        "risk":asdict(RISK),
    },separators=(",",":"))
