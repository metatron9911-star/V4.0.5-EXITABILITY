"""Shared BBB microstructure feature engine.

It creates normalized features only. Strategy thresholds stay head-specific.
"""
from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass
import math
from statistics import pstdev
from typing import Deque

from orderbook import LocalBook


@dataclass
class Trade:
    ts_ms: int
    price: float
    size: float
    side: str


class FeatureEngine:
    def __init__(self) -> None:
        self.trades: dict[str,Deque[Trade]] = defaultdict(lambda: deque(maxlen=20000))
        self.mids: dict[str,Deque[tuple[int,float]]] = defaultdict(lambda: deque(maxlen=10000))

    def on_trade(self, symbol: str, ts_ms: int, price: float, size: float, side: str) -> None:
        if price<=0 or size<0: return
        self.trades[symbol].append(Trade(ts_ms,price,size,side.upper()))

    def on_book(self, symbol: str, ts_ms: int, book: LocalBook) -> None:
        m=book.mid
        if m and m>0:
            self.mids[symbol].append((ts_ms,m))
        self._trim(symbol,ts_ms)

    def _trim(self,symbol: str,now_ms: int) -> None:
        cutoff=now_ms-15000
        while self.trades[symbol] and self.trades[symbol][0].ts_ms<cutoff:
            self.trades[symbol].popleft()
        while self.mids[symbol] and self.mids[symbol][0][0]<cutoff:
            self.mids[symbol].popleft()

    def _mid_at(self,symbol: str,target_ms: int) -> float | None:
        arr=self.mids[symbol]
        if not arr: return None
        return min(arr,key=lambda x:abs(x[0]-target_ms))[1]

    def snapshot(self,symbol: str,now_ms: int,book: LocalBook) -> dict | None:
        self._trim(symbol,now_ms)
        mid=book.mid
        if not mid or not book.synced: return None

        def ret(window_ms: int) -> float:
            old=self._mid_at(symbol,now_ms-window_ms)
            return (mid/old-1)*10000 if old else 0.0

        t1=[x for x in self.trades[symbol] if x.ts_ms>=now_ms-1000]
        t5=[x for x in self.trades[symbol] if x.ts_ms>=now_ms-5000]
        buy=sum(x.size*x.price for x in t1 if x.side=="BUY")
        sell=sum(x.size*x.price for x in t1 if x.side=="SELL")
        mids=[x[1] for x in self.mids[symbol] if x[0]>=now_ms-5000]
        rets=[(mids[i]/mids[i-1]-1)*10000 for i in range(1,len(mids)) if mids[i-1]>0]
        rv=pstdev(rets) if len(rets)>1 else 0.0

        return {
            "mid":mid,
            "bid":book.best_bid,
            "ask":book.best_ask,
            "spread_bps":book.spread_bps or 0.0,
            "depth_bid_5_usd":book.depth_usd("bid",5),
            "depth_ask_5_usd":book.depth_usd("ask",5),
            "book_imbalance_5":book.imbalance(5),
            "trade_count_1s":len(t1),
            "trade_count_5s":len(t5),
            "buy_ratio_1s":buy/(buy+sell) if buy+sell>0 else 0.5,
            "ret_250ms_bps":ret(250),
            "ret_1s_bps":ret(1000),
            "ret_5s_bps":ret(5000),
            "realized_vol_5s_bps":rv,
        }


def classify_head(symbol: str, product_class: str="STANDARD") -> str:
    if product_class in {"PRE_MARKET","INNOVATION"}:
        return "LAUNCH"
    if symbol in {"BTCUSDT","ETHUSDT","SOLUSDT"}:
        return "LIQUID"
    return "VOL"


def baseline_score(f: dict) -> float:
    """Observer-only score. It is NOT a promoted trading threshold."""
    momentum=max(-20.0,min(20.0,float(f["ret_1s_bps"])))
    imbalance=float(f["book_imbalance_5"])*15.0
    buy=(float(f["buy_ratio_1s"])-0.5)*20.0
    spread_penalty=min(20.0,float(f["spread_bps"])*2.0)
    return 50.0 + momentum + imbalance + buy - spread_penalty
