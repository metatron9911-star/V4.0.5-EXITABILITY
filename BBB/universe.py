"""Dynamic Bybit VOL universe scanner.

Public REST only. No API key. Selects large, fast linear USDT perpetuals by
24h turnover plus observed 24h range, then assigns CORE / FAST / EXTREME.
"""
from __future__ import annotations
import asyncio, json, math, os, urllib.parse, urllib.request
from dataclasses import dataclass, asdict

# Main endpoints first, then official regional endpoints. Railway currently runs
# in a US region where Bybit REST can return HTTP 403; the worker therefore also
# has a websocket-driven fallback pool below.
BASES=[
    "https://api.bybit.com",
    "https://api.bytick.com",
    "https://api.bybit.eu",
    "https://api.bybit.kz",
    "https://api.bybit.tr",
    "https://api.bybitgeorgia.ge",
    "https://api.bybit.ae",
    "https://api.bybit.id",
]

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
    last=None
    for base in BASES:
        try:
            url=base+path+"?"+urllib.parse.urlencode(params)
            req=urllib.request.Request(url,headers={"User-Agent":"BBB-VOL-Scanner/1.0"})
            with urllib.request.urlopen(req,timeout=4) as r:
                obj=json.loads(r.read().decode("utf-8"))
            if int(obj.get("retCode",0))!=0:
                raise RuntimeError(f"Bybit REST error: {obj.get('retMsg')}")
            return obj
        except Exception as e:
            last=e
    raise RuntimeError(f"all Bybit REST hosts failed: {last}")

def _scan_sync() -> list[Candidate]:
    tickers=_get("/v5/market/tickers",{"category":"linear"}).get("result",{}).get("list",[])
    info=_get("/v5/market/instruments-info",{"category":"linear","status":"Trading","limit":1000}).get("result",{}).get("list",[])
    valid={x.get("symbol") for x in info
           if x.get("status")=="Trading" and x.get("contractType")=="LinearPerpetual"
           and x.get("quoteCoin")=="USDT"}
    excludes={x.strip().upper() for x in os.getenv(
        "BBB_VOL_EXCLUDE","BTCUSDT,ETHUSDT,SOLUSDT,USDCUSDT,USDEUSDT,USDTUSDT"
    ).split(",") if x.strip()}
    min_turn=float(os.getenv("BBB_ROCKET_MIN_TURNOVER_USD","5000000"))
    min_range=float(os.getenv("BBB_ROCKET_MIN_RANGE24H_BPS","1000"))
    max_n=int(os.getenv("BBB_ROCKET_UNIVERSE_N","14"))
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
        if rng<min_range: continue
        # One purpose only: keep meme-like / explosive contracts.
        # Rank prefers extreme range first, then meaningful turnover.
        rank=rng*math.log10(max(turn,10))
        rows.append(Candidate(s,"EXTREME",turn,rng,ch,oi,rank))

    rows.sort(key=lambda x:(x.range_bps,x.score,x.turnover24h),reverse=True)
    return rows[:max_n]

async def scan_vol_universe() -> list[Candidate]:
    return await asyncio.to_thread(_scan_sync)

def universe_json(rows:list[Candidate]) -> str:
    return json.dumps([asdict(x) for x in rows],separators=(",",":"))


def fallback_vol_universe() -> list[Candidate]:
    """Fallback discovery pool. Live ROCKET gates still decide actual tradability."""
    symbols=[x.strip().upper() for x in os.getenv(
        "BBB_ROCKET_FALLBACK_SYMBOLS",
        "HYPEUSDT,PEPEUSDT,DOGEUSDT,SUIUSDT,GRASSUSDT,ZROUSDT,JUPUSDT,ONDOUSDT,APTUSDT,ARBUSDT"
    ).split(",") if x.strip()]
    return [Candidate(s,"EXTREME",0.0,0.0,0.0,0.0,0.0) for s in symbols]

