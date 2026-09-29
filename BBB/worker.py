"""BBB feed-only worker scaffold.

Phase A intentionally does not create positions. It proves public-feed health and
records normalized events before strategy code is enabled.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import uuid

from market_bybit import BybitPublicFeed, now_ms

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "bbb.sqlite3"
SESSION_ID = os.getenv("BBB_SESSION_ID") or str(uuid.uuid4())

DEFAULT_SYMBOLS = ["SOLUSDT", "ETHUSDT", "BTCUSDT"]


def connect() -> sqlite3.Connection:
    cx = sqlite3.connect(DB)
    cx.execute("PRAGMA journal_mode=WAL")
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    cx.executescript(schema)
    cx.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('session_id',?)", (SESSION_ID,))
    cx.commit()
    return cx


async def main() -> None:
    cx = connect()
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=100000)
    stop = asyncio.Event()

    async def on_event(ev: dict) -> None:
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            # Phase A must surface loss rather than block the network reader.
            pass

    symbols = [x.strip().upper() for x in os.getenv("BBB_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if x.strip()]
    feed = BybitPublicFeed(symbols, on_event)
    feed_task = asyncio.create_task(feed.run(stop))

    try:
        while True:
            ev = await q.get()
            cx.execute(
                """INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    now_ms(),
                    ev.get("exchange_ts_ms"),
                    SESSION_ID,
                    None,
                    None,
                    "MARKET",
                    ev.get("topic"),
                    json.dumps(ev, separators=(",", ":"), ensure_ascii=False),
                ),
            )
            if cx.total_changes % 250 == 0:
                cx.commit()
    finally:
        stop.set()
        await feed_task
        cx.commit()
        cx.close()


if __name__ == "__main__":
    asyncio.run(main())
