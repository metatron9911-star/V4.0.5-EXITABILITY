"""ROCKET V1 operational dashboard."""
# NIXPACKS DEPLOY RETRY
from __future__ import annotations
import json, os, sqlite3, time
from pathlib import Path
import streamlit as st

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DB=DATA_DIR/"bbb.sqlite3"

st.set_page_config(page_title="BBB — MARKET DNA",layout="wide")
st.title("BBB — CORE / HEALTH")
st.caption("PAPER only · live Bybit publicTrade + L50 VWAP · WIN portrait from BBB own Bybit history · legacy ROCKET entries OFF")

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
    st.caption(f"Storage: {meta.get('storage_mode','CORE_ONLY')} · persistence {meta.get('market_persist_enabled','0')} · guard {meta.get('storage_guard_state','OK')} · bytes {meta.get('storage_used_bytes','0')}")
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
    active=meta.get("active_strategy","MARKET_DNA_V1")
    mdna_cash=float(meta.get("market_dna_cash_usd","500") or 0)
    st.markdown(f"**{active} PAPER wallet USD {mdna_cash:.2f}** · Session {session} · {elapsed//3600:02d}:{(elapsed%3600)//60:02d}:{elapsed%60:02d}")
    st.caption(f"Selector: {meta.get('market_dna_selector','T5>=203 & ACTIVITY>=117.02 & D10_T5>=63')} · Exit: {meta.get('market_dna_exit','TP80bps_STOP30bps_TIMEOUT20s')}")

    if meta.get("committee_paper_enabled","0")=="1":
        st.subheader("Expert Committee V1 — PAPER")
        c_closed=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='COMMITTEE_V1'",(session,)).fetchone()[0]
        c_wins=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='COMMITTEE_V1' AND net_pnl_usd>0",(session,)).fetchone()[0]
        c_losses=c_closed-c_wins
        c_open=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='OPEN' AND risk_state='COMMITTEE_V1'",(session,)).fetchone()[0]
        c_wr=100*c_wins/c_closed if c_closed else 0.0
        cc=st.columns(8)
        cc[0].metric("Committee","ON")
        cc[1].metric("Closed",c_closed)
        cc[2].metric("W / L",f"{c_wins} / {c_losses}")
        cc[3].metric("WR",f"{c_wr:.1f}%")
        cc[4].metric("Realized $",f"{float(meta.get('committee_realized_usd','0') or 0):.4f}")
        cc[5].metric("Fees $",f"{float(meta.get('committee_fees_usd','0') or 0):.4f}")
        cc[6].metric("Open",c_open)
        cc[7].metric("Approved / Signals",f"{meta.get('committee_approved','0')} / {meta.get('committee_signals','0')}")
        st.caption(f"Experts: IMPULSE 40% · ABSORPTION 30% · TAPE 20% · BOOK 10% · absolute RISK VETO · {meta.get('committee_selector','')}")
        st.caption(f"Exit: {meta.get('committee_exit','')}")
        ctr=db.execute("""SELECT symbol,status,entry_vwap,exit_vwap,net_pnl_usd,exit_reason,
                                 hold_ms,notional_usd,entry_features_json
                          FROM trades WHERE session_id=? AND risk_state='COMMITTEE_V1'
                          ORDER BY id DESC LIMIT 50""",(session,)).fetchall()
        crep=[]
        for r in ctr:
            ef=parse_json(r[8]); d=ef.get("committee_v1") or {}
            n=float(r[7] or 0); net=float(r[4] or 0)
            crep.append({
              "symbol":r[0],"status":r[1],"entry":r[2],"exit":r[3],
              "net_$":round(net,5),"net_bps":round(net/n*10000,2) if n else None,
              "reason":r[5],"hold_s":round(float(r[6] or 0)/1000,3),
              "regime":d.get("regime"),"weighted":round(float(d.get("weighted",0) or 0),3),
              "IMP":round(float(d.get("impulse",0) or 0),3),
              "ABS":round(float(d.get("absorption",0) or 0),3),
              "TAPE":round(float(d.get("tape",0) or 0),3),
              "BOOK":round(float(d.get("book",0) or 0),3)
            })
        if crep: st.dataframe(crep,hide_index=True,width='stretch')
        else: st.caption("Waiting for the committee's first approved PAPER BUY…")
        st.divider()

    md_closed=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='MARKET_DNA_V1'",(session,)).fetchone()[0]
    md_wins=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND risk_state='MARKET_DNA_V1' AND net_pnl_usd>0",(session,)).fetchone()[0]
    md_losses=md_closed-md_wins
    md_open=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='OPEN' AND risk_state='MARKET_DNA_V1'",(session,)).fetchone()[0]
    md_wr=100*md_wins/md_closed if md_closed else 0.0
    md=st.columns(8)
    md[0].metric("MARKET DNA","ON" if meta.get("market_dna_entries_enabled","0")=="1" else "OFF")
    md[1].metric("Closed",md_closed)
    md[2].metric("W / L",f"{md_wins} / {md_losses}")
    md[3].metric("WR",f"{md_wr:.1f}%")
    md[4].metric("Realized $",f"{float(meta.get('market_dna_realized_usd','0') or 0):.4f}")
    md[5].metric("Fees $",f"{float(meta.get('market_dna_fees_usd','0') or 0):.4f}")
    md[6].metric("Open",md_open)
    md[7].metric("Signals",meta.get("market_dna_signals","0"))

    mdtr=db.execute("""SELECT symbol,status,entry_vwap,exit_vwap,gross_pnl_usd,net_pnl_usd,
                              exit_reason,hold_ms,notional_usd,entry_features_json
                       FROM trades WHERE session_id=? AND risk_state='MARKET_DNA_V1'
                       ORDER BY id DESC LIMIT 50""",(session,)).fetchall()
    st.subheader("MARKET-DNA V1 trades")
    mdrep=[]
    for r in mdtr:
        ef=parse_json(r[9]); d=(ef.get("market_dna_v1") or {})
        n=float(r[8] or 0); g=float(r[4] or 0); net=float(r[5] or 0)
        mdrep.append({"symbol":r[0],"status":r[1],"entry":r[2],"exit":r[3],
                      "gross_bps":round(g/n*10000,2) if n else None,
                      "net_bps":round(net/n*10000,2) if n else None,
                      "reason":r[6],"hold_s":round(float(r[7] or 0)/1000,3),
                      "t5":d.get("t5"),"activity":round(float(d.get("activity",0) or 0),2),
                      "d10_t5":d.get("d10_t5")})
    if mdrep: st.dataframe(mdrep,hide_index=True,width='stretch')
    else: st.caption("Waiting for first MARKET-DNA V1 signal...")

    st.divider()
    st.caption("Legacy ROCKET diagnostics/history below — entries are disabled.")
    st.markdown(f"**Legacy ROCKET wallet USD {cash:.2f}** · Session {session}")

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
    a[0].metric("ROCKET PAPER","ON" if meta.get("rocket_entries_enabled","0")=="1" else "OFF")
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
        } for x in uni],hide_index=True,width='stretch')
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
        st.dataframe(live,hide_index=True,width='stretch')
    else:
        st.caption("Waiting for live samples...")

    st.subheader("ROCKET rejection diagnostics")
    rej=db.execute("""SELECT symbol,ts_ms,reason,payload_json FROM events
                      WHERE session_id=? AND stage='ROCKET_REJECT'
                      ORDER BY id DESC LIMIT 100""",(session,)).fetchall()
    if rej:
        reason_counts={}
        failed_counts={}
        recent=[]
        for symbol,ts_ms,reason,payload_json in rej:
            reason_counts[reason]=reason_counts.get(reason,0)+1
            p=parse_json(payload_json)
            for chk in p.get("failed_checks",[]) or []:
                failed_counts[chk]=failed_counts.get(chk,0)+1
            recent.append({
                "symbol":symbol,
                "age_s":round(max(0,now-int(ts_ms or now))/1000,1),
                "reason":reason,
                "failed_checks":", ".join(p.get("failed_checks",[]) or []),
                "beyond_bps":round(float(p.get("beyond_bps",0) or 0),2),
                "tape_x":round(float(p.get("tape_ratio",0) or 0),2),
                "samples":p.get("samples",""),
                "spread_bps":round(float(p.get("spread_bps",0) or 0),2) if "spread_bps" in p else None,
                "trades_1s":int(p.get("trades_1s",0) or 0) if "trades_1s" in p else None,
                "trades_5s":int(p.get("trades_5s",0) or 0) if "trades_5s" in p else None,
                "dir250_bps":round(float(p.get("dir250_bps",0) or 0),2) if "dir250_bps" in p else None,
                "dir1_bps":round(float(p.get("dir1_bps",0) or 0),2) if "dir1_bps" in p else None,
                "flow":round(float(p.get("directional_flow",0) or 0),3) if "directional_flow" in p else None
            })
        c1,c2=st.columns(2)
        c1.dataframe([{"reject_reason":k,"count":v} for k,v in sorted(reason_counts.items(),key=lambda kv:-kv[1])],
                     hide_index=True,width='stretch')
        c2.dataframe([{"failed_check":k,"count":v} for k,v in sorted(failed_counts.items(),key=lambda kv:-kv[1])],
                     hide_index=True,width='stretch')
        st.dataframe(recent,hide_index=True,width='stretch')
    else:
        st.caption("No ROCKET rejections yet.")

    trades=db.execute("""SELECT symbol,side,status,entry_vwap,exit_vwap,
                         gross_pnl_usd,net_pnl_usd,entry_fee_usd,exit_fee_usd,exit_reason,
                         opened_ms,closed_ms,hold_ms,notional_usd,entry_features_json
                         FROM trades
                         WHERE session_id=? AND risk_state='ROCKET_V1'
                         ORDER BY id DESC LIMIT 50""",(session,)).fetchall()
    st.subheader("ROCKET trades")
    report=[]
    for r in trades:
        ef=parse_json(r[14])
        notional=float(r[13] or 0)
        net=float(r[6] or 0)
        gross=float(r[5] or 0)
        report.append({
            "symbol":r[0],"side":r[1],"status":r[2],
            "entry":r[3],"exit":r[4],
            "gross_$":round(gross,5),
            "net_$":round(net,5),
            "gross_bps":round((gross/notional)*10000,2) if notional else None,
            "net_bps":round((net/notional)*10000,2) if notional else None,
            "entry_fee_$":round(float(r[7] or 0),5),
            "exit_fee_$":round(float(r[8] or 0),5),
            "reason":r[9],
            "hold_s":round((float(r[12] or 0)/1000),3) if r[12] is not None else round((((r[11] or now)-(r[10] or now))/1000),3),
            "arm_tape_x":round(float(ef.get("arm_tape_ratio",0) or 0),2),
            "confirm_tape_x":round(float(ef.get("confirm_tape_ratio",0) or 0),2),
            "confirm_samples":ef.get("confirm_samples"),
            "best_vote_count":ef.get("best_vote_count"),
            "best_beyond_bps":round(float(ef.get("best_beyond_bps",0) or 0),2)
        })
    if report:
        st.dataframe(report,hide_index=True,width='stretch')
    else:
        st.caption("No ROCKET fills yet.")

board()

