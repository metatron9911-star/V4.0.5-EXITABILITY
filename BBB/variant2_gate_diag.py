"""Variant 2 gate diagnostic: counts which checks reject current candidates. Read-only."""
from __future__ import annotations
import os, sqlite3, json
from pathlib import Path
from collections import Counter,defaultdict
DB=Path(os.getenv("DATA_DIR","/data"))/"bbb.sqlite3"
SESSION=os.getenv("BBB_SESSION_ID","")
def main():
    cx=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=5)
    rows=list(cx.execute("""SELECT payload_json FROM agent_council_decisions
                            WHERE session_id=? ORDER BY ts_ms""",(SESSION,)))
    checks=Counter(); decisions=Counter(); total=0
    for (pj,) in rows:
        try:p=json.loads(pj or "{}")
        except Exception:continue
        total+=1
        decisions[p.get("decision","?")]+=1
        # reconstruct current variant-2 checks from stored council payload
        regime=p.get("regime")
        rv=p.get("risk_veto") or {}
        votes=p.get("votes") or {}
        imp=float((votes.get("IMPULSE") or {}).get("score") or 0)
        absb=float((votes.get("ABSORPTION") or {}).get("score") or 0)
        tape=float((votes.get("TAPE") or {}).get("score") or 0)
        book=float((votes.get("BOOK") or {}).get("score") or 0)
        weighted=.40*imp+.30*absb+.20*tape+.10*book
        agreement=float(p.get("agreement") or 0)
        frame=p.get("frame") or {}
        flow1s=float(frame.get("flow1s") or 0)
        vals={
          "REGIME": regime in ("ACTIVE","QUIET"),
          "NO_VETO": not bool(rv.get("veto")),
          "IMPULSE": imp>=0.45,
          "ABSORPTION": absb>=0.20,
          "TAPE": tape>=0.30,
          "BOOK": book>=-0.05,
          "WEIGHTED": weighted>=0.40,
          "AGREEMENT_ENVELOPE": 0.6983031332207035<=agreement<=0.8677086708817653,
          "FLOW1S_ENVELOPE": -0.2277099099360245<=flow1s<=1.0,
        }
        for k,v in vals.items():
            if not v: checks[k]+=1
    print("BBB_VARIANT2_GATE_DIAG "+json.dumps({"session":SESSION,"rows":total,"decisions":decisions,"fails":checks},default=dict,separators=(",",":")),flush=True)
    cx.close()
if __name__=="__main__":main()
