"""Bybit public-market adapter for BBB PAPER.

No API key and no order endpoint are used here.
"""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

import websockets

BYBIT_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class FeedHealth:
    connected: bool = False
    reconnects: int = 0
    messages: int = 0
    dropped: int = 0
    last_receive_ms: int = 0
    last_exchange_ms: int = 0

    @property
    def feed_age_ms(self) -> int | None:
        if not self.last_receive_ms or not self.last_exchange_ms:
            return None
        return max(0, self.last_receive_ms - self.last_exchange_ms)


class BybitPublicFeed:
    def __init__(self, symbols: Iterable[str], on_event: Callable[[dict], Awaitable[None]]):
        self.symbols = sorted({s.upper() for s in symbols})
        self.desired_symbols = set(self.symbols)
        self.on_event = on_event
        self.health = FeedHealth()

    def set_symbols(self, symbols: Iterable[str]) -> None:
        self.desired_symbols = {s.upper() for s in symbols}

    def topics_for(self, symbols: Iterable[str]) -> list[str]:
        out = []
        for s in sorted(set(symbols)):
            out.extend([
                f"publicTrade.{s}",
                f"orderbook.1.{s}",
                f"orderbook.50.{s}",
                f"tickers.{s}",
            ])
        return out

    def topics(self) -> list[str]:
        return self.topics_for(self.symbols)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(
                    BYBIT_LINEAR_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=10000,
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": self.topics()}))
                    self.health.connected = True
                    backoff = 1.0
                    while not stop.is_set():
                        # Apply universe changes without restarting the service.
                        desired=set(self.desired_symbols)
                        current=set(self.symbols)
                        add=desired-current; remove=current-desired
                        if add:
                            await ws.send(json.dumps({"op":"subscribe","args":self.topics_for(add)}))
                        if remove:
                            await ws.send(json.dumps({"op":"unsubscribe","args":self.topics_for(remove)}))
                        if add or remove:
                            self.symbols=sorted(desired)
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        recv_ms = now_ms()
                        msg = json.loads(raw)
                        if "topic" not in msg:
                            continue
                        self.health.messages += 1
                        self.health.last_receive_ms = recv_ms

                        exchange_ms = int(msg.get("ts") or 0)
                        data = msg.get("data")
                        if isinstance(data, dict):
                            exchange_ms = int(data.get("cts") or data.get("ts") or exchange_ms or 0)
                        elif isinstance(data, list) and data:
                            exchange_ms = int(data[-1].get("T") or exchange_ms or 0)

                        self.health.last_exchange_ms = exchange_ms
                        await self.on_event({
                            "venue": "BYBIT",
                            "receive_ts_ms": recv_ms,
                            "exchange_ts_ms": exchange_ms,
                            "topic": msg["topic"],
                            "type": msg.get("type"),
                            "data": data,
                            "raw": msg,
                        })
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.connected = False
                self.health.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(20.0, backoff * 2.0)
        self.health.connected = False
