"""Local Bybit L50 order-book reconstruction."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Iterable, Tuple


def _pairs(rows: Iterable) -> list[tuple[float, float]]:
    out=[]
    for row in rows or []:
        try:
            p=float(row[0]); q=float(row[1])
            if p>0 and q>=0: out.append((p,q))
        except Exception:
            continue
    return out


@dataclass
class LocalBook:
    symbol: str
    bids: Dict[float,float] = field(default_factory=dict)
    asks: Dict[float,float] = field(default_factory=dict)
    update_id: int = 0
    exchange_ts_ms: int = 0
    synced: bool = False

    def apply(self, msg_type: str | None, data: dict, exchange_ts_ms: int = 0) -> None:
        if not isinstance(data,dict):
            return
        if msg_type == "snapshot":
            self.bids={p:q for p,q in _pairs(data.get("b")) if q>0}
            self.asks={p:q for p,q in _pairs(data.get("a")) if q>0}
            self.synced=True
        else:
            if not self.synced:
                return
            for side,key in ((self.bids,"b"),(self.asks,"a")):
                for p,q in _pairs(data.get(key)):
                    if q==0: side.pop(p,None)
                    else: side[p]=q
        self.update_id=int(data.get("u") or self.update_id or 0)
        self.exchange_ts_ms=int(data.get("cts") or exchange_ts_ms or self.exchange_ts_ms or 0)

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        b,a=self.best_bid,self.best_ask
        return (b+a)/2 if b and a else None

    @property
    def spread_bps(self) -> float | None:
        b,a,m=self.best_bid,self.best_ask,self.mid
        return (a-b)/m*10000 if b and a and m else None

    def top(self, side: str, n: int=5) -> list[tuple[float,float]]:
        data=self.bids if side=="bid" else self.asks
        return sorted(data.items(), reverse=(side=="bid"))[:n]

    def depth_usd(self, side: str, n: int=5) -> float:
        return sum(p*q for p,q in self.top(side,n))

    def imbalance(self, n: int=5) -> float:
        b=self.depth_usd("bid",n); a=self.depth_usd("ask",n)
        return (b-a)/(b+a) if b+a>0 else 0.0

    def walk_vwap(self, side: str, notional_usd: float) -> tuple[float | None,float]:
        """side BUY consumes asks; SELL consumes bids. Returns (VWAP, filled_notional)."""
        levels=self.top("ask" if side.upper()=="BUY" else "bid",50)
        remain=max(0.0,float(notional_usd)); qty=0.0; spent=0.0
        for price,available_qty in levels:
            level_notional=price*available_qty
            take=min(remain,level_notional)
            if take<=0: break
            qty += take/price
            spent += take
            remain -= take
            if remain<=1e-9: break
        return (spent/qty if qty>0 else None,spent)
