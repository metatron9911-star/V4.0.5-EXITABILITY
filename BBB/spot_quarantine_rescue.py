from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Optional

Decision = Literal["HOLD_QUARANTINE","RESCUE_BUY","FINAL_CUT"]


@dataclass(frozen=True)
class RescueConfig:
    high_churn_trade_count: int = 49
    quarantine_ms: int = 4000
    min_buy_ratio: float = 0.55
    min_book_imbalance: float = 0.0
    min_ret_250ms_bps: float = 0.0
    min_ret_1s_bps: float = 0.0
    min_confirmation_votes: int = 3
    max_rescue_buys: int = 1


@dataclass(frozen=True)
class MicroSnapshot:
    ts_ms: int
    trade_count_1s: int
    buy_ratio_1s: float
    book_imbalance_5: float
    ret_250ms_bps: float
    ret_1s_bps: float
    mid: float


@dataclass
class QuarantineState:
    armed_ms: int
    side: Literal["BUY"] = "BUY"
    rescue_buys: int = 0
    initial_trade_count_1s: int = 0
    initial_mid: float = 0.0
    best_mid: float = 0.0
    last_mid: float = 0.0


def should_quarantine(snapshot: MicroSnapshot, cfg: RescueConfig) -> bool:
    return int(snapshot.trade_count_1s) > int(cfg.high_churn_trade_count)


def confirmation_votes(prev: MicroSnapshot, cur: MicroSnapshot, cfg: RescueConfig) -> int:
    higher_high = cur.mid > prev.mid
    buy_pressure = cur.buy_ratio_1s >= cfg.min_buy_ratio
    sellers_not_dominant = cur.book_imbalance_5 >= cfg.min_book_imbalance
    micro_momentum = cur.ret_250ms_bps > cfg.min_ret_250ms_bps
    short_momentum = cur.ret_1s_bps > cfg.min_ret_1s_bps
    return sum((higher_high, buy_pressure, sellers_not_dominant, micro_momentum, short_momentum))


def deterioration_votes(prev: MicroSnapshot, cur: MicroSnapshot, cfg: RescueConfig) -> int:
    lower_low = cur.mid < prev.mid
    sell_pressure = cur.buy_ratio_1s < 0.5
    negative_imbalance = cur.book_imbalance_5 < 0.0
    negative_micro = cur.ret_250ms_bps <= 0.0
    negative_short = cur.ret_1s_bps <= 0.0
    return sum((lower_low, sell_pressure, negative_imbalance, negative_micro, negative_short))


def evaluate_quarantine(
    state: QuarantineState,
    snapshots: Iterable[MicroSnapshot],
    cfg: RescueConfig,
) -> tuple[Decision, Optional[MicroSnapshot], dict]:
    seq = list(snapshots)
    if not seq:
        return "HOLD_QUARANTINE", None, {"reason": "no_snapshots"}

    state.best_mid = max(state.best_mid or state.initial_mid, *(s.mid for s in seq))
    state.last_mid = seq[-1].mid

    elapsed = seq[-1].ts_ms - state.armed_ms
    if elapsed < cfg.quarantine_ms:
        return "HOLD_QUARANTINE", seq[-1], {
            "reason": "quarantine_active",
            "elapsed_ms": elapsed,
        }

    # Use the most recent transition; no lookahead beyond the quarantine window.
    prev = seq[-2] if len(seq) >= 2 else seq[-1]
    cur = seq[-1]
    c_votes = confirmation_votes(prev, cur, cfg)
    d_votes = deterioration_votes(prev, cur, cfg)

    if (
        state.rescue_buys < cfg.max_rescue_buys
        and c_votes >= cfg.min_confirmation_votes
        and cur.mid >= state.initial_mid
    ):
        return "RESCUE_BUY", cur, {
            "reason": "confirmed_recovery",
            "confirmation_votes": c_votes,
            "deterioration_votes": d_votes,
            "elapsed_ms": elapsed,
        }

    return "FINAL_CUT", cur, {
        "reason": "confirmation_failed",
        "confirmation_votes": c_votes,
        "deterioration_votes": d_votes,
        "elapsed_ms": elapsed,
    }
