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
from universe import scan_vol_universe, fallback_vol_universe, universe_json
from rocket_profile import settings_json as rocket_settings_json
from rocket_paper import RocketPaper

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DATA_DIR.mkdir(parents=True,exist_ok=True)
DB=DATA_DIR/"bbb.sqlite3"
DEFAULT_SYMBOLS=["SOLUSDT","ETHUSDT","BTCUSDT"]
GATES={"LIQUID":60.0,"VOL":65.0,"LAUNCH":70.0}


def purge_legacy_data_once():
    epoch=os.getenv("BBB_DATA_EPOCH","").strip()
    if not epoch:
        return
    marker=DATA_DIR/(".epoch_"+epoch)
    if marker.exists():
        return
    before=0
    try:
        for p in DATA_DIR.rglob("*"):
            if p.is_file():
                try: before+=p.stat().st_size
                except Exception: pass
        # Old research packages, caches and the old multi-strategy DB are not
        # part of the new ROCKET machine. Start the new epoch from a clean volume.
        for p in sorted(DATA_DIR.iterdir(),key=lambda x:len(str(x)),reverse=True):
            try:
                if p.is_dir():
                    import shutil; shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            except Exception as e:
                print("PURGE_FAIL",str(p),repr(e),flush=True)
        marker.write_text(epoch,encoding="utf-8")
        print("DATA_EPOCH_PURGED",epoch,"freed_bytes",before,flush=True)
    except Exception as e:
        print("DATA_EPOCH_PURGE_FATAL",repr(e),flush=True)

def runtime_storage_audit():
    try:
        rows=[]; total=0
        for p in DATA_DIR.rglob("*"):
            if p.is_file():
                z=p.stat().st_size; total+=z; rows.append((z,p))
        print("RUNTIME_STORAGE_TOTAL_BYTES",total,flush=True)
        for z,p in sorted(rows,reverse=True)[:40]:
            print("RUNTIME_STORAGE_FILE",z,str(p),flush=True)
    except Exception as e:
        print("RUNTIME_STORAGE_AUDIT_FAIL",repr(e),flush=True)

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
    purge_legacy_data_once()
    runtime_storage_audit()
    cx=connect()
    if os.getenv("BBB_RUN_WIN_DNA_ON_START","0").lower() in {"1","true","yes","on"}:
        try:
            from bbb_win_dna import run_analysis
            dna=run_analysis(DB)
            setmeta(cx,"bbb_win_dna_status",dna.get("status",""))
            setmeta(cx,"bbb_win_dna_decision",dna.get("decision",""))
            setmeta(cx,"bbb_win_dna_closed",dna.get("closed_trades",0))
            setmeta(cx,"bbb_win_dna_wins",dna.get("wins",0))
            setmeta(cx,"bbb_win_dna_losses",dna.get("losses",0))
            setmeta(cx,"bbb_win_dna_approved","1" if dna.get("approved_for_new_paper_selector") else "0")
            print("BBB_WIN_DNA_RESULT",json.dumps({
                "closed":dna.get("closed_trades"),"wins":dna.get("wins"),"losses":dna.get("losses"),
                "base_win_rate":dna.get("base_win_rate"),"split":dna.get("split"),
                "train":dna.get("train"),"holdout":dna.get("holdout"),
                "feature_count":dna.get("feature_count"),
                "top_contrasts":dna.get("top_win_loss_contrasts",[])[:12],
                "top_rules":dna.get("top_rules",[])[:12],
                "vote_portrait":dna.get("vote_portrait"),
                "approved":dna.get("approved_for_new_paper_selector"),"decision":dna.get("decision")
            },separators=(",",":")),flush=True)
            cx.commit()
        except Exception as e:
            print("BBB_WIN_DNA_FAILED",repr(e),flush=True)
    if os.getenv("BBB_RUN_MARKET_DNA_ON_START","0").lower() in {"1","true","yes","on"}:
        try:
            from bbb_market_win_dna import run_analysis as run_market_dna
            mdna=run_market_dna(DB)
            setmeta(cx,"bbb_market_dna_status",mdna.get("status",""))
            setmeta(cx,"bbb_market_dna_decision",mdna.get("decision",""))
            setmeta(cx,"bbb_market_dna_approved","1" if mdna.get("approved_for_fresh_paper_selector") else "0")
            print("BBB_MARKET_WIN_DNA_RESULT",json.dumps({
                "raw_feature_rows_scanned":mdna.get("raw_feature_rows_scanned"),
                "symbols":mdna.get("symbols"),"decision_rows":mdna.get("decision_rows"),
                "labels":mdna.get("labels"),"feature_count":mdna.get("feature_count"),
                "stable_rules":mdna.get("stable_rules",[])[:10],
                "stable_pairs":mdna.get("stable_pairs",[])[:5],
                "vote_portrait":mdna.get("vote_portrait"),
                "approved":mdna.get("approved_for_fresh_paper_selector"),
                "decision":mdna.get("decision")
            },separators=(",",":")),flush=True)
            cx.commit()
        except Exception as e:
            print("BBB_MARKET_WIN_DNA_FAILED",repr(e),flush=True)
    old=dict(cx.execute("SELECT key,value FROM meta").fetchall()).get("session_id")
    requested_session=os.getenv("BBB_SESSION_ID") or old or str(uuid.uuid4())
    # Never mix two runtime boots into the same paper accounting session. If the
    # requested session already contains trades, create a restart segment.
    prior_n=cx.execute("SELECT COUNT(*) FROM trades WHERE session_id=?",(requested_session,)).fetchone()[0]
    if prior_n:
        session=f"{requested_session}-R{int(time.time())}"
    else:
        session=requested_session

    # Publish a clean session immediately. This prevents the dashboard from
    # showing wallet/PnL counters left by a previous runtime while discovery
    # or websocket startup is still in progress.
    session_start_ms=now_ms()
    setmeta(cx,"session_id",session)
    setmeta(cx,"session_start_ms",session_start_ms)
    setmeta(cx,"heartbeat_ms",0)
    setmeta(cx,"feed_state","STARTING")
    setmeta(cx,"messages",0)
    setmeta(cx,"reconnects",0)
    setmeta(cx,"queue_depth",0)
    setmeta(cx,"dropped",0)
    setmeta(cx,"source_age_p50_ms",0)
    setmeta(cx,"source_age_p99_ms",0)
    setmeta(cx,"processing_p50_ms",0)
    setmeta(cx,"processing_p99_ms",0)
    setmeta(cx,"trading_cash_usd",os.getenv("BBB_PAPER_START_USD","500"))
    setmeta(cx,"profit_wallet_usd",0)
    setmeta(cx,"realized_pnl_usd",0)
    setmeta(cx,"fees_usd",0)
    setmeta(cx,"funding_usd",0)
    setmeta(cx,"slippage_impact_usd",0)
    setmeta(cx,"paper_open_positions",0)
    setmeta(cx,"strategy_mode","ROCKET_V1")
    setmeta(cx,"sanitizer_rejects",0)
    cx.commit()

    configured=[x.strip().upper() for x in os.getenv("BBB_SYMBOLS","").split(",") if x.strip()]
    dynamic=os.getenv("BBB_VOL_DYNAMIC_UNIVERSE","1").lower() in {"1","true","yes","on"}
    universe=[]
    universe_source="CONFIGURED"
    if dynamic:
        try:
            universe=await scan_vol_universe()
            if universe:
                universe_source="BYBIT_REST"
            else:
                universe=fallback_vol_universe()
                universe_source="FALLBACK_LIVE_POOL"
                setmeta(cx,"vol_universe_error","scanner_empty_using_live_pool")
        except Exception as e:
            universe=fallback_vol_universe()
            universe_source="FALLBACK_LIVE_POOL"
            setmeta(cx,"vol_universe_error",str(e)[:300])
    symbols=[x.symbol for x in universe] or configured or ["HYPEUSDT","GRASSUSDT","STRKUSDT","ZROUSDT"]
    bucket_map={x.symbol:x.bucket for x in universe}
    scan_meta={x.symbol:x for x in universe}
    launch_symbols=set()
    liquid_symbols=set()
    setmeta(cx,"session_id",session); setmeta(cx,"session_start_ms",session_start_ms); setmeta(cx,"mode",os.getenv("BBB_MODE","PAPER"))
    setmeta(cx,"symbols",",".join(symbols)); setmeta(cx,"launch_symbols","")
    setmeta(cx,"liquid_symbols",""); setmeta(cx,"vol_universe_json",universe_json(universe))
    setmeta(cx,"vol_dynamic","1" if dynamic else "0")
    setmeta(cx,"vol_universe_source",universe_source)
    setmeta(cx,"runtime_base_session",requested_session)
    setmeta(cx,"paper_start_usd",os.getenv("BBB_PAPER_START_USD","500"))
    setmeta(cx,"rocket_settings_json",rocket_settings_json())
    # Any open rows from an older paper session are bookkeeping leftovers after a deliberate reset.
    cx.execute("""UPDATE trades SET status='ABANDONED', exit_reason='SESSION_RESET', closed_ms=?
                  WHERE status='OPEN' AND session_id<>?""",(now_ms(),session))
    cx.commit()
    rocket=RocketPaper(cx,session,float(os.getenv("BBB_PAPER_START_USD","500")))
    rocket_entries_enabled=os.getenv("BBB_ROCKET_ENTRY_ENABLED","1").lower() in {"1","true","yes","on"}
    setmeta(cx,"rocket_entries_enabled","1" if rocket_entries_enabled else "0")

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

                    # ROCKET_V1 is the only active trading machine.
                    score=baseline_score(snap); gate=65.0
                    state=f"{bucket}|{'ACTIVE' if trade_now else 'WAIT'}"

                    if rocket_entries_enabled:
                        rocket.on_snapshot(symbol,snap,books[symbol],books[symbol].exchange_ts_ms)
                    else:
                        # Old ROCKET entry logic is retired while the replacement WIN-DNA selector
                        # is researched. Keep live market telemetry, but create no new positions.
                        for held_symbol in list(rocket.positions.keys()):
                            if held_symbol==symbol:
                                rocket._manage(symbol,snap,books[symbol],books[symbol].exchange_ts_ms)
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
                        universe_source="BYBIT_REST"
                        new_symbols=[x.symbol for x in fresh]
                        for s in new_symbols:
                            if s not in books: books[s]=LocalBook(s)
                        bucket_map={x.symbol:x.bucket for x in fresh}
                        scan_meta={x.symbol:x for x in fresh}
                        universe=fresh
                        held=set(rocket.positions.keys())|set(rocket.arms.keys())
                        feed.set_symbols(set(new_symbols)|held)
                        symbols=new_symbols
                        setmeta(cx,"symbols",",".join(new_symbols))
                        setmeta(cx,"vol_universe_json",universe_json(fresh))
                        setmeta(cx,"vol_universe_updated_ms",now_ms())
                        setmeta(cx,"vol_universe_source",universe_source)
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
                # Session-aware retention:
                # - NEVER delete heavy telemetry from the active session.
                # - After a session changes, keep its heavy telemetry for retention_hours.
                # - trades and wallet_ledger are never deleted here, so compact session results
                #   remain available for long-term analysis after raw telemetry expires.
                if now_ms()-last_maintenance >= 60000:
                    cutoff=now_ms()-retention_hours*3600*1000
                    cx.execute("DELETE FROM events WHERE session_id<>? AND ts_ms < ?",(session,cutoff))
                    cx.execute("DELETE FROM feature_samples WHERE session_id<>? AND ts_ms < ?",(session,cutoff))
                    cx.execute("DELETE FROM equity_samples WHERE session_id<>? AND ts_ms < ?",(session,cutoff))
                    setmeta(cx,"retention_policy","ACTIVE_SESSION_FULL; PRIOR_SESSIONS_RAW_%sh; TRADES_LEDGER_KEEP" % retention_hours)
                    setmeta(cx,"retention_last_cleanup_ms",now_ms())
                    # Do not checkpoint from the hot writer loop: dashboard readers can
                    # legitimately hold read locks. WAL autocheckpoint handles this safely.
                    last_maintenance=now_ms()
                cx.commit(); last_hb=now_ms()
    finally:
        stop.set(); await feed_task; cx.commit(); cx.close()

if __name__=="__main__":
    asyncio.run(main())
