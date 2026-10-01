"""BBB PAPER core scaffold.

No exchange order submission path exists here.
The point of this module is to freeze accounting and attribution semantics before
strategy tuning begins.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
import math
import time
from typing import Dict, Optional


def now_ms() -> int:
    return int(time.time() * 1000)


class ProductClass(str, Enum):
    STANDARD = "STANDARD"
    PRE_MARKET = "PRE_MARKET"
    INNOVATION = "INNOVATION"


class HeadName(str, Enum):
    LIQUID = "LIQUID"
    VOL = "VOL"
    LAUNCH = "LAUNCH"


@dataclass(frozen=True)
class FeeSchedule:
    maker_bps: float
    taker_bps: float

    def fee_usd(self, notional_usd: float, maker: bool) -> float:
        bps = self.maker_bps if maker else self.taker_bps
        return notional_usd * bps / 10_000.0


BYBIT_FEES: Dict[ProductClass, FeeSchedule] = {
    ProductClass.STANDARD: FeeSchedule(2.0, 5.5),
    ProductClass.PRE_MARKET: FeeSchedule(4.0, 10.0),
    ProductClass.INNOVATION: FeeSchedule(4.0, 11.0),
}


@dataclass
class Wallets:
    start_usd: float
    trading_cash_usd: float = field(init=False)
    profit_wallet_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    slippage_impact_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.start_usd <= 0 or not math.isfinite(self.start_usd):
            raise ValueError("start_usd must be positive")
        self.trading_cash_usd = self.start_usd

    def reserve_entry(self, margin_usd: float, entry_fee_usd: float) -> None:
        debit = margin_usd + entry_fee_usd
        if margin_usd <= 0 or entry_fee_usd < 0 or debit > self.trading_cash_usd:
            raise ValueError("insufficient trading cash")
        self.trading_cash_usd -= debit
        self.fees_usd += entry_fee_usd

    def settle_close(
        self,
        margin_usd: float,
        gross_pnl_usd: float,
        entry_fee_usd: float,
        exit_fee_usd: float,
        funding_usd: float,
        slippage_impact_usd: float,
    ) -> float:
        # Entry commission was already removed from cash on entry. Do not debit it twice.
        cash_credit = margin_usd + gross_pnl_usd - exit_fee_usd - funding_usd - slippage_impact_usd
        self.trading_cash_usd += cash_credit
        net = gross_pnl_usd - entry_fee_usd - exit_fee_usd - funding_usd - slippage_impact_usd
        self.realized_pnl_usd += net
        self.fees_usd += exit_fee_usd
        self.funding_usd += funding_usd
        self.slippage_impact_usd += slippage_impact_usd
        return net

    def skim_profit(self, amount_usd: float) -> None:
        if amount_usd <= 0 or amount_usd > self.trading_cash_usd:
            raise ValueError("invalid skim")
        self.trading_cash_usd -= amount_usd
        self.profit_wallet_usd += amount_usd

    @property
    def total_cash_usd(self) -> float:
        return self.trading_cash_usd + self.profit_wallet_usd


@dataclass
class SignalSnapshot:
    exchange_ts_ms: int
    receive_ts_ms: int
    signal_ts_ms: int
    symbol: str
    head: HeadName
    product_class: ProductClass
    score: float
    gate: float
    bid: float
    ask: float
    expected_entry_vwap: float
    spread_bps: float
    expected_roundtrip_cost_bps: float
    risk_state: str
    features: dict

    @property
    def source_age_ms(self) -> int:
        return max(0, self.receive_ts_ms - self.exchange_ts_ms)

    @property
    def processing_ms(self) -> int:
        return max(0, self.signal_ts_ms - self.receive_ts_ms)


@dataclass
class Position:
    position_id: str
    head: HeadName
    product_class: ProductClass
    symbol: str
    side: str
    leverage: float
    margin_usd: float
    notional_usd: float
    entry_vwap: float
    opened_ms: int
    signal: SignalSnapshot
    entry_fee_usd: float
    funding_usd: float = 0.0
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    mfe_ms: int = 0
    mae_ms: int = 0
    tail_armed: bool = False
    tail_arm_ms: int = 0


class PaperBook:
    """Global wallet pair with per-head attribution.

    Heads may request capital; only this object is allowed to mutate wallet cash.
    """

    def __init__(self, start_usd: float = 500.0) -> None:
        self.wallets = Wallets(start_usd)
        self.positions: Dict[str, Position] = {}
        self.head_realized: Dict[HeadName, float] = {h: 0.0 for h in HeadName}

    def open_position(
        self,
        position_id: str,
        signal: SignalSnapshot,
        side: str,
        leverage: float,
        margin_usd: float,
        entry_vwap: float,
        maker: bool = False,
    ) -> Position:
        if position_id in self.positions:
            raise ValueError("duplicate position_id")
        if leverage <= 0:
            raise ValueError("leverage must be positive")
        notional = margin_usd * leverage
        fee = BYBIT_FEES[signal.product_class].fee_usd(notional, maker)
        # Commission is a real cash cost at fill.
        self.wallets.reserve_entry(margin_usd, fee)

        p = Position(
            position_id=position_id,
            head=signal.head,
            product_class=signal.product_class,
            symbol=signal.symbol,
            side=side,
            leverage=leverage,
            margin_usd=margin_usd,
            notional_usd=notional,
            entry_vwap=entry_vwap,
            opened_ms=now_ms(),
            signal=signal,
            entry_fee_usd=fee,
            mfe_ms=now_ms(),
            mae_ms=now_ms(),
        )
        self.positions[position_id] = p
        return p

    def close_position(
        self,
        position_id: str,
        exit_vwap: float,
        exit_fee_maker: bool = False,
        funding_usd: float = 0.0,
        slippage_impact_usd: float = 0.0,
    ) -> float:
        p = self.positions.pop(position_id)
        direction = 1.0 if p.side.upper() == "BUY" else -1.0
        gross = direction * (exit_vwap / p.entry_vwap - 1.0) * p.notional_usd
        exit_fee = BYBIT_FEES[p.product_class].fee_usd(p.notional_usd, exit_fee_maker)
        net = self.wallets.settle_close(
            margin_usd=p.margin_usd,
            gross_pnl_usd=gross,
            entry_fee_usd=p.entry_fee_usd,
            exit_fee_usd=exit_fee,
            funding_usd=funding_usd,
            slippage_impact_usd=slippage_impact_usd,
        )
        self.head_realized[p.head] += net
        return net

    def snapshot(self) -> dict:
        return {
            "wallets": asdict(self.wallets),
            "open_positions": len(self.positions),
            "head_realized": {k.value: v for k, v in self.head_realized.items()},
        }
