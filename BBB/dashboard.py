"""BBB H2 dashboard — RP7-style operational panel."""
from __future__ import annotations
import json, os, sqlite3, time
from collections import defaultdict
from pathlib import Path
import streamlit as st

ROOT=Path(__file__).resolve().parent
DATA_DIR=Path(os.getenv("DATA_DIR",str(ROOT/"data")))
DB=DATA_DIR/"bbb.sqlite3"

st.set_page_config(page_title="BBB — BybitBot Beta",layout="wide")

def _st_ver():
    try:
        parts=st.__version__.split(".")[:2]
        return tuple(int(x) for x in parts)
    except Exception:
        return (1,40)

_DF_KW={"width":"stretch"} if _st_ver()>=(1,40) else {"use_container_width":True}

st.markdown("""
<style>
.block-container { padding-top: 1.1rem; padding-bottom: 2rem; }
h1 { font-size: 2.35rem !important; margin-bottom: .2rem !important; }
h2, h3 { letter-spacing: .01em; }
div[data-testid="stMetricLabel"] p { font-size: 1.18rem !important; font-weight: 800 !important; opacity: .96 !important; }
div[data-testid="stMetricValue"] { font-size: 1.18rem !important; font-weight: 800 !important; }
div[data-testid="stMetricDelta"] { font-size: .95rem !important; }
[data-testid="stDataFrame"] { font-size: 0.94rem !important; }
.rp7-strip { padding: .48rem .72rem; border: 1px solid rgba(128,128,128,.23); border-radius: .55rem; margin: .25rem 0 1rem 0; font-weight: 700; }
.rp7-wallet-strip { padding: .55rem .8rem; border: 1px solid rgba(128,128,128,.23); border-radius: .55rem; margin: .25rem 0 1rem 0; font-size: 1.18rem; font-weight: 800; }
.rp7-wallet-strip .lbl { opacity: .82; font-size: 1.18rem; font-weight: 800; }
.rp7-wallet-strip .val { font-size: 1.18rem; font-weight: 800; }
.pnl-green { color:#39d98a; font-size:2.15rem; font-weight:900; }
.pnl-red { color:#ff6262; font-size:2.15rem; font-weight:900; }
.pnl-flat { color:#e6e6e6; font-size:2.15rem; font-weight:900; }
.small-muted { opacity:.7; font-size:.86rem; }
</style>
""",unsafe_allow_html=True)

st.title("BBB — BYBIT BOT BETA")
st.caption("PAPER / SHADOW ONLY · H2 winner/loser sizing · dynamic VOL universe")

def cx():
    return sqlite3.connect(DB,check_same_thread=False) if DB.exists() else None

def safe_json(value):
    try:return json.loads(value or "{}")
    except Exception:return {}

def pnl_html(label,value):
    cls="pnl-green" if value>0 else ("pnl-red" if value<0 else "pnl-flat")
    return f"<div class='small-muted'>{label}</div><div class='{cls}'>USD {value:.2f}</div>"

@st.fragment(run_every="2s")
def board():
    db=cx()
    if not db:
        st.warning("Waiting for BBB database…")
        return
    now=int(time.time()*1000)
    meta=dict(db.execute("SELECT key,value FROM meta").fetchall())
    session=meta.get("session_id","")
    hb=int(meta.get("heartbeat_ms","0") or 0)
    feed_state=meta.get("feed_state","STARTING")
    reconnects=meta.get("reconnects","0")
    msgs=meta.get("messages","0")
    qdepth=meta.get("queue_depth","0")
    src_p50=meta.get("source_age_p50_ms","—"); src_p99=meta.get("source_age_p99_ms","—")
    proc_p50=meta.get("processing_p50_ms","—"); proc_p99=meta.get("processing_p99_ms","—")
    green=bool(hb and now-hb<10000 and feed_state=="CONNECTED")
    status="🟢" if green else "🔴"
    st.markdown(
        f"<div class='rp7-strip'>{status} BYBIT PUBLIC WS <b>{feed_state}</b>"
        f" &nbsp;|&nbsp; messages {msgs} &nbsp;|&nbsp; reconnects {reconnects}"
        f" &nbsp;|&nbsp; queue {qdepth} &nbsp;|&nbsp; src p50/p99 {src_p50}/{src_p99} ms"
        f" &nbsp;|&nbsp; proc p50/p99 {proc_p50}/{proc_p99} ms</div>",
        unsafe_allow_html=True
    )

    closed=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED'",(session,)).fetchone()[0]
    wins=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND net_pnl_usd>0",(session,)).fetchone()[0]
    losses=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='CLOSED' AND net_pnl_usd<=0",(session,)).fetchone()[0]
    open_n=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND status='OPEN'",(session,)).fetchone()[0]
    wr=(wins/closed*100.0) if closed else 0.0
    realized=float(meta.get("realized_pnl_usd","0") or 0)
    fees=float(meta.get("fees_usd","0") or 0)
    profit_wallet=float(meta.get("profit_wallet_usd","0") or 0)
    trading_cash=float(meta.get("trading_cash_usd",meta.get("paper_start_usd","500")) or 0)

    elc=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND exit_reason='EARLY_LOSER_CUT'",(session,)).fetchone()[0]
    tails=db.execute("SELECT COUNT(*) FROM trades WHERE session_id=? AND exit_reason IN ('RIGHT_TAIL_TRAIL','RIGHT_TAIL_TIME','RIGHT_TAIL_FLOOR')",(session,)).fetchone()[0]
    cf60=db.execute("SELECT payload_json FROM events WHERE session_id=? AND stage='ELC_COUNTERFACTUAL' AND reason='60s'",(session,)).fetchall()
    cf60_rec=sum(1 for (j,) in cf60 if safe_json(j).get("recovered"))

    st.markdown(
        f"<div class='rp7-wallet-strip'>"
        f"<span class='lbl'>Trading wallet</span> <span class='val'>USD {trading_cash:.2f}</span>"
        f" &nbsp;|&nbsp; "
        f"<span class='lbl'>Session</span> <span class='val'>{session[-18:] if session else '—'}</span>"
        f"</div>",
        unsafe_allow_html=True
    )

    st.subheader("Session")
    a=st.columns(11)
    a[0].metric("Entries","ON" if meta.get("paper_entries_enabled","0")=="1" else "OFF")
    a[1].metric("Closed",closed)
    a[2].metric("W / L",f"{wins} / {losses}",delta=f"{wr:.1f}% WR")
    a[3].metric("Realized $",f"{realized:.2f}")
    a[4].metric("Fees $",f"{fees:.2f}")
    a[5].metric("Profit wallet $",f"{profit_wallet:.2f}")
    a[6].metric("Open positions",open_n)
    a[7].metric("ELC cuts",elc)
    a[8].metric("Tail closes",tails)
    a[9].metric("ELC CF 60s",len(cf60))
    a[10].metric("CF recovered",cf60_rec)

    st.subheader("Machine state")
    m=st.columns(6)
    m[0].metric("Mode",meta.get("mode","PAPER"))
    m[1].metric("Symbols",len([x for x in meta.get("symbols","").split(",") if x]))
    m[2].metric("DB events",db.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    m[3].metric("Feature samples",db.execute("SELECT COUNT(*) FROM feature_samples").fetchone()[0])
    rules=safe_json(meta.get("sanitizer_rules_json","{}"))
    active_rules=sum(len(v) for v in rules.values()) if isinstance(rules,dict) else 0
    m[4].metric("Sanitizer rules",active_rules)
    m[5].metric("Retention",meta.get("retention_policy","—"))

    st.subheader("Dynamic VOL universe")
    st.caption("Source: "+meta.get("vol_universe_source","—") + ((" · REST: "+meta.get("vol_universe_error","")) if meta.get("vol_universe_error") else ""))
    try: uni=json.loads(meta.get("vol_universe_json","[]"))
    except Exception: uni=[]
    if not uni:
        st.caption("Universe scanner fallback: "+(meta.get("vol_universe_error") or "no candidates"))
    else:
        st.dataframe([{
            "symbol":x.get("symbol"),"bucket":x.get("bucket"),
            "turnover24h_$":round(float(x.get("turnover24h") or 0),0),
            "range24h_bps":round(float(x.get("range_bps") or 0),0),
            "abs_change24h_bps":round(float(x.get("abs_change_bps") or 0),0),
            "OI_$":round(float(x.get("open_interest_value") or 0),0)
        } for x in uni],hide_index=True,**_DF_KW)

    rows=db.execute("""SELECT symbol,head,ts_ms,mid,spread_bps,depth_bid_5_usd,depth_ask_5_usd,
                      book_imbalance_5,trade_count_1s,buy_ratio_1s,ret_1s_bps,realized_vol_5s_bps,
                      score,gate,state,payload_json
                      FROM feature_samples WHERE session_id=? AND id IN
                      (SELECT MAX(id) FROM feature_samples WHERE session_id=? GROUP BY symbol)
                      ORDER BY symbol""",(session,session)).fetchall()
    st.subheader("Heads / signals")
    head_rows=[]
    for r in rows:
        p=safe_json(r[15])
        head_rows.append({
            "symbol":r[0],"rest_bucket":p.get("rest_bucket","—"),"live_bucket":p.get("vol_bucket","—"),
            "trade_now":"YES" if p.get("live_trade_ok") else "NO",
            "activity":round(float(p.get("live_activity_score") or 0),1),
            "age_ms":now-r[2],"mid":r[3],"spread_bps":round(r[4] or 0,3),
            "depth5_min_$":round(min(r[5] or 0,r[6] or 0),0),
            "imbalance":round(r[7] or 0,3),"trades_1s":r[8],
            "buy_ratio":round(r[9] or 0,3),"ret_1s_bps":round(r[10] or 0,2),
            "rv_5s_bps":round(r[11] or 0,2),"score":round(r[12] or 0,1),
            "gate":r[13],"state":r[14],
        })
    st.dataframe(head_rows,hide_index=True,**_DF_KW)

    trades=db.execute("""SELECT head,symbol,side,status,margin_usd,notional_usd,entry_vwap,exit_vwap,
                                net_pnl_usd,exit_reason,opened_ms,closed_ms,risk_state,entry_features_json,
                                mae_pct,mfe_pct
                         FROM trades WHERE session_id=? ORDER BY id DESC LIMIT 80""",(session,)).fetchall()
    open_rows=[]; closed_rows=[]
    for r in trades:
        ef=safe_json(r[13])
        item={
            "symbol":r[1],"side":r[2],"status":r[3],
            "bucket":ef.get("vol_bucket","—"),"regime":ef.get("regime","—"),
            "tier":ef.get("sizing_tier","—"),
            "win_score":round(float(ef.get("winner_score",ef.get("confidence",0)) or 0),1),
            "loser_risk":round(float(ef.get("loser_risk",0) or 0),1),
            "size_score":round(float(ef.get("sizing_score",0) or 0),1),
            "edge_ratio":round(float(ef.get("edge_ratio",0) or 0),2),
            "aligned":ef.get("aligned_count","—"),"opposed":ef.get("opposing_count","—"),
            "size_x":ef.get("size_multiplier","—"),"margin_$":r[4],"notional_$":r[5],
            "entry":r[6],"exit":r[7],"net_$":r[8],"reason":r[9],
            "MFE_bps":round(float(r[15] or 0)*100,1),"MAE_bps":round(float(r[14] or 0)*100,1),
            "state":r[12],
        }
        (open_rows if r[3]=="OPEN" else closed_rows).append(item)

    st.subheader("Open positions")
    if open_rows: st.dataframe(open_rows,hide_index=True,**_DF_KW)
    else: st.caption("No open positions")

    st.subheader("Recent closed trades")
    if closed_rows: st.dataframe(closed_rows[:30],hide_index=True,**_DF_KW)
    else: st.caption("No closed trades yet")

    st.subheader("Sizing quality")
    tier_stats=defaultdict(lambda:{"trades":0,"wins":0,"pnl":0.0,"win_sum":0.0,"loss_sum":0.0})
    all_closed=db.execute("""SELECT net_pnl_usd,entry_features_json FROM trades
                             WHERE session_id=? AND status='CLOSED'""",(session,)).fetchall()
    for pnl,js in all_closed:
        ef=safe_json(js); tier=str(ef.get("sizing_tier","UNKNOWN"))
        x=float(pnl or 0); s=tier_stats[tier]; s["trades"]+=1; s["pnl"]+=x
        if x>0: s["wins"]+=1; s["win_sum"]+=x
        else: s["loss_sum"]+=x
    order=["MICRO","CAUTIOUS","BASE","GOOD","STRONG","PRIME","ELITE","UNKNOWN"]
    report=[]
    for tier in order:
        s=tier_stats.get(tier)
        if not s or not s["trades"]: continue
        n=s["trades"]; w=s["wins"]; l=n-w
        report.append({
            "tier":tier,"trades":n,"W/L":f"{w}/{l}","WR_%":round(w/n*100,1),
            "total_net_$":round(s["pnl"],4),"expectancy_$":round(s["pnl"]/n,5),
            "avg_win_$":round(s["win_sum"]/w,5) if w else 0,
            "avg_loss_$":round(s["loss_sum"]/l,5) if l else 0,
        })
    if report: st.dataframe(report,hide_index=True,**_DF_KW)
    else: st.caption("Waiting for H2 trades")

    st.subheader("Strategy cells")
    cells=safe_json(meta.get("strategy_cells_json","{}"))
    if cells:
        st.dataframe([{"cell":k,**v} for k,v in cells.items()],hide_index=True,**_DF_KW)

    st.subheader("Sanitizer")
    s=st.columns(4)
    s[0].metric("Rejected",meta.get("sanitizer_rejects","0"))
    s[1].metric("Winner retention target",">= 92%")
    s[2].metric("Active rules",active_rules)
    s[3].metric("CF recovery rate",f"{(cf60_rec/len(cf60)*100):.1f}%" if cf60 else "—")
    if rules:
        rr=[]
        for k,vals in rules.items():
            for r in vals:
                rr.append({
                    "head/side":k,
                    "rule":f"{r['feature']} {r['op']} {r['threshold']:.6g}",
                    "losers_removed":r["losers_removed"],"winners_removed":r["winners_removed"],
                    "winner_retention":round(r["winner_retention"],3),
                    "ΔPESB":round(r["delta_pesb"],6)
                })
        st.dataframe(rr,hide_index=True,**_DF_KW)

    st.subheader("Wallets")
    w=st.columns(4)
    w[0].markdown(pnl_html("Trading wallet",trading_cash),unsafe_allow_html=True)
    w[1].markdown(pnl_html("Profit wallet",profit_wallet),unsafe_allow_html=True)
    w[2].markdown(pnl_html("Realized PnL",realized),unsafe_allow_html=True)
    w[3].metric("Fees $",f"{fees:.2f}")

board()
