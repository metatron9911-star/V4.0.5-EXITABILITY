"""One worker process; independent scanner and per-position exit threads."""
import concurrent.futures
import fcntl
import math
import os
import threading
import time
from engine import Store, Experiment, CFG, TOKEN_PROGRAM, now_ms, db_path
from market import MarketData


def security(md, mint, cfg):
    """Fail closed. Account concentration is conservative, not beneficial ownership."""
    try:
        endpoint = os.getenv('SOLANA_RPC_URL') or (
            f'https://mainnet.helius-rpc.com/?api-key={md.helius_key}' if md.helius_key else '')
        if not endpoint: return False,'RPC_KEY_MISSING',{}
        def rpc(method, params):
            r=md.s.post(endpoint,json=dict(jsonrpc='2.0',id=1,method=method,params=params),timeout=8)
            r.raise_for_status(); j=r.json()
            if 'error' in j or 'result' not in j: raise ValueError('RPC_UNAVAILABLE')
            return j['result']
        account=rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}])['value']
        if not account or account['owner'] != TOKEN_PROGRAM: return False,'TOKEN_PROGRAM_UNSUPPORTED',{}
        info=account['data']['parsed']['info']
        if 'mintAuthority' not in info or 'freezeAuthority' not in info: return False,'AUTHORITY_UNKNOWN',{}
        if info['mintAuthority'] or info['freezeAuthority']: return False,'ACTIVE_AUTHORITY',{}
        supply=int(info['supply']); decimals=int(info['decimals'])
        if supply<=0 or not 0<=decimals<=18: return False,'INVALID_SUPPLY',{}
        holders=rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}])['value']
        if not holders: return False,'HOLDERS_UNKNOWN',{}
        largest=100*max(int(h['amount']) for h in holders)/supply
        evidence=dict(decimals=decimals,largest_token_account_pct=largest,
            authority_mint=None,authority_freeze=None,token_program=account['owner'])
        if largest>cfg['max_token_account_pct']: return False,'LARGE_TOKEN_ACCOUNT_INCLUDING_POOLS',evidence
        r=md.s.get(f'https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary',timeout=8)
        r.raise_for_status(); report=r.json(); evidence['rugcheck']=report
        locked=float(report['lpLockedPct'])
        if not math.isfinite(locked) or not 0<=locked<=100: return False,'LP_UNKNOWN',evidence
        if locked<cfg['min_lp_locked_pct']: return False,'LP_LOCK_LOW',evidence
        risks=report['risks']
        if not isinstance(risks,list): return False,'RISKS_UNKNOWN',evidence
        if any(x.get('level','').lower() in ('danger','warn','warning') for x in risks):
            return False,'RUGCHECK_WARNING',evidence
        return True,'SECURITY_SCREEN_PASSED',evidence
    except Exception as exc:
        return False,'SECURITY_UNAVAILABLE',dict(error=type(exc).__name__)


def observe(db, mint, pair):
    c=db.cfg; ts=now_ms()
    try:
        price=float(pair['priceUsd']); liq=float(pair['liquidity']['usd'])
        volume=float(pair['volume']['m5']); tx=pair['txns']['m5']
        rise=float(pair['priceChange']['m5']); created=float(pair['pairCreatedAt'])
        if not all(math.isfinite(x) for x in (price,liq,volume,rise,created)) or price<=0: raise ValueError()
        evidence=dict(price=price,liquidity=liq,volume_m5=volume,buys=tx['buys'],sells=tx['sells'],rise_m5=rise)
    except (KeyError,TypeError,ValueError):
        db.event(mint,'MARKET','INVALID_PAIR')
        with db.lock,db.cx: db.s['watch'].pop(mint,None); db.save()
        return False,{}
    reasons=[]
    if liq<c['min_liquidity_usd']: reasons.append('LOW_LIQUIDITY')
    if volume<c['min_volume_m5_usd']: reasons.append('LOW_VOLUME')
    if tx['sells']<c['min_sells_m5']: reasons.append('LOW_SELL_ACTIVITY')
    if tx['buys']<=tx['sells']: reasons.append('BUY_FLOW_WEAK')
    if (ts-created)/1000<c['min_pair_age_sec']: reasons.append('PAIR_TOO_NEW')
    if not 0<rise<=c['max_m5_rise_pct']: reasons.append('MOMENTUM_OUTSIDE_RANGE')
    db.event(mint,'MARKET','PASS' if not reasons else '|'.join(reasons),evidence)
    with db.lock,db.cx:
        if reasons:
            db.s['watch'].pop(mint,None); db.save(); return False,evidence
        w=db.s['watch'].get(mint)
        if w and ts-w['last_ms']<c['scan_seconds']*1000: return False,evidence
        if not w or ts-w['last_ms']>c['watch_gap_sec']*1000 or w['pair']!=pair['pairAddress']:
            w=dict(first_ms=ts,last_ms=ts,n=0,price=price,liquidity=liq,pair=pair['pairAddress'])
        stable=(w['price']<=price<=w['price']*(1+c['watch_max_rise_pct']/100)
            and liq>=w['liquidity']*(1-c['watch_max_liquidity_drop_pct']/100))
        if not stable:
            db.s['watch'].pop(mint,None); db.save()
            db.event(mint,'WATCH','RESET_UNSTABLE',evidence); return False,evidence
        w['n']+=1; w['last_ms']=ts; db.s['watch'][mint]=w; db.save()
        ready=w['n']>=c['watch_samples'] and ts-w['first_ms']>=c['watch_span_sec']*1000
    db.event(mint,'WATCH','CONFIRMED' if ready else 'WAIT',w)
    return ready,evidence


def heartbeat(db, name, status):
    with db.lock,db.cx:
        db.s['heartbeat'][name]=dict(ts_ms=now_ms(),status=status); db.save()


def scanner(db, stop):
    md=MarketData(); exp=Experiment(db,md); cursor=0
    while not stop.is_set():
        heartbeat(db,'scanner','RUNNING')
        try:
            listings,counts=md.discovery(); db.event('','DISCOVERY','RESULT',counts)
            mints=list(dict.fromkeys([x.get('address') or x.get('mint') or x.get('tokenAddress') for x in listings]))
            watched=list(db.snapshot()['watch'])
            # Rotate discovery; existing watches remain eligible even if no longer promoted.
            mints=[m for m in mints if m]
            rotated=mints[cursor:]+mints[:cursor]; cursor=(cursor+20)%max(1,len(mints))
            selected=list(dict.fromkeys(watched+rotated))[:40]
            for mint in selected:
                if stop.is_set(): break
                if mint in db.snapshot()['seen']: continue
                heartbeat(db,'scanner','RUNNING')
                pair=md.dex_pair(mint)
                if not pair:
                    db.event(mint,'MARKET','PAIR_UNAVAILABLE')
                    with db.lock,db.cx: db.s['watch'].pop(mint,None); db.save()
                    continue
                ready,evidence=observe(db,mint,pair)
                if not ready: continue
                budget,reason=db.allocation(); db.event(mint,'RISK',reason,dict(budget=budget))
                if not budget: continue
                safe,reason,proof=security(md,mint,db.cfg); db.event(mint,'SECURITY',reason,proof)
                if not safe: continue
                symbol=(pair.get('baseToken') or {}).get('symbol') or mint[:8]
                outcome=exp.open_cohort(mint,symbol,evidence['price'],proof['decimals'])
                db.event(mint,'DECISION',outcome)
            heartbeat(db,'scanner','IDLE')
        except Exception as exc:
            db.event('','SCANNER','ERROR',dict(error=type(exc).__name__))
            heartbeat(db,'scanner','ERROR')
        stop.wait(db.cfg['scan_seconds'])


def exit_monitor(db, stop):
    # Separate HTTP sessions per position; at most six concurrent workers.
    managers={}
    def tick(p):
        try:
            if p['id'] not in managers: managers[p['id']]=Experiment(db,MarketData())
            managers[p['id']].exit_one(p['id'])
        except Exception as exc:
            db.event(p['mint'],'EXIT','ERROR',dict(error=type(exc).__name__,policy=p['policy']))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        while not stop.is_set():
            started=time.monotonic(); heartbeat(db,'exits','RUNNING')
            opened=[p for p in db.snapshot()['positions'] if int(p['remaining_raw'])]
            list(pool.map(tick,opened))
            db.sample_equity(); heartbeat(db,'exits','IDLE')
            stop.wait(max(0,db.cfg['exit_seconds']-(time.monotonic()-started)))


def main():
    import signal
    lock_file=open(db_path().with_suffix('.worker.lock'),'w')
    fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)  # One writer deployment per volume.
    db=Store(); stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:stop.set())
    threads=[threading.Thread(target=f,args=(db,stop),daemon=True) for f in (scanner,exit_monitor)]
    for t in threads:t.start()
    while not stop.wait(1):
        if not all(t.is_alive() for t in threads): raise RuntimeError('Worker thread stopped')
    for t in threads:t.join(timeout=20)

if __name__=='__main__':main()
