"""BBB Streamlit dashboard — feed/feature observability only."""
from __future__ import annotations
import json, os, sqlite3, time
from pathlib import Path
import streamlit as st

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DB=DATA_DIR/"bbb.sqlite3"

st.set_page_config(page_title="BBB — BybitBot Beta",layout="wide")
st.title("BBB — BYBIT BOT BETA")
st.caption("PAPER / SHADOW ONLY · no live order submission path")

def cx():
    return sqlite3.connect(DB,check_same_thread=False) if DB.exists() else None

@st.fragment(run_every="2s")
def board():
    db=cx()
    if not db:
        st.warning("Waiting for BBB database…"); return
    now=int(time.time()*1000)
    meta=dict(db.execute("SELECT key,value FROM meta").fetchall())
    hb=int(meta.get("heartbeat_ms","0") or 0)
    feed_state=meta.get("feed_state","STARTING")
    reconnects=meta.get("reconnects","0")
    msgs=meta.get("messages","0")
    qdepth=meta.get("queue_depth","0")
    src_p50=meta.get("source_age_p50_ms","—")
    src_p99=meta.get("source_age_p99_ms","—")
    proc_p50=meta.get("processing_p50_ms","—")
    proc_p99=meta.get("processing_p99_ms","—")
    green=hb and now-hb<10000 and feed_state=="CONNECTED"
    st.markdown(("🟢" if green else "🔴")+f" **BYBIT PUBLIC WS {feed_state}** | messages {msgs} | reconnects {reconnects} | queue {qdepth} | src p50/p99 {src_p50}/{src_p99} ms | proc p50/p99 {proc_p50}/{proc_p99} ms")

    rows=db.execute("""SELECT symbol,head,ts_ms,mid,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                      book_imbalance_5,trade_count_1s,buy_ratio_1s,ret_1s_bps,realized_vol_5s_bps,
                      score,gate,state
                      FROM feature_samples WHERE id IN
                      (SELECT MAX(id) FROM feature_samples GROUP BY symbol)
                      ORDER BY head,symbol""").fetchall()
    st.subheader("Heads / signals")
    st.dataframe([{
        "symbol":r[0],"head":r[1],"age_ms":now-r[2],"mid":r[3],"spread_bps":round(r[4] or 0,3),
        "bid_depth5_$":round(r[5] or 0,0),"ask_depth5_$":round(r[6] or 0,0),
        "imbalance":round(r[7] or 0,3),"trades_1s":r[8],"buy_ratio":round(r[9] or 0,3),
        "ret_1s_bps":round(r[10] or 0,2),"rv_5s_bps":round(r[11] or 0,2),
        "score":round(r[12] or 0,1),"gate":r[13],"state":r[14]
    } for r in rows],hide_index=True,width="stretch")

    st.subheader("Machine state")
    cols=st.columns(6)
    cols[0].metric("Mode",meta.get("mode","PAPER"))
    cols[1].metric("Session",meta.get("session_id","—")[:12])
    cols[2].metric("Symbols",meta.get("symbols","—"))
    cols[3].metric("DB events",db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    cols[4].metric("Feature samples",db.execute("SELECT COUNT(*) FROM feature_samples").fetchone()[0])
    cols[5].metric("Open positions",db.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0])

    st.subheader("Wallets")
    start=float(meta.get("paper_start_usd","500"))
    st.write({"TRADING WALLET":start,"PROFIT WALLET":0.0,"note":"Phase A feed-only: trading disabled"})

board()
