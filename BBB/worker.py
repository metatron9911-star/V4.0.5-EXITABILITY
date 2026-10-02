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
from paper import PaperTrader
from universe import scan_vol_universe, universe_json

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

def live_vol_state(snap:dict,rest_bucket:str) -> tuple[str,bool,float]:
    """Classify present-tense speed/jumpiness. 24h stats choose the pool;
    these microstructure features decide whether we trade it right now."""
    t1=int(snap.get("trade_count_1s",0) or 0)
    t5=int(snap.get("trade_count_5s",0) or 0)
    rv=abs(float(snap.get("realized_vol_5s_bps",0) or 0))
    r250=abs(float(snap.get("ret_250ms_bps",0) or 0))
    r1=abs(float(snap.get("ret_1s_bps",0) or 0))
    r5=abs(float(snap.get("ret_5s_bps",0) or 0))
    impulse=max(r250,r1,0.5*r5)
    velocity=t1 + 0.20*t5
    if (impulse>=8 or rv>=0.80) and t5>=3:
        bucket="EXTREME"
        active=True
    elif (impulse>=3 or rv>=0.25) and (t1>=1 or t5>=6):
        bucket="FAST"
        active=True
    else:
        bucket="CORE" if rest_bucket=="CORE" else rest_bucket
        active=(rest_bucket=="CORE" and (t1>=3 or t5>=12) and (impulse>=1.5 or rv>=0.12))
    activity_score=velocity + impulse*2.0 + rv*10.0
    return bucket,active,activity_score


async def main():
    cx=connect()
    old=dict(cx.execute("SELECT key,value FROM meta").fetchall()).get("session_id")
    session=os.getenv("BBB_SESSION_ID") or old or str(uuid.uuid4())
    configured=[x.strip().upper() for x in os.getenv("BBB_SYMBOLS","").split(",") if x.strip()]
    dynamic=os.getenv("BBB_VOL_DYNAMIC_UNIVERSE","1").lower() in {"1","true","yes","on"}
    universe=[]
    if dynamic:
        try:
            universe=await scan_vol_universe()
            if not universe: setmeta(cx,"vol_universe_error","scanner_empty_fallback")
        except Exception as e:
            universe=[]
            setmeta(cx,"vol_universe_error",str(e)[:300])
    symbols=[x.symbol for x in universe] or configured or ["HYPEUSDT","GRASSUSDT","STRKUSDT","ZROUSDT"]
    bucket_map={x.symbol:x.bucket for x in universe}
    scan_meta={x.symbol:x for x in universe}
    launch_symbols=set()
    liquid_symbols=set()
    setmeta(cx,"session_id",session); setmeta(cx,"mode",os.getenv("BBB_MODE","PAPER"))
    setmeta(cx,"symbols",",".join(symbols)); setmeta(cx,"launch_symbols","")
    setmeta(cx,"liquid_symbols",""); setmeta(cx,"vol_universe_json",universe_json(universe))
    setmeta(cx,"vol_dynamic","1" if dynamic else "0")
    setmeta(cx,"paper_start_usd",os.getenv("BBB_PAPER_START_USD","500"))
    # Any open rows from an older paper session are bookkeeping leftovers after a deliberate reset.
    cx.execute("""UPDATE trades SET status='ABANDONED', exit_reason='SESSION_RESET', closed_ms=?
                  WHERE status='OPEN' AND session_id<>?""",(now_ms(),session))
    cx.commit()
    trader=PaperTrader(cx,session,float(os.getenv("BBB_PAPER_START_USD","500")),launch_symbols)

    q=asyncio.Queue(maxsize=int(os.getenv("BBB_MAX_QUEUE","100000")))
    stop=asyncio.Event(); books={s:LocalBook(s) for s in symbols}; fx=FeatureEngine()
    source_ages=deque(maxlen=10000); processing=deque(maxlen=10000)
    last_sample=defaultdict(int); last_market_sample=defaultdict(int); dropped=0
    feature_persist_ms=int(os.getenv("BBB_FEATURE_PERSIST_MS","1000"))
    market_persist_ms=int(os.getenv("BBB_MARKET_PERSIST_MS","5000"))
    retention_hours=int(os.getenv("BBB_RETENTION_HOURS","24"))
    last_maintenance=0
    scan_interval_ms=int(os.getenv("BBB_VOL_SCAN_INTERVAL_SEC","900"))*1000
    last_universe_scan=now_ms()

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

            # Do not persist every exchange message: Bybit can emit thousands/sec.
            # Keep a lightweight sampled evidence trail; latency counters remain in memory/meta.
            if now_ms()-last_market_sample[symbol] >= market_persist_ms:
                cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                              VALUES(?,?,?,?,?,?,?,?)""",
                           (recv,exch,session,None,symbol,"MARKET_SAMPLE",topic,
                            json.dumps({"type":ev.get("type"),"receive_ts_ms":recv},separators=(",",":"))))
                last_market_sample[symbol]=now_ms()

            if symbol in books and now_ms()-last_sample[symbol]>=feature_persist_ms:
                snap=fx.snapshot(symbol,now_ms(),books[symbol])
                if snap:
                    head="VOL"
                    rest_bucket=bucket_map.get(symbol,"FAST")
                    bucket,trade_now,activity_score=live_vol_state(snap,rest_bucket)
                    meta_scan=scan_meta.get(symbol)
                    if meta_scan:
                        snap["turnover24h"]=meta_scan.turnover24h
                        snap["range24h_bps"]=meta_scan.range_bps
                        snap["abs_change24h_bps"]=meta_scan.abs_change_bps
                    snap["vol_bucket"]=bucket
                    snap["rest_bucket"]=rest_bucket
                    snap["live_activity_score"]=activity_score
                    snap["live_trade_ok"]=1 if trade_now else 0
                    score=baseline_score(snap); gate=65.0
                    state=f"{bucket}|{'ACTIVE' if trade_now else 'WAIT'}"
                    trader.on_snapshot(symbol,head,snap,books[symbol],books[symbol].exchange_ts_ms,recv,bucket=bucket,allow_new_entries=trade_now)
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

            # Refresh the traded universe periodically. Existing open positions remain managed;
            # new subscriptions are added/removed live.
            if dynamic and now_ms()-last_universe_scan>=scan_interval_ms:
                try:
                    fresh=await scan_vol_universe()
                    if fresh:
                        new_symbols=[x.symbol for x in fresh]
                        for s in new_symbols:
                            if s not in books: books[s]=LocalBook(s)
                        bucket_map={x.symbol:x.bucket for x in fresh}
                        scan_meta={x.symbol:x for x in fresh}
                        universe=fresh
                        held={p.symbol for p in trader.book.positions.values()}
                        feed.set_symbols(set(new_symbols)|held)
                        symbols=new_symbols
                        setmeta(cx,"symbols",",".join(new_symbols))
                        setmeta(cx,"vol_universe_json",universe_json(fresh))
                        setmeta(cx,"vol_universe_updated_ms",now_ms())
                        setmeta(cx,"vol_universe_error","")
                except Exception as e:
                    setmeta(cx,"vol_universe_error",str(e)[:300])
                last_universe_scan=now_ms()

            if now_ms()-last_hb>=1000:
                setmeta(cx,"heartbeat_ms",now_ms()); setmeta(cx,"feed_state","CONNECTED" if feed.health.connected else "RECONNECTING")
                setmeta(cx,"reconnects",feed.health.reconnects); setmeta(cx,"messages",feed.health.messages)
                setmeta(cx,"queue_depth",q.qsize()); setmeta(cx,"dropped",dropped)
                setmeta(cx,"source_age_p50_ms",round(pct(source_ages,.50),2)); setmeta(cx,"source_age_p99_ms",round(pct(source_ages,.99),2))
                setmeta(cx,"processing_p50_ms",round(pct(processing,.50),2)); setmeta(cx,"processing_p99_ms",round(pct(processing,.99),2))
                # Rolling retention prevents telemetry from filling the Railway volume.
                if now_ms()-last_maintenance >= 60000:
                    cutoff=now_ms()-retention_hours*3600*1000
                    cx.execute("DELETE FROM events WHERE ts_ms < ?",(cutoff,))
                    cx.execute("DELETE FROM feature_samples WHERE ts_ms < ?",(cutoff,))
                    cx.execute("DELETE FROM equity_samples WHERE ts_ms < ?",(cutoff,))
                    # Do not checkpoint from the hot writer loop: dashboard readers can
                    # legitimately hold read locks. WAL autocheckpoint handles this safely.
                    last_maintenance=now_ms()
                cx.commit(); last_hb=now_ms()
    finally:
        stop.set(); await feed_task; cx.commit(); cx.close()

if __name__=="__main__":
    asyncio.run(main())
