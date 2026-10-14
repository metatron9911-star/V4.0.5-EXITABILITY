"""Count microflow candidate signals during historical COMMITTEE_V1 trading window."""
from __future__ import annotations
import os, sqlite3, json
from pathlib import Path
DB=Path(os.getenv("DATA_DIR","/data"))/"bbb.sqlite3"
def main():
    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=5)
    r=cx.execute("""SELECT MIN(opened_ms),MAX(closed_ms),COUNT(*),
                         SUM(CASE WHEN net_pnl_usd>0 THEN 1 ELSE 0 END),
                         SUM(CASE WHEN net_pnl_usd<=0 THEN 1 ELSE 0 END)
                  FROM trades WHERE risk_state='COMMITTEE_V1' AND status='CLOSED'""").fetchone()
    lo,hi,n,w,l=r
    out={"closed":n or 0,"wins":w or 0,"losses":l or 0,"window_start":lo,"window_end":hi}
    if lo and hi:
        out["microflow_all_episodes_in_window"]=cx.execute(
          "SELECT COUNT(*) FROM microflow_episodes WHERE decision_ms BETWEEN ? AND ?",(lo,hi)).fetchone()[0]
        out["microflow_signals_in_window"]=cx.execute(
          "SELECT COUNT(*) FROM microflow_episodes WHERE decision_stage='SIGNAL' AND decision_ms BETWEEN ? AND ?",(lo,hi)).fetchone()[0]
        out["microflow_rejects_in_window"]=cx.execute(
          "SELECT COUNT(*) FROM microflow_episodes WHERE decision_stage<>'SIGNAL' AND decision_ms BETWEEN ? AND ?",(lo,hi)).fetchone()[0]
        out["signal_labels"]=dict(cx.execute(
          """SELECT COALESCE(outcome_label,'NULL'),COUNT(*) FROM microflow_episodes
             WHERE decision_stage='SIGNAL' AND decision_ms BETWEEN ? AND ?
             GROUP BY COALESCE(outcome_label,'NULL')""",(lo,hi)).fetchall())
    print("BBB_V1_FUNNEL "+json.dumps(out,separators=(",",":")),flush=True)
    cx.close()
if __name__=="__main__":main()
