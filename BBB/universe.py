"""Dynamic Bybit VOL universe scanner.

Public REST only. No API key. Selects large, fast linear USDT perpetuals by
24h turnover plus observed 24h range, then assigns CORE / FAST / EXTREME.
"""
from __future__ import annotations
import asyncio, json, math, os, urllib.parse, urllib.request
from dataclasses import dataclass, asdict

BASE="https://api.bybit.com"

@dataclass(frozen=True)
class Candidate:
    symbol:str
    bucket:str
    turnover24h:float
    range_bps:float
    abs_change_bps:float
    open_interest_value:float
    score:float

def _get(path:str,params:dict) -> dict:
    url=BASE+path+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"BBB-VOL-Scanner/1.0"})
    with urllib.request.urlopen(req,timeout=15) as r:
        obj=json.loads(r.read().decode("utf-8"))
    if int(obj.get("retCode",0))!=0:
        raise RuntimeError(f"Bybit REST error: {obj.get('retMsg')}")
    return obj

def _scan_sync() -> list[Candidate]:
    tickers=_get("/v5/market/tickers",{"category":"linear"}).get("result",{}).get("list",[])
    info=_get("/v5/market/instruments-info",{"category":"linear","status":"Trading","limit":1000}).get("result",{}).get("list",[])
    valid={x.get("symbol") for x in info
           if x.get("status")=="Trading" and x.get("contractType")=="LinearPerpetual"
           and x.get("quoteCoin")=="USDT"}
    excludes={x.strip().upper() for x in os.getenv(
        "BBB_VOL_EXCLUDE","BTCUSDT,ETHUSDT,SOLUSDT,USDCUSDT,USDEUSDT,USDTUSDT"
    ).split(",") if x.strip()}
    min_turn=float(os.getenv("BBB_VOL_MIN_TURNOVER_USD","10000000"))
    rows=[]
    for t in tickers:
        s=str(t.get("symbol","")).upper()
        if s not in valid or s in excludes or not s.endswith("USDT"): continue
        try:
            last=float(t.get("lastPrice") or 0)
            hi=float(t.get("highPrice24h") or 0)
            lo=float(t.get("lowPrice24h") or 0)
            turn=float(t.get("turnover24h") or 0)
            ch=abs(float(t.get("price24hPcnt") or 0))*10000
            oi=float(t.get("openInterestValue") or 0)
        except Exception:
            continue
        if last<=0 or hi<=0 or lo<=0 or turn<min_turn: continue
        rng=max(0.0,(hi-lo)/last*10000)
        # Large turnover is mandatory; range rewards actual movement.
        rank=math.log10(max(turn,1))*max(1.0,rng)
        bucket=None
        if turn>=100_000_000 and rng>=300:
            bucket="CORE"
        elif turn>=10_000_000 and rng>=1200:
            bucket="EXTREME"
        elif turn>=25_000_000 and rng>=500:
            bucket="FAST"
        if bucket:
            rows.append(Candidate(s,bucket,turn,rng,ch,oi,rank))

    limits={
        "CORE":int(os.getenv("BBB_VOL_CORE_N","4")),
        "FAST":int(os.getenv("BBB_VOL_FAST_N","5")),
        "EXTREME":int(os.getenv("BBB_VOL_EXTREME_N","3")),
    }
    out=[]
    used=set()
    # CORE prioritizes turnover, FAST/EXTREME prioritize range-adjusted rank.
    for bucket in ("CORE","FAST","EXTREME"):
        pool=[x for x in rows if x.bucket==bucket and x.symbol not in used]
        if bucket=="CORE":
            pool.sort(key=lambda x:(x.turnover24h,x.range_bps),reverse=True)
        else:
            pool.sort(key=lambda x:(x.score,x.turnover24h),reverse=True)
        for x in pool[:limits[bucket]]:
            used.add(x.symbol); out.append(x)
    return out

async def scan_vol_universe() -> list[Candidate]:
    return await asyncio.to_thread(_scan_sync)

def universe_json(rows:list[Candidate]) -> str:
    return json.dumps([asdict(x) for x in rows],separators=(",",":"))
