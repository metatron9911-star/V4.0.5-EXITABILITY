"""ROCKET V1 operational dashboard."""
from __future__ import annotations
import json, os, sqlite3, time
from pathlib import Path
import streamlit as st

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DB=DATA_DIR/"bbb.sqlite3"

st.set_page_config(page_title="BBB — ROCKET V1",layout="wide")
st.title("BBB — ROCKET V1")
st.caption("PAPER only · live Bybit publicTrade + L50 VWAP · high-volatility / tight-spread / dense-event universe")

def open_db():
    return sqlite3.connect(DB,check_same_thread=False) if DB.exists() else None

def parse_json(v):
    try:return json.loads(v or "{}")
    except Exception:return {}

@st.fragment(run_every="2s")
def board():
    db=open_db()
    if not db:
        st.warning("Waiting for ROCKET database...")
        return

    meta=dict(db.execute("SELECT key,value FROM meta").fetchall())
    now=int(time.time()*1000)
    session=meta.get("session_id","")
    hb=int(meta.get("heartbeat_ms","0") or 0)
    feed=meta.get("feed_state","STARTING")
    green=bool(hb and now-hb<10000 and feed=="CONNECTED")
    icon="🟢" if green else "🔴"
    st.markdown(f"{icon} **BYBIT PUBLIC WS {feed}** · messages {meta.get('messages','0')} · reconnects {meta.get('reconnects','0')} · queue {meta.get('queue_depth','0')}")

    start=int(meta.get("session_start_ms","0") or 0)
    elapsed=max(0,(now-start)//1000) if start else 0
    cash=float(meta.get("rocket_cash_usd","500") or 0)
    st.markdown(f"**ROCKET PAPER wallet USD {cash:.2f}** · Session {session} · {elapsed//3600:02d}:{(elapsed%3600)//60:02d}:{elapsed%60:02d}")

    closed=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='ROCKET_V1'",(session,)).fetchone()[0]
    wins=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='ROCKET_V1' AND net_pnl_usd>0",(session,)).fetchone()[0]
    losses=closed-wins
    open_n=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='OPEN' AND risk_state='ROCKET_V1'",(session,)).fetchone()[0]
    wr=100*wins/closed if closed else 0.0

    arms=db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND stage='ROCKET_ARM'",(session,)).fetchone()[0]
    confirms=db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND stage='ROCKET_CONFIRM'",(session,)).fetchone()[0]
    rejects=db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND stage='ROCKET_REJECT'",(session,)).fetchone()[0]
    entryfills=db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND stage='ROCKET_FILL' AND reason='ENTRY'",(session,)).fetchone()[0]
    exitfills=db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND stage='ROCKET_FILL' AND reason='EXIT'",(session,)).fetchone()[0]

    a=st.columns(8)
    a[0].metric("ROCKET PAPER","ON")
    a[1].metric("Closed",closed)
    a[2].metric("W / L",f"{wins} / {losses}")
    a[3].metric("WR",f"{wr:.1f}%")
    a[4].metric("Realized $",f"{float(meta.get('rocket_realized_usd','0') or 0):.4f}")
    a[5].metric("Fees $",f"{float(meta.get('rocket_fees_usd','0') or 0):.4f}")
    a[6].metric("Open",open_n)
    a[7].metric("Execution","LIVE L50")

    b=st.columns(5)
    b[0].metric("Breakout arms",arms)
    b[1].metric("Confirmed",confirms)
    b[2].metric("Rejected",rejects)
    b[3].metric("Entry fills",entryfills)
    b[4].metric("Exit fills",exitfills)

    st.subheader("Selected high-volatility universe")
    try: uni=json.loads(meta.get("vol_universe_json","[]"))
    except Exception: uni=[]
    if uni:
        st.dataframe([{
            "symbol":x.get("symbol"),
            "range24h_%":round(float(x.get("range_bps") or 0)/100,2),
            "change24h_%":round(float(x.get("abs_change_bps") or 0)/100,2),
            "turnover24h_$":round(float(x.get("turnover24h") or 0),0)
        } for x in uni],hide_index=True,use_container_width=True)
    else:
        st.caption("Waiting for universe scanner...")

    rows=db.execute("""SELECT symbol,ts_ms,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                      trade_count_1s,trade_count_5s,buy_ratio_1s,ret_250ms_bps,ret_1s_bps,
                      ret_5s_bps,realized_vol_5s_bps,payload_json
                      FROM feature_samples WHERE session_id=? AND id IN
                      (SELECT MAX(id) FROM feature_samples WHERE session_id=? GROUP BY symbol)
                      ORDER BY symbol""",(session,session)).fetchall()
    st.subheader("Live ROCKET gate")
    live=[]
    for r in rows:
        p=parse_json(r[12])
        live.append({
            "symbol":r[0],"age_ms":now-r[1],
            "spread_bps":round(float(r[2] or 0),2),
            "depth5_min_$":round(min(float(r[3] or 0),float(r[4] or 0)),0),
            "trades_1s":r[5],"trades_5s":r[6],
            "buy_ratio":round(float(r[7] or 0),3),
            "ret250_bps":round(float(r[8] or 0),2),
            "ret1s_bps":round(float(r[9] or 0),2),
            "ret5s_bps":round(float(r[10] or 0),2),
            "rv5s_bps":round(float(r[11] or 0),2),
            "range24h_%":round(float(p.get("range24h_bps") or 0)/100,2)
        })
    if live:
        st.dataframe(live,hide_index=True,use_container_width=True)
    else:
        st.caption("Waiting for live samples...")

    trades=db.execute("""SELECT symbol,side,status,entry_vwap,exit_vwap,net_pnl_usd,exit_reason,
                         opened_ms,closed_ms,entry_features_json FROM trades
                         WHERE session_id=? AND risk_state='ROCKET_V1'
                         ORDER BY id DESC LIMIT 50""",(session,)).fetchall()
    st.subheader("ROCKET trades")
    report=[]
    for r in trades:
        ef=parse_json(r[9])
        report.append({
            "symbol":r[0],"side":r[1],"status":r[2],
            "entry":r[3],"exit":r[4],
            "net_$":round(float(r[5] or 0),5),
            "reason":r[6],
            "hold_s":round((((r[8] or now)-(r[7] or now))/1000),2),
            "arm_tape_x":round(float(ef.get("arm_tape_ratio",0) or 0),2),
            "confirm_tape_x":round(float(ef.get("confirm_tape_ratio",0) or 0),2),
            "best_beyond_bps":round(float(ef.get("best_beyond_bps",0) or 0),2)
        })
    if report:
        st.dataframe(report,hide_index=True,use_container_width=True)
    else:
        st.caption("No ROCKET fills yet.")

board()
