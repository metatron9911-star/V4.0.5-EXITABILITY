import json
import math
import pandas as pd
import streamlit as st

from engine import V4Engine, CFG, age_text
from monitoring import RuntimeSupervisor
from telegram_notify import test_message

st.set_page_config(page_title="V4 Momentum Hunter", layout="wide")


def _clamp(v, a, b):
    return max(a, min(b, v))


def _score_breakdown_from_raw(raw_text):
    """Rebuild the exact current engine Scorer components from stored DEX pair raw JSON."""
    try:
        p = json.loads(raw_text or "{}")
    except Exception:
        p = {}

    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    pc = p.get("priceChange") or {}
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}

    m5 = float(pc.get("m5") or 0)
    h1 = float(pc.get("h1") or 0)
    v5 = float(vol.get("m5") or 0)
    v1 = float(vol.get("h1") or 0)
    t5 = tx.get("m5") or {}
    buys = float(t5.get("buys") or 0)
    sells = float(t5.get("sells") or 0)

    imbalance = (buys + 1) / (sells + 1)
    vol_acc = (v5 * 12) / (v1 + 1) if v1 else (2 if v5 > 1000 else 0)

    pts_m5 = _clamp((m5 - 1) / 7 * 22, 0, 22)
    pts_h1 = _clamp((h1 - 3) / 37 * 16, 0, 16)
    pts_vol = _clamp((vol_acc - 1) / 4 * 20, 0, 20)
    pts_flow = _clamp((imbalance - 1) / 2.5 * 18, 0, 18)
    pts_liq = _clamp(math.log10(max(liq, 1) / 25000) * 12, 0, 12)
    pts_buys = 12 if buys >= 20 else _clamp(buys / 20 * 12, 0, 12)

    score_calc = round(_clamp(pts_m5 + pts_h1 + pts_vol + pts_flow + pts_liq + pts_buys, 0, 100), 1)

    risk_parts = []
    risk_calc = 0
    if liq < CFG["min_liquidity_usd"]:
        risk_calc += 35
        risk_parts.append("liq<min +35")
    if h1 > 100:
        risk_calc += 20
        risk_parts.append("h1>100% +20")
    if m5 > 35:
        risk_calc += 25
        risk_parts.append("m5>35% +25")
    if sells == 0 and buys > 20:
        risk_calc += 10
        risk_parts.append("0 sells / >20 buys +10")
    fdv = float(p.get("fdv") or 0)
    mc = float(p.get("marketCap") or 0)
    basis = mc or fdv
    if basis and liq / basis < 0.01:
        risk_calc += 25
        risk_parts.append("liq/cap<1% +25")
    elif basis and liq / basis < 0.03:
        risk_calc += 12
        risk_parts.append("liq/cap<3% +12")

    return {
        "m5_%": m5,
        "h1_%": h1,
        "vol_acc": vol_acc,
        "buy/sell": imbalance,
        "buys_5m": buys,
        "sells_5m": sells,
        "pts_m5": round(pts_m5, 1),
        "pts_h1": round(pts_h1, 1),
        "pts_vol": round(pts_vol, 1),
        "pts_flow": round(pts_flow, 1),
        "pts_liq": round(pts_liq, 1),
        "pts_buys": round(pts_buys, 1),
        "score_calc": score_calc,
        "risk_calc": round(_clamp(risk_calc, 0, 100), 1),
        "risk_why": "; ".join(risk_parts) if risk_parts else "no active risk penalties",
    }


@st.cache_resource
def get_engine():
    return V4Engine()


@st.cache_resource
def get_supervisor(_engine):
    return RuntimeSupervisor(_engine).start()


def candidate_table(engine: V4Engine) -> pd.DataFrame:
    rows = engine.db.q(
        "SELECT symbol,mint,liquidity,price,score,risk,first_seen_ms,raw "
        "FROM candidates ORDER BY score DESC LIMIT 100"
    )
    open_positions = {
        x["mint"] for x in engine.db.q("SELECT mint FROM positions WHERE status='OPEN'")
    }
    any_open = bool(open_positions)
    min_liq = float(CFG["min_liquidity_usd"])
    min_score = float(CFG["min_momentum_score"])
    max_risk = float(CFG["max_risk_score"])

    out = []
    for r in rows:
        liq = float(r.get("liquidity") or 0)
        score = float(r.get("score") or 0)
        risk = float(r.get("risk") or 0)
        mint = r.get("mint") or ""
        bd = _score_breakdown_from_raw(r.get("raw"))

        reasons = []
        if liq < min_liq:
            reasons.append(f"LIQ ${liq:,.0f} < ${min_liq:,.0f}")
        if score < min_score:
            reasons.append(f"SCORE {score:.1f} < {min_score:.0f}")
        if risk > max_risk:
            reasons.append(f"RISK {risk:.1f} > {max_risk:.0f}")

        if mint in open_positions:
            entry = "🟢 OPEN PAPER"
            reason = "POSITION OPEN"
        elif reasons:
            entry = "❌ NO ENTRY"
            reason = " • ".join(reasons)
        elif any_open:
            entry = "🟡 WAIT"
            reason = f"MAX OPEN POSITIONS = {CFG.get('max_open_positions', 1)}"
        else:
            entry = "✅ ELIGIBLE"
            reason = "ALL ENTRY FILTERS PASSED"

        formula = (
            f"m5 {bd['pts_m5']}/22 + h1 {bd['pts_h1']}/16 + "
            f"vol {bd['pts_vol']}/20 + flow {bd['pts_flow']}/18 + "
            f"liq {bd['pts_liq']}/12 + buys {bd['pts_buys']}/12"
        )

        out.append(
            {
                "symbol": r.get("symbol"),
                "mint": mint,
                "liquidity": liq,
                "price": float(r.get("price") or 0),
                "score": score,
                "risk": risk,
                "ENTRY": entry,
                "REJECT / STATE": reason,
                "SCORE BREAKDOWN": formula,
                "m5 %": round(bd["m5_%"], 2),
                "h1 %": round(bd["h1_%"], 2),
                "vol accel": round(bd["vol_acc"], 2),
                "buy/sell": round(bd["buy/sell"], 2),
                "buys 5m": int(bd["buys_5m"]),
                "sells 5m": int(bd["sells_5m"]),
                "RISK WHY": bd["risk_why"],
                "score_gap": round(score - min_score, 1),
                "first_seen_ms": r.get("first_seen_ms"),
            }
        )
    return pd.DataFrame(out)


e = get_engine()
sup = get_supervisor(e)

st.title("🚀 V4 Momentum Hunter — PAPER v4.0.7")
st.caption(
    "Solana momentum scanner • Moonbag Ladder • background PAPER scanner • "
    "transparent score/risk diagnostics • AUTO execution physically disabled"
)

s = e.hard_safety()
if s["execution"] != "DISABLED_IN_CODE":
    st.error("Safety invariant broken")
else:
    st.success("AUTO HARD LOCK: ON — no real orders can be signed or sent by this build")

st.subheader("🛰 Sources & modules")
status = sup.external_status(refresh=False)
cols = st.columns(4)
for i, (name, x) in enumerate(status.items()):
    with cols[i % 4]:
        icon = "✅" if x.get("ok") else "❌"
        if x.get("ok"):
            st.success(f"{icon} **{name}**\n\n{x.get('status')}\n\n{x.get('detail','')}")
        else:
            st.error(f"{icon} **{name}**\n\n{x.get('status')}\n\n{x.get('detail','')}")

b1, b2, b3, b4 = st.columns(4)
with b1:
    if st.button("Scan now"):
        e.scan_once(trigger="MANUAL")
        st.rerun()
with b2:
    if st.button("Refresh status"):
        sup.external_status(refresh=True)
        st.rerun()
with b3:
    if st.button("Send status to Telegram"):
        if sup.send_status(reason="dashboard request"):
            st.success("Status sent ✅")
        else:
            st.error("Telegram status send failed")
with b4:
    if st.button("Test Telegram"):
        ok, detail = test_message()
        st.success("Telegram test sent ✅") if ok else st.error(detail)

stats = e.stats()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Candidates", stats["candidates"])
c2.metric("Open PAPER", stats["open"])
c3.metric("Closed", stats["closed"])
c4.metric("Realized P&L", f"${stats['realized']:.2f}")

st.subheader("🧾 PAPER accounting — v4.0.7")
a1,a2,a3,a4,a5 = st.columns(5)
a1.metric("Starting capital", f"${stats['paper_capital']:.2f}")
a2.metric("Cash", f"${stats['cash']:.2f}")
a3.metric("Open marked value", f"${stats['open_value']:.2f}")
a4.metric("Total equity", f"${stats['equity']:.2f}")
a5.metric("Total P&L", f"${stats['total_pnl']:+.2f}")
st.caption(f"Harvested cash proceeds: ${stats['harvested_proceeds']:.2f}. Realized P&L counts only fully CLOSED positions. Open value uses latest stored candidate price.")
st.caption("v4.0.7 COMPOUND HARVEST: each +15% cycle banks 10% of the working base to CASH and compounds the remaining +5%. After 3 harvests, 10% of remaining tokens are ring-fenced as MOONBAG; 50% of that moonbag is harvested at x10 and x100.")

st.subheader("🔬 Last scan diagnostics")
d = dict(e.scan_diag or {})
if not d or d.get("status") == "NOT RUN":
    st.info("Waiting for first background scan…")
else:
    a, b, c, dcol = st.columns(4)
    a.metric("Discovered", d.get("discovered", 0))
    b.metric("Pairs found", d.get("pairs_found", 0))
    c.metric("Eligible", d.get("eligible", 0))
    dcol.metric("PAPER opened", d.get("paper_opened", 0))
    st.caption(
        f"Status: {d.get('status')} • trigger: {d.get('trigger')} • "
        f"last scan {age_text(int(d.get('ts_ms') or 0))} • duration {d.get('duration_ms', 0)} ms"
    )
    diag_rows = [
        ["DEX discovered", d.get("dex_discovered", 0)],
        ["Birdeye discovered", d.get("birdeye_discovered", 0)],
        ["Unique discovered", d.get("unique_discovered", 0)],
        ["No DEX pair", d.get("no_pair", 0)],
        ["Saved candidates", d.get("candidates_saved", 0)],
        ["Low liquidity", d.get("low_liquidity", 0)],
        ["Low momentum score", d.get("low_score", 0)],
        ["High risk", d.get("high_risk", 0)],
        ["Exitability failed", d.get("exitability_failed", 0)],
        ["Open positions updated", d.get("open_positions_updated", 0)],
    ]
    st.dataframe(
        pd.DataFrame(diag_rows, columns=["Stage / filter", "Count"]),
        width="stretch",
        hide_index=True,
    )
if e.last_error:
    st.warning(e.last_error)

st.subheader("🔥 Candidates — entry decision + score anatomy")
st.caption(
    f"Current PAPER entry gates: liquidity ≥ ${CFG['min_liquidity_usd']:,.0f} • "
    f"momentum score ≥ {CFG['min_momentum_score']} • risk ≤ {CFG['max_risk_score']} • "
    f"max open positions = {CFG.get('max_open_positions', 1)}. "
    "Score is rebuilt below from the exact current engine formula stored in engine.py."
)

with st.expander("🧮 How SCORE is built (current engine formula)", expanded=False):
    st.markdown(
        """
**Maximum = 100 points**

- **5m price momentum:** up to **22** points. Starts contributing above +1%/5m; reaches full 22 around +8%/5m.
- **1h price momentum:** up to **16** points. Starts contributing above +3%/1h; reaches full 16 around +40%/1h.
- **Volume acceleration:** up to **20** points. Compares current 5m volume annualized to 1h volume.
- **Buy/sell imbalance:** up to **18** points. More buys relative to sells = more points.
- **Liquidity:** up to **12** points, logarithmic relative to $25k.
- **Buys in last 5m:** up to **12** points; 20+ buys receives full 12.

The **Risk score is separate** and adds penalties for low liquidity, extremely vertical price moves, one-sided buy flow, and weak liquidity/market-cap ratio.
"""
    )

cdf = candidate_table(e)
st.dataframe(cdf, width="stretch", hide_index=True)

st.subheader("📦 Positions")
pos = pd.DataFrame(
    e.db.q(
        "SELECT id,symbol,status,entry_price,initial_usd,remaining_qty,peak_price,"
        "realized_usd,working_base_usd,harvest_count,active_qty,moonbag_qty,moonbag_milestone_idx,reason FROM positions ORDER BY id DESC LIMIT 100"
    )
)
st.dataframe(pos, width="stretch", hide_index=True)

st.subheader("💰 Ledger")
led = pd.DataFrame(
    e.db.q(
        "SELECT ts_ms,action,symbol,usd,qty,price,fee_usd,note "
        "FROM ledger ORDER BY id DESC LIMIT 200"
    )
)
st.dataframe(led, width="stretch", hide_index=True)

st.subheader("🧪 Relaxed-gates Replay Lab")
st.caption("Replays only the snapshots already stored in /data/v4.sqlite3. This is a diagnostic, not a claim of historical profitability. Sparse tokens are reported separately.")
try:
    snaps=pd.DataFrame(e.db.q("SELECT ts_ms,mint,price,liquidity,score,risk FROM snapshots ORDER BY mint,ts_ms"))
    rg=CFG.get("replay_gates",{})
    rminliq=float(rg.get("min_liquidity_usd",CFG["min_liquidity_usd"]))
    rminscore=float(rg.get("min_momentum_score",CFG["min_momentum_score"]))
    rmaxrisk=float(rg.get("max_risk_score",CFG["max_risk_score"]))
    trades=[]; sparse=0
    if not snaps.empty:
        for mint,g in snaps.groupby("mint"):
            g=g.sort_values("ts_ms").reset_index(drop=True)
            ent=g[(g.liquidity>=rminliq)&(g.score>=rminscore)&(g.risk<=rmaxrisk)]
            if ent.empty: continue
            i=int(ent.index[0]); entry=float(g.loc[i,"price"] or 0)
            future=g.iloc[i+1:]
            if entry<=0 or future.empty:
                sparse+=1; continue
            exitp=float(future.iloc[-1].price); reason="LAST_SNAPSHOT"; peak=entry
            for _,rr in future.iterrows():
                px=float(rr.price or 0); peak=max(peak,px)
                if px<=entry*0.85:
                    exitp=px; reason="STOP_-15%"; break
                if peak>=entry*1.25 and px<=peak*0.90:
                    exitp=px; reason="TRAIL_10%_AFTER_+25%"; break
            gross=(exitp/entry-1)*100
            net=gross-2*float(CFG.get("paper_friction_pct_per_sale",1.5))
            trades.append({"mint":mint,"entry":entry,"exit":exitp,"gross_%":round(gross,2),"net_%_approx":round(net,2),"reason":reason,"observations":len(future)+1})
    rdf=pd.DataFrame(trades)
    a,b,c,dcol=st.columns(4)
    a.metric("Replay trades",len(rdf)); b.metric("Winners",int((rdf["net_%_approx"]>0).sum()) if not rdf.empty else 0); c.metric("Losers",int((rdf["net_%_approx"]<=0).sum()) if not rdf.empty else 0); dcol.metric("Sparse / unusable",sparse)
    if not rdf.empty:
        st.caption(f"Approx mean net: {rdf['net_%_approx'].mean():+.2f}% • median: {rdf['net_%_approx'].median():+.2f}% • win rate: {(rdf['net_%_approx']>0).mean()*100:.1f}%")
        st.dataframe(rdf.sort_values("net_%_approx",ascending=False),width="stretch",hide_index=True)
except Exception as exc:
    st.warning(f"Replay Lab unavailable: {exc!r}")

with st.expander("Safety / configuration"):
    st.json(s)
    st.json(CFG)

st.info(
    "Telegram: hourly /status health reports are active. PAPER BUY, HARVEST and EXIT "
    "are sent immediately by the engine. Commands: /status or /health."
)
