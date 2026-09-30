"""BBB Phase A worker: Bybit public feed + local L50 + feature telemetry.

PAPER/SHADOW only. No exchange private API, signer or order endpoint.
"""
from __future__ import annotations
import asyncio, json, os, sqlite3, statistics, time, uuid
from collections import defaultdict, deque
from pathlib import Path

from market_bybit import BybitPublicFeed, now_ms
from orderbook import LocalBook
from features import FeatureEngine, classify_head, baseline_score

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DATA_DIR.mkdir(parents=True,exist_ok=True)
DB=DATA_DIR/"bbb.sqlite3"
DEFAULT_SYMBOLS=["SOLUSDT","ETHUSDT","BTCUSDT"]
GATES={"LIQUID":60.0,"VOL":65.0,"LAUNCH":70.0}


def connect():
    cx=sqlite3.connect(DB,check_same_thread=False,timeout=15)
    cx.execute("PRAGMA journal_mode=WAL")
    cx.executescript((ROOT/"schema.sql").read_text(encoding="utf-8"))
    return cx


def setmeta(cx,key,value):
    cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(key,str(value)))


def pct(values,p):
    if not values:return 0.0
    arr=sorted(values); i=min(len(arr)-1,max(0,int(round((len(arr)-1)*p))))
    return float(arr[i])


async def main():
    cx=connect()
    old=dict(cx.execute("SELECT key,value FROM meta").fetchall()).get("session_id")
    session=os.getenv("BBB_SESSION_ID") or old or str(uuid.uuid4())
    symbols=[x.strip().upper() for x in os.getenv("BBB_SYMBOLS",",".join(DEFAULT_SYMBOLS)).split(",") if x.strip()]
    launch_symbols={x.strip().upper() for x in os.getenv("BBB_LAUNCH_SYMBOLS","").split(",") if x.strip()}
    liquid_symbols={x.strip().upper() for x in os.getenv("BBB_LIQUID_SYMBOLS","BTCUSDT,ETHUSDT,SOLUSDT").split(",") if x.strip()}
    setmeta(cx,"session_id",session); setmeta(cx,"mode",os.getenv("BBB_MODE","PAPER"))
    setmeta(cx,"symbols",",".join(symbols)); setmeta(cx,"launch_symbols",",".join(sorted(launch_symbols)))
    setmeta(cx,"liquid_symbols",",".join(sorted(liquid_symbols)))
    setmeta(cx,"paper_start_usd",os.getenv("BBB_PAPER_START_USD","500"))
    cx.commit()

    q=asyncio.Queue(maxsize=int(os.getenv("BBB_MAX_QUEUE","100000")))
    stop=asyncio.Event(); books={s:LocalBook(s) for s in symbols}; fx=FeatureEngine()
    source_ages=deque(maxlen=10000); processing=deque(maxlen=10000)
    last_sample=defaultdict(int); dropped=0

    async def on_event(ev):
        nonlocal dropped
        try:q.put_nowait(ev)
        except asyncio.QueueFull:dropped+=1

    feed=BybitPublicFeed(symbols,on_event)
    feed_task=asyncio.create_task(feed.run(stop))
    last_hb=0
    try:
        while True:
            ev=await q.get(); recv=ev["receive_ts_ms"]; exch=int(ev.get("exchange_ts_ms") or 0)
            if exch>0: source_ages.append(max(0,recv-exch))
            topic=ev.get("topic",""); data=ev.get("data"); symbol=topic.rsplit(".",1)[-1]
            start=now_ms()

            if topic.startswith("publicTrade.") and isinstance(data,list):
                for t in data:
                    try:fx.on_trade(symbol,int(t.get("T") or exch or recv),float(t["p"]),float(t["v"]),str(t.get("S") or ""))
                    except Exception:pass
            elif topic.startswith("orderbook.") and isinstance(data,dict) and symbol in books:
                books[symbol].apply(ev.get("type"),data,exch)
                fx.on_book(symbol,int(data.get("cts") or exch or recv),books[symbol])

            # Persist light normalized market evidence, not the full raw message.
            cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (recv,exch,session,None,symbol,"MARKET",topic,
                        json.dumps({"type":ev.get("type"),"receive_ts_ms":recv},separators=(",",":"))))

            if symbol in books and now_ms()-last_sample[symbol]>=250:
                snap=fx.snapshot(symbol,now_ms(),books[symbol])
                if snap:
                    head="LAUNCH" if symbol in launch_symbols else ("LIQUID" if symbol in liquid_symbols else "VOL")
                    score=baseline_score(snap); gate=GATES[head]
                    state="SHADOW_PASS" if score>=gate else "GATED"
                    cx.execute("""INSERT INTO feature_samples(
                      ts_ms,exchange_ts_ms,session_id,symbol,head,mid,bid,ask,spread_bps,
                      depth_bid_5_usd,depth_ask_5_usd,book_imbalance_5,trade_count_1s,trade_count_5s,
                      buy_ratio_1s,ret_250ms_bps,ret_1s_bps,ret_5s_bps,realized_vol_5s_bps,
                      score,gate,state,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (now_ms(),books[symbol].exchange_ts_ms,session,symbol,head,snap["mid"],snap["bid"],snap["ask"],
                       snap["spread_bps"],snap["depth_bid_5_usd"],snap["depth_ask_5_usd"],snap["book_imbalance_5"],
                       snap["trade_count_1s"],snap["trade_count_5s"],snap["buy_ratio_1s"],snap["ret_250ms_bps"],
                       snap["ret_1s_bps"],snap["ret_5s_bps"],snap["realized_vol_5s_bps"],score,gate,state,
                       json.dumps(snap,separators=(",",":"))))
                    last_sample[symbol]=now_ms()

            processing.append(max(0,now_ms()-start))
            if now_ms()-last_hb>=1000:
                setmeta(cx,"heartbeat_ms",now_ms()); setmeta(cx,"feed_state","CONNECTED" if feed.health.connected else "RECONNECTING")
                setmeta(cx,"reconnects",feed.health.reconnects); setmeta(cx,"messages",feed.health.messages)
                setmeta(cx,"queue_depth",q.qsize()); setmeta(cx,"dropped",dropped)
                setmeta(cx,"source_age_p50_ms",round(pct(source_ages,.50),2)); setmeta(cx,"source_age_p99_ms",round(pct(source_ages,.99),2))
                setmeta(cx,"processing_p50_ms",round(pct(processing,.50),2)); setmeta(cx,"processing_p99_ms",round(pct(processing,.99),2))
                cx.commit(); last_hb=now_ms()
    finally:
        stop.set(); await feed_task; cx.commit(); cx.close()

if __name__=="__main__":
    asyncio.run(main())
