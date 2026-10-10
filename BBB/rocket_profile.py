"""ROCKET scalper settings.

Single-purpose PAPER strategy: only instruments with meme-like or higher volatility,
tight spread, and dense live event flow are eligible.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import json

@dataclass(frozen=True)
class RocketSettings:
    # 24h admission (worker/universe scanner)
    min_turnover_usd: float = 5_000_000.0
    min_range24h_bps: float = 1000.0   # >=10% daily high-low range
    max_universe: int = 14

    # live market-quality gate
    max_spread_bps: float = 8.0
    min_trade_count_1s: int = 8
    min_trade_count_5s: int = 35
    min_realized_vol_5s_bps: float = 0.60
    min_abs_ret_1s_bps: float = 3.0
    min_abs_ret_5s_bps: float = 10.0

    # breakout / continuation
    level_lookback_sec: int = 30
    min_level_touches: int = 2
    touch_tolerance_bps: float = 4.0
    arm_tape_ratio_min: float = 2.5
    confirm_ms: int = 2_000
    confirm_samples: int = 2
    confirm_quorum_votes: int = 3  # strong confirmation snapshot: 3 of 4 votes
    confirm_consecutive_samples: int = 2  # require 2 strong snapshots in a row
    final_entry_min_votes: int = 3  # final snapshot must still be strong before MARKET entry
    max_entry_chase_bps: float = 35.0  # do not chase an already extended breakout
    min_beyond_level_bps: float = 2.0
    max_retest_bps: float = 2.0
    confirm_tape_ratio_min: float = 2.0
    confirm_tape_vs_arm_min: float = 0.80
    min_directional_flow: float = 0.20
    min_directional_ret_250ms_bps: float = 0.8
    min_directional_ret_1s_bps: float = 2.5

    # PAPER execution/risk. 1x only while proving edge.
    position_fraction: float = 0.02
    position_multiplier: float = 3.0  # ordinary stake formula, multiplied by 3
    max_open_positions: int = 2
    stop_bps: float = 25.0
    timeout_ms: int = 10_000
    breakeven_arm_bps: float = 45.0
    breakeven_floor_bps: float = 25.0
    trail_arm_bps: float = 55.0
    trail_giveback_bps: float = 25.0
    hard_take_profit_bps: float = 250.0
    cooldown_after_loss_ms: int = 30_000

ROCKET=RocketSettings()

def live_eligible(f:dict) -> bool:
    spread=float(f.get("spread_bps",999) or 999)
    t1=int(f.get("trade_count_1s",0) or 0)
    t5=int(f.get("trade_count_5s",0) or 0)
    rv=abs(float(f.get("realized_vol_5s_bps",0) or 0))
    r1=abs(float(f.get("ret_1s_bps",0) or 0))
    r5=abs(float(f.get("ret_5s_bps",0) or 0))
    return (
        spread<=ROCKET.max_spread_bps
        and t1>=ROCKET.min_trade_count_1s
        and t5>=ROCKET.min_trade_count_5s
        and rv>=ROCKET.min_realized_vol_5s_bps
        and (r1>=ROCKET.min_abs_ret_1s_bps or r5>=ROCKET.min_abs_ret_5s_bps)
    )

def directional_trigger(side:str,f:dict) -> bool:
    sign=1.0 if side=="BUY" else -1.0
    d250=sign*float(f.get("ret_250ms_bps",0) or 0)
    d1=sign*float(f.get("ret_1s_bps",0) or 0)
    flow=sign*((float(f.get("buy_ratio_1s",0.5) or 0.5)-0.5)*2.0)
    return (
        live_eligible(f)
        and d250>=ROCKET.min_directional_ret_250ms_bps
        and d1>=ROCKET.min_directional_ret_1s_bps
        and flow>=ROCKET.min_directional_flow
    )

def settings_json():
    return json.dumps(asdict(ROCKET),separators=(",",":"))
