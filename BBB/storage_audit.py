"""Read-only /data storage audit for Railway."""
from pathlib import Path
import os, sqlite3

ROOT=Path(os.getenv("DATA_DIR","/data"))

def human(n):
    n=float(n)
    for u in ("B","KB","MB","GB","TB"):
        if n<1024 or u=="TB": return f"{n:.2f}{u}"
        n/=1024

print("=== BBB STORAGE AUDIT ===", flush=True)
print("ROOT", ROOT, flush=True)
total=0
rows=[]
if ROOT.exists():
    for p in ROOT.rglob("*"):
        try:
            if p.is_file():
                sz=p.stat().st_size
                total+=sz
                rows.append((sz,str(p.relative_to(ROOT))))
        except Exception as e:
            print("STAT_FAIL",p,e,flush=True)
print("TOTAL_FILES_BYTES",total,human(total),flush=True)
for sz,name in sorted(rows,reverse=True)[:80]:
    print("FILE",human(sz),name,flush=True)

db=ROOT/"bbb.sqlite3"
if db.exists():
    try:
        cx=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=5)
        print("DB_FILE",human(db.stat().st_size),flush=True)
        for table in ("events","feature_samples","trades","wallet_ledger","equity_samples","meta"):
            try:
                n=cx.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print("TABLE",table,n,flush=True)
            except Exception as e:
                print("TABLE_FAIL",table,e,flush=True)
        try:
            for sid,n in cx.execute("SELECT session_id,COUNT(*) FROM events GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 20"):
                print("EVENT_SESSION",n,sid,flush=True)
        except Exception: pass
        try:
            for sid,n in cx.execute("SELECT session_id,COUNT(*) FROM feature_samples GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 20"):
                print("FEATURE_SESSION",n,sid,flush=True)
        except Exception: pass
        try:
            for sid,n in cx.execute("SELECT session_id,COUNT(*) FROM trades GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 20"):
                print("TRADE_SESSION",n,sid,flush=True)
        except Exception: pass
        cx.close()
    except Exception as e:
        print("DB_AUDIT_FAIL",repr(e),flush=True)
print("=== END STORAGE AUDIT ===",flush=True)
