from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Literal

from spot_quarantine_rescue import (
    RescueConfig, MicroSnapshot, QuarantineState,
    should_quarantine, evaluate_quarantine,
)

Action = Literal[
    "NOOP","BASE_BUY","PLACE_TP","SAFETY_BUY",
    "ENTER_QUARANTINE","RESCUE_BUY","FINAL_CUT","BASKET_STOP","TIME_EXIT"
]


@dataclass(frozen=True)
class SpotGridConfig:
    timeframe_min: int = 15
    rsi_max: float = 35.0
    atr_min: float = 0.006
    grid_step_atr: float = 2.0
    tp_atr: float = 1.0
    basket_stop_frac: float = 0.12
    max_levels: int = 3
    safety_size_mult: float = 1.5
    max_age_bars: int = 32
    rescue: RescueConfig = field(default_factory=RescueConfig)


@dataclass
class Basket:
    symbol: str
    qty: float
    cost: float
    opened_ms: int
    opened_bar_index: int
    base_atr: float
    levels: int = 1
    quarantine: Optional[QuarantineState] = None
    rescue_used: bool = False

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


class SpotGridMachine:
    """Paper/research state machine for the new 15m spot candidate.

    The microstructure rescue overlay is applied only when a safety-buy would
    otherwise be considered. It never creates repeated martingale rescues:
    at most one RESCUE_BUY is allowed.
    """

    def __init__(self, cfg: SpotGridConfig):
        self.cfg = cfg
        self.basket: Optional[Basket] = None

    def open_base(self, symbol: str, price: float, atr_frac: float, now_ms: int, bar_index: int) -> Action:
        if self.basket is not None:
            return "NOOP"
        self.basket = Basket(
            symbol=symbol, qty=1.0, cost=price, opened_ms=now_ms,
            opened_bar_index=bar_index, base_atr=atr_frac,
        )
        return "BASE_BUY"

    def target_price(self) -> Optional[float]:
        if not self.basket:
            return None
        return self.basket.avg * (1.0 + self.cfg.tp_atr * max(self.basket.base_atr, 0.002))

    def safety_trigger(self) -> Optional[float]:
        if not self.basket:
            return None
        return self.basket.avg * (1.0 - self.cfg.grid_step_atr * max(self.basket.base_atr, 0.002))

    def basket_stop(self) -> Optional[float]:
        if not self.basket:
            return None
        return self.basket.avg * (1.0 - self.cfg.basket_stop_frac)

    def on_safety_candidate(self, snap: MicroSnapshot) -> Action:
        b=self.basket
        if b is None or b.levels >= self.cfg.max_levels:
            return "NOOP"

        # High-churn candidates are not averaged immediately.
        if should_quarantine(snap, self.cfg.rescue):
            if b.quarantine is None:
                b.quarantine=QuarantineState(
                    armed_ms=snap.ts_ms,
                    initial_trade_count_1s=snap.trade_count_1s,
                    initial_mid=snap.mid,
                    best_mid=snap.mid,
                    last_mid=snap.mid,
                    rescue_buys=1 if b.rescue_used else 0,
                )
                return "ENTER_QUARANTINE"
            return "NOOP"

        # Non-high-churn path may use the ordinary safety order.
        self._add_safety(snap.mid, rescue=False)
        return "SAFETY_BUY"

    def on_quarantine_snapshots(self, snaps: list[MicroSnapshot]) -> Action:
        b=self.basket
        if b is None or b.quarantine is None:
            return "NOOP"

        decision, snap, meta = evaluate_quarantine(b.quarantine, snaps, self.cfg.rescue)
        if decision=="HOLD_QUARANTINE":
            return "NOOP"
        if decision=="RESCUE_BUY" and snap is not None and not b.rescue_used:
            self._add_safety(snap.mid, rescue=True)
            b.rescue_used=True
            b.quarantine=None
            return "RESCUE_BUY"

        # Any failed confirmation, or attempted second rescue, is an explicit cut.
        b.quarantine=None
        return "FINAL_CUT"

    def _add_safety(self, price: float, rescue: bool) -> None:
        b=self.basket
        if b is None:
            return
        # Soft DCA only. Rescue never escalates beyond the same sizing schedule.
        add_qty = self.cfg.safety_size_mult ** b.levels
        b.cost += price * add_qty
        b.qty += add_qty
        b.levels += 1

    def close(self) -> None:
        self.basket=None
