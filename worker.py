"""V7.4 FAST CORE PAPER worker. No real execution."""
import concurrent.futures, fcntl, math, os, threading, time
from engine import Store, Experiment, CFG, SUPPORTED_TOKEN_PROGRAMS, now_ms, db_path
from market import MarketData
from telegram_status import configured as telegram_configured, status_text, send

def security(md,mint,cfg):
    """Critical checks stay fail-closed. RugCheck warnings are logged but only danger is a hard block."""
    try:
        endpoint=os.getenv('SOLANA_RPC_URL') or (f'https://mainnet.helius-rpc.com/?api-key={md.helius_key}' if md.helius_key else '')
        if not endpoint:return False,'RPC_KEY_MISSING',{}
        def rpc(method,params):
            r=md.s.post(endpoint,json=dict(jsonrpc='2.0',id=1,method=method,params=params),timeout=8); r.raise_for_status(); j=r.json()
            if 'error' in j or 'result' not in j:raise ValueError('RPC_UNAVAILABLE')
            return j['result']
        account=rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}])['value']
        if not account:return False,'MINT_ACCOUNT_MISSING',{}
        owner=account.get('owner')
        if owner not in SUPPORTED_TOKEN_PROGRAMS:return False,'TOKEN_PROGRAM_UNSUPPORTED',{'token_program':owner}
        info=account['data']['parsed']['info']
        if 'mintAuthority' not in info or 'freezeAuthority' not in info:return False,'AUTHORITY_UNKNOWN',{}
        if info['mintAuthority'] or info['freezeAuthority']:return False,'ACTIVE_AUTHORITY',{}
        supply=int(info['supply']); decimals=int(info['decimals'])
        if supply<=0 or not 0<=decimals<=18:return False,'INVALID_SUPPLY',{}
        holders=rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}])['value']
        if not holders:return False,'HOLDERS_UNKNOWN',{}
        largest=100*max(int(h['amount']) for h in holders)/supply
        evidence=dict(decimals=decimals,largest_token_account_pct=largest,authority_mint=None,authority_freeze=None,token_program=owner)
        if largest>cfg['max_token_account_pct']:return False,'LARGE_TOKEN_ACCOUNT_INCLUDING_POOLS',evidence
        r=md.s.get(f'https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary',timeout=8); r.raise_for_status(); report=r.json(); evidence['rugcheck']=report
        locked=float(report['lpLockedPct'])
        if not math.isfinite(locked) or not 0<=locked<=100:return False,'LP_UNKNOWN',evidence
        if locked<cfg['min_lp_locked_pct']:return False,'LP_LOCK_LOW',evidence
        risks=report['risks']
        if not isinstance(risks,list):return False,'RISKS_UNKNOWN',evidence
        levels=[str(x.get('level','')).lower() for x in risks]
        evidence['rugcheck_levels']=levels
        if any(x=='danger' for x in levels):return False,'RUGCHECK_DANGER',evidence
        if any(x in ('warn','warning') for x in levels):return True,'SECURITY_PASSED_WITH_WARNING',evidence
        return True,'SECURITY_SCREEN_PASSED',evidence
    except Exception as exc:return False,'SECURITY_UNAVAILABLE',dict(error=type(exc).__name__)

def observe(db,mint,pair):
    c=db.cfg; ts=now_ms()
    try:
        price=float(pair.get('priceUsd') or 0); liq=float((pair.get('liquidity') or {}).get('usd') or 0); volume=float((pair.get('volume') or {}).get('m5') or 0); tx=(pair.get('txns') or {}).get('m5') or {}; buys=int(tx.get('buys') or 0); sells=int(tx.get('sells') or 0); rise=float((pair.get('priceChange') or {}).get('m5') or 0); created=float(pair.get('pairCreatedAt') or ts)
        if not all(math.isfinite(x) for x in (price,liq,volume,rise,created)) or price<=0:raise ValueError()
        evidence=dict(price=price,liquidity=liq,volume_m5=volume,buys=buys,sells=sells,rise_m5=rise,dex=pair.get('dexId'),pair=pair.get('pairAddress'))
    except (KeyError,TypeError,ValueError):
        db.event(mint,'MARKET','INVALID_PAIR');
        with db.lock,db.cx:db.s['watch'].pop(mint,None); db.save()
        return False,{}
    reasons=[]
    if liq<c['min_liquidity_usd']:reasons.append('LOW_LIQUIDITY')
    if volume<c['min_volume_m5_usd']:reasons.append('LOW_VOLUME')
    if sells<c['min_sells_m5']:reasons.append('LOW_SELL_ACTIVITY')
    if buys<=sells:reasons.append('BUY_FLOW_WEAK')
    if (ts-created)/1000<c['min_pair_age_sec']:reasons.append('PAIR_TOO_NEW')
    if not 0<rise<=c['max_m5_rise_pct']:reasons.append('MOMENTUM_OUTSIDE_RANGE')
    db.event(mint,'MARKET','PASS' if not reasons else '|'.join(reasons),evidence)
    with db.lock,db.cx:
        if reasons:db.s['watch'].pop(mint,None); db.save(); return False,evidence
        w=db.s['watch'].get(mint)
        if w and ts-w['last_ms']<c['scan_seconds']*1000:return False,evidence
        if not w or ts-w['last_ms']>c['watch_gap_sec']*1000 or w['pair']!=pair['pairAddress']:w=dict(first_ms=ts,last_ms=ts,n=0,price=price,liquidity=liq,pair=pair['pairAddress'])
        stable=(w['price']<=price<=w['price']*(1+c['watch_max_rise_pct']/100) and liq>=w['liquidity']*(1-c['watch_max_liquidity_drop_pct']/100))
        if not stable:db.s['watch'].pop(mint,None); db.save(); db.event(mint,'WATCH','RESET_UNSTABLE',evidence); return False,evidence
        w['n']+=1; w['last_ms']=ts; db.s['watch'][mint]=w; db.save(); ready=w['n']>=c['watch_samples'] and ts-w['first_ms']>=c['watch_span_sec']*1000
    db.event(mint,'WATCH','CONFIRMED' if ready else 'WAIT',w); return ready,evidence

def heartbeat(db,name,status):
    with db.lock,db.cx:db.s['heartbeat'][name]=dict(ts_ms=now_ms(),status=status); db.save()

def scanner(db,stop):
    md=MarketData(); exp=Experiment(db,md); cursor=0; security_cache={}
    with db.lock,db.cx:db.cx.execute("UPDATE commands SET status='INTERRUPTED',detail='worker restarted' WHERE status='RUNNING'")
    while not stop.is_set():
        command_id=db.claim_scan(); scan_status='DONE'; heartbeat(db,'scanner','RUNNING')
        try:
            listings,counts=md.discovery(); db.event('','DISCOVERY','RESULT',counts); mints=list(dict.fromkeys([x.get('address') or x.get('mint') or x.get('tokenAddress') for x in listings])); watched=list(db.snapshot()['watch']); mints=[m for m in mints if m]; rotated=mints[cursor:]+mints[:cursor]; cursor=(cursor+20)%max(1,len(mints)); selected=list(dict.fromkeys(watched+rotated))[:int(db.cfg.get('max_candidates_per_scan',100))]
            for mint in selected:
                if stop.is_set():break
                if mint in db.snapshot()['seen']:continue
                heartbeat(db,'scanner','RUNNING'); pair=md.dex_pair(mint)
                if not pair:
                    db.event(mint,'MARKET','PAIR_UNAVAILABLE');
                    with db.lock,db.cx:db.s['watch'].pop(mint,None); db.save()
                    continue
                ready,evidence=observe(db,mint,pair)
                if not ready:continue
                strength=max(0.5,min(2.0,(evidence.get('buys',0)/max(1,evidence.get('sells',1)))/2+min(1,evidence.get('volume_m5',0)/max(1,db.cfg['min_volume_m5_usd'])*0.25))); budget,reason=db.allocation(strength); db.event(mint,'RISK',reason,dict(budget=budget,strength=strength))
                if not budget:continue
                cached=security_cache.get(mint); ttl=int(db.cfg.get('security_cache_sec',600))*1000
                if cached and now_ms()-cached[0]<ttl:safe,reason,proof=cached[1:]; db.event(mint,'SECURITY_CACHE',reason,proof)
                else:safe,reason,proof=security(md,mint,db.cfg); security_cache[mint]=(now_ms(),safe,reason,proof); db.event(mint,'SECURITY',reason,proof)
                if not safe:continue
                symbol=(pair.get('baseToken') or {}).get('symbol') or mint[:8]; outcome=exp.open_cohort(mint,symbol,evidence['price'],proof['decimals'],strength); db.event(mint,'DECISION',outcome)
            heartbeat(db,'scanner','IDLE')
        except Exception as exc:scan_status='ERROR'; db.event('','SCANNER','ERROR',dict(error=type(exc).__name__)); heartbeat(db,'scanner','ERROR')
        db.finish_scan(command_id,scan_status); deadline=time.monotonic()+db.cfg['scan_seconds']
        while not stop.is_set() and time.monotonic()<deadline:
            with db.lock:pending=db.cx.execute("SELECT 1 FROM commands WHERE status='QUEUED' LIMIT 1").fetchone()
            if pending:break
            stop.wait(min(1,max(0,deadline-time.monotonic())))

def exit_monitor(db,stop):
    managers={}
    def tick(p):
        try:
            if p['id'] not in managers:managers[p['id']]=Experiment(db,MarketData())
            managers[p['id']].exit_one(p['id'])
        except Exception as exc:db.event(p['mint'],'EXIT','ERROR',dict(error=type(exc).__name__,policy=p['policy']))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while not stop.is_set():
            started=time.monotonic(); heartbeat(db,'exits','RUNNING'); opened=[p for p in db.snapshot()['positions'] if int(p['remaining_raw'])]; list(pool.map(tick,opened)); db.sample_equity(); heartbeat(db,'exits','IDLE'); stop.wait(max(0,db.cfg['exit_seconds']-(time.monotonic()-started)))

def hourly_telegram_report(db,stop):
    interval=max(60,int(db.cfg.get('telegram_report_interval_sec',3600)))
    while not stop.wait(interval):
        heartbeat(db,'telegram_report','RUNNING')
        if not telegram_configured():db.event('','TELEGRAM_REPORT','NOT_CONFIGURED'); heartbeat(db,'telegram_report','NOT_CONFIGURED'); continue
        try:
            ok,detail=send(status_text(db,db.snapshot())); db.event('','TELEGRAM_REPORT','SENT' if ok else 'FAILED',dict(detail=detail)); heartbeat(db,'telegram_report','IDLE' if ok else 'ERROR')
        except Exception as exc:db.event('','TELEGRAM_REPORT','ERROR',dict(error=type(exc).__name__)); heartbeat(db,'telegram_report','ERROR')

def main():
    import signal
    lock_file=open(db_path().with_suffix('.worker.lock'),'w'); fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB); db=Store(); stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    threads=[threading.Thread(target=f,args=(db,stop),daemon=True) for f in (scanner,exit_monitor,hourly_telegram_report)]
    for t in threads:t.start()
    while not stop.wait(1):
        if not all(t.is_alive() for t in threads):raise RuntimeError('Worker thread stopped')
    for t in threads:t.join(timeout=20)
if __name__=='__main__':main()
