"""V7.6 CURVE+DEX BASELINE PAPER worker. No wallet, no signing, no real execution."""
import concurrent.futures, fcntl, math, os, threading, time
# Isolate V7.5 experiment in its own persistent subdirectory.
os.environ['DATA_DIR']=os.path.join(os.getenv('DATA_DIR','/data'),'v76')
from engine import Store, Experiment, CFG, SUPPORTED_TOKEN_PROGRAMS, now_ms, db_path
from market import MarketData
from pump_curve import PumpCurve
from telegram_status import configured as telegram_configured, status_text, send

CURVE_FEE_BPS=125


def security_curve(md,mint):
    """Pre-graduation checks: verify mint program and authorities. LP/holder tests do not apply to Pump curve inventory."""
    try:
        endpoint=os.getenv('SOLANA_RPC_URL') or (f'https://mainnet.helius-rpc.com/?api-key={md.helius_key}' if md.helius_key else '')
        if not endpoint:return False,'RPC_KEY_MISSING',{}
        r=md.s.post(endpoint,json={'jsonrpc':'2.0','id':1,'method':'getAccountInfo','params':[mint,{'encoding':'jsonParsed','commitment':'confirmed'}]},timeout=7); r.raise_for_status(); j=r.json(); account=(j.get('result') or {}).get('value')
        if not account:return False,'MINT_ACCOUNT_MISSING',{}
        owner=account.get('owner'); info=((account.get('data') or {}).get('parsed') or {}).get('info') or {}
        if owner not in SUPPORTED_TOKEN_PROGRAMS:return False,'TOKEN_PROGRAM_UNSUPPORTED',{'token_program':owner}
        if 'mintAuthority' not in info or 'freezeAuthority' not in info:return False,'AUTHORITY_UNKNOWN',{}
        if info.get('mintAuthority') or info.get('freezeAuthority'):return False,'ACTIVE_AUTHORITY',{}
        evidence={'token_program':owner,'decimals':int(info.get('decimals',6)),'venue':'PUMP_CURVE'}
        # RugCheck is advisory here except explicit danger. Early curves often have no conventional LP yet.
        try:
            rr=md.s.get(f'https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary',timeout=5)
            if rr.ok:
                report=rr.json(); levels=[str(x.get('level','')).lower() for x in (report.get('risks') or [])]; evidence['rugcheck_levels']=levels
                if any(x=='danger' for x in levels):return False,'RUGCHECK_DANGER',evidence
        except Exception:pass
        return True,'CURVE_SECURITY_PASSED',evidence
    except Exception as exc:return False,'SECURITY_UNAVAILABLE',{'error':type(exc).__name__}


def security_dex(md,mint,cfg):
    try:
        endpoint=os.getenv('SOLANA_RPC_URL') or (f'https://mainnet.helius-rpc.com/?api-key={md.helius_key}' if md.helius_key else '')
        if not endpoint:return False,'RPC_KEY_MISSING',{}
        def rpc(method,params):
            r=md.s.post(endpoint,json={'jsonrpc':'2.0','id':1,'method':method,'params':params},timeout=8);r.raise_for_status();j=r.json()
            if 'error' in j or 'result' not in j:raise ValueError('RPC_UNAVAILABLE')
            return j['result']
        account=rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}])['value']
        if not account:return False,'MINT_ACCOUNT_MISSING',{}
        owner=account.get('owner'); info=account['data']['parsed']['info']
        if owner not in SUPPORTED_TOKEN_PROGRAMS:return False,'TOKEN_PROGRAM_UNSUPPORTED',{'token_program':owner}
        if info.get('mintAuthority') or info.get('freezeAuthority'):return False,'ACTIVE_AUTHORITY',{}
        supply=int(info['supply']); holders=rpc('getTokenLargestAccounts',[mint,{'commitment':'confirmed'}])['value']
        if not holders or supply<=0:return False,'HOLDERS_UNKNOWN',{}
        largest=100*max(int(h['amount']) for h in holders)/supply; evidence={'decimals':int(info['decimals']),'largest_token_account_pct':largest,'token_program':owner,'venue':'DEX'}
        if largest>cfg['max_token_account_pct']:return False,'LARGE_TOKEN_ACCOUNT_INCLUDING_POOLS',evidence
        r=md.s.get(f'https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary',timeout=8);r.raise_for_status();report=r.json();evidence['rugcheck']=report
        locked=float(report['lpLockedPct']); levels=[str(x.get('level','')).lower() for x in (report.get('risks') or [])]
        if not math.isfinite(locked) or locked<cfg['min_lp_locked_pct']:return False,'LP_LOCK_LOW',evidence
        if any(x=='danger' for x in levels):return False,'RUGCHECK_DANGER',evidence
        return True,'SECURITY_SCREEN_PASSED',evidence
    except Exception as exc:return False,'SECURITY_UNAVAILABLE',{'error':type(exc).__name__}


def observe_dex(db,mint,pair):
    c=db.cfg;ts=now_ms()
    try:
        price=float(pair.get('priceUsd') or 0);liq=float((pair.get('liquidity') or {}).get('usd') or 0);volume=float((pair.get('volume') or {}).get('m5') or 0);tx=(pair.get('txns') or {}).get('m5') or {};buys=int(tx.get('buys') or 0);sells=int(tx.get('sells') or 0);rise=float((pair.get('priceChange') or {}).get('m5') or 0);created=float(pair.get('pairCreatedAt') or ts)
        if not all(math.isfinite(x) for x in (price,liq,volume,rise,created)) or price<=0:raise ValueError()
        e={'price':price,'liquidity':liq,'volume_m5':volume,'buys':buys,'sells':sells,'rise_m5':rise,'dex':pair.get('dexId'),'pair':pair.get('pairAddress')}
    except Exception:db.event(mint,'MARKET','INVALID_PAIR');return False,{}
    reasons=[]
    if liq<c['min_liquidity_usd']:reasons.append('LOW_LIQUIDITY')
    if volume<c['min_volume_m5_usd']:reasons.append('LOW_VOLUME')
    if sells<c['min_sells_m5']:reasons.append('LOW_SELL_ACTIVITY')
    if buys<=sells:reasons.append('BUY_FLOW_WEAK')
    if (ts-created)/1000<c['min_pair_age_sec']:reasons.append('PAIR_TOO_NEW')
    if not 0<rise<=c['max_m5_rise_pct']:reasons.append('MOMENTUM_OUTSIDE_RANGE')
    db.event(mint,'MARKET','PASS' if not reasons else '|'.join(reasons),e)
    return not reasons,e


class HybridExperiment(Experiment):
    def __init__(self,db,md):super().__init__(db,md);self.pump=PumpCurve()
    def open_curve(self,mint,symbol,strength=1.0):
        budget,reason=self.db.allocation(strength)
        if not budget:return reason
        network=float(self.c['network_fee_usd']); trade=max(0,budget-network)
        q=self.pump.buy_usd(mint,trade,CURVE_FEE_BPS);self.db.event(mint,'CURVE_QUOTE','ENTRY_BUY',q)
        if not q.get('ok'):return q.get('reason','CURVE_BUY_UNAVAILABLE')
        raw=int(q['token_raw']); mark=self.pump.sell_raw(mint,raw,CURVE_FEE_BPS);self.db.event(mint,'CURVE_QUOTE','ENTRY_ROUNDTRIP',mark)
        if not mark.get('ok'):return mark.get('reason','CURVE_ROUNDTRIP_UNAVAILABLE')
        mark_net=max(0,float(mark['out_usd'])-network); loss=100*(1-mark_net/budget)
        if loss>max(8.0,float(self.c['max_roundtrip_loss_pct'])):return 'CURVE_ROUNDTRIP_COST_HIGH'
        with self.db.lock,self.db.cx:
            allowed,why=self.db.allocation(strength)
            if allowed+1e-8<budget:return why if not allowed else 'RISK_CHANGED'
            if mint in self.db.s['seen']:return 'ALREADY_TRADED'
            policy=next(iter(self.db.s['accounts']))
            self.db.s['positions'].append({'id':f'{mint}:{policy}','mint':mint,'symbol':symbol or mint[:8],'policy':policy,'venue':'PUMP_CURVE','entry_ms':now_ms(),'initial_raw':str(raw),'remaining_raw':str(raw),'decimals':6,'cost':budget,'reserve':network,'received':0,'fees':network,'half_done':False,'peak_total':budget,'entry_mark_gross':float(mark['out_usd']),'entry_baseline_net':mark_net,'mark_net':mark_net,'mark_ms':now_ms(),'mae_pct':0.0,'mfe_pct':0.0,'mark_count':1,'pending':'','exit_ms':None})
            self.db.s['accounts'][policy]['cash']-=budget;self.db.s['seen'].append(mint);self.db.save()
        self.db.event(mint,'ENTRY','CURVE_OPENED',{'budget':budget,'roundtrip_pct':loss,'fee_bps':CURVE_FEE_BPS,'venue':'PUMP_CURVE'});return 'CURVE_OPENED'
    def exit_one(self,position_id):
        p=next(x for x in self.db.snapshot()['positions'] if x['id']==position_id)
        if p.get('venue')!='PUMP_CURVE':return super().exit_one(position_id)
        raw=int(p['remaining_raw']);
        if not raw:return
        network=float(self.c['network_fee_usd']); mark=self.pump.sell_raw(p['mint'],raw,CURVE_FEE_BPS)
        # Graduation: switch exit marking to Jupiter/PumpSwap automatically.
        if not mark.get('ok') and mark.get('reason')=='CURVE_COMPLETE':
            q=self.md.quote(p['mint'],'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',raw)
            if q.get('ok'):mark={'ok':True,'out_usd':q['min_raw']/1e6,'ts_ms':q.get('ts_ms',now_ms()),'venue':'JUPITER_AFTER_GRADUATION'}
        self.db.event(p['mint'],'CURVE_QUOTE','MARK',mark)
        if not mark.get('ok'):
            with self.db.lock,self.db.cx:
                d=next(x for x in self.db.s['positions'] if x['id']==p['id']);d['mark_net']=None;self.db.save()
            return
        net=max(0,float(mark['out_usd'])-network);total=p['received']+net;pnl=100*(total/p['cost']-1);reason=''
        if pnl<=-float(self.c['stop_loss_pct']):reason='NET_STOP_LOSS'
        elif pnl>=float(self.c.get('take_profit_net_pct',1.5)):reason='NET_TAKE_PROFIT'
        with self.db.lock,self.db.cx:
            d=next(x for x in self.db.s['positions'] if x['id']==p['id']);d.update(mark_net=net,mark_ms=now_ms(),peak_total=max(p['peak_total'],total),mae_pct=mae,mfe_pct=mfe,mark_count=int(d.get('mark_count',0))+1)
            if reason:
                d['received']+=net;d['fees']+=network;d['remaining_raw']='0';d['mark_net']=0;d['exit_ms']=now_ms();d['reserve']=0;self.db.s['accounts'][p['policy']]['cash']+=net
                self.db.cx.execute('INSERT INTO events(ts_ms,mint,stage,reason,payload) VALUES(?,?,?,?,?)',(now_ms(),p['mint'],'SALE',reason,'{}'))
            self.db.save()


def heartbeat(db,name,status):
    with db.lock,db.cx:db.s['heartbeat'][name]={'ts_ms':now_ms(),'status':status};db.save()


def scanner(db,stop):
    md=MarketData();exp=HybridExperiment(db,md);pump=PumpCurve();cursor=0;curve_watch={};security_cache={}
    while not stop.is_set():
        command_id=db.claim_scan();status='DONE';heartbeat(db,'scanner','RUNNING')
        try:
            listings,counts=md.discovery();db.event('','DISCOVERY','RESULT',counts);mints=list(dict.fromkeys(x.get('address') or x.get('mint') or x.get('tokenAddress') for x in listings));mints=[m for m in mints if m];rot=mints[cursor:]+mints[:cursor];cursor=(cursor+25)%max(1,len(mints));selected=rot[:int(db.cfg.get('max_candidates_per_scan',100))]
            for mint in selected:
                if stop.is_set():break
                if mint in db.snapshot()['seen']:continue
                # FAST CURVE lane first for Pump mints: no DEX pair required.
                if mint.endswith('pump'):
                    coin=pump.coin(mint)
                    if coin and not coin.get('complete') and pump.is_sol_curve(coin):
                        now=int(time.time());created=int((coin.get('created_timestamp') or 0)/1000 if (coin.get('created_timestamp') or 0)>10**12 else (coin.get('created_timestamp') or 0));last=int(coin.get('last_trade_timestamp') or 0);mc=float(coin.get('usd_market_cap') or 0);age=now-created if created else 999999;fresh_trade=now-last if last else 999999
                        prev=curve_watch.get(mint);delta=0 if not prev or prev[0]<=0 else 100*(mc/prev[0]-1);curve_watch[mint]=(mc,now_ms())
                        ev={'venue':'PUMP_CURVE','market_cap_usd':mc,'age_sec':age,'last_trade_age_sec':fresh_trade,'momentum_pct':delta}
                        if not (8<=age<=1800):db.event(mint,'CURVE','CURVE_AGE_WAIT',ev);continue
                        if mc<4000:db.event(mint,'CURVE','CURVE_MC_LOW',ev);continue
                        if fresh_trade>60:db.event(mint,'CURVE','CURVE_TRADE_STALE',ev);continue
                        if not prev:db.event(mint,'CURVE','CURVE_WARMUP',ev);continue
                        if not 0.15<=delta<=25:db.event(mint,'CURVE','CURVE_MOMENTUM_WAIT',ev);continue
                        cached=security_cache.get(mint)
                        if cached and now_ms()-cached[0]<600000:safe,why,proof=cached[1:]
                        else:safe,why,proof=security_curve(md,mint);security_cache[mint]=(now_ms(),safe,why,proof)
                        db.event(mint,'SECURITY_CURVE',why,proof)
                        if safe:
                            strength=2.0 if delta>=3 else (1.5 if delta>=1 else 1.0);out=exp.open_curve(mint,coin.get('symbol') or mint[:8],strength);db.event(mint,'DECISION',out,ev)
                        continue
                pair=md.dex_pair(mint)
                if not pair:db.event(mint,'MARKET','PAIR_UNAVAILABLE');continue
                ready,e=observe_dex(db,mint,pair)
                if not ready:continue
                safe,why,proof=security_dex(md,mint,db.cfg);db.event(mint,'SECURITY',why,proof)
                if not safe:continue
                strength=max(.5,min(2,(e.get('buys',0)/max(1,e.get('sells',1)))/2));out=exp.open_cohort(mint,(pair.get('baseToken') or {}).get('symbol') or mint[:8],e['price'],proof['decimals'],strength);db.event(mint,'DECISION',out)
            heartbeat(db,'scanner','IDLE')
        except Exception as exc:status='ERROR';db.event('','SCANNER','ERROR',{'error':type(exc).__name__});heartbeat(db,'scanner','ERROR')
        db.finish_scan(command_id,status);stop.wait(db.cfg['scan_seconds'])


def exit_monitor(db,stop):
    managers={}
    def tick(p):
        try:
            if p['id'] not in managers:managers[p['id']]=HybridExperiment(db,MarketData())
            managers[p['id']].exit_one(p['id'])
        except Exception as exc:db.event(p['mint'],'EXIT','ERROR',{'error':type(exc).__name__})
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while not stop.is_set():
            started=time.monotonic();heartbeat(db,'exits','RUNNING');opened=[p for p in db.snapshot()['positions'] if int(p['remaining_raw'])];list(pool.map(tick,opened));db.sample_equity();heartbeat(db,'exits','IDLE');stop.wait(max(0,db.cfg['exit_seconds']-(time.monotonic()-started)))


def hourly_telegram_report(db,stop):
    interval=max(60,int(db.cfg.get('telegram_report_interval_sec',3600)))
    while not stop.wait(interval):
        heartbeat(db,'telegram_report','RUNNING')
        if not telegram_configured():heartbeat(db,'telegram_report','NOT_CONFIGURED');continue
        try:ok,detail=send('V7.6 CURVE+DEX BASELINE PAPER\n'+status_text(db,db.snapshot()));db.event('','TELEGRAM_REPORT','SENT' if ok else 'FAILED',{'detail':detail});heartbeat(db,'telegram_report','IDLE' if ok else 'ERROR')
        except Exception as exc:db.event('','TELEGRAM_REPORT','ERROR',{'error':type(exc).__name__});heartbeat(db,'telegram_report','ERROR')


def main():
    import signal
    lock_file=open(db_path().with_suffix('.worker.lock'),'w');fcntl.flock(lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB);db=Store();stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    threads=[threading.Thread(target=f,args=(db,stop),daemon=True) for f in (scanner,exit_monitor,hourly_telegram_report)]
    for t in threads:t.start()
    while not stop.wait(1):
        if not all(t.is_alive() for t in threads):raise RuntimeError('Worker thread stopped')
    for t in threads:t.join(timeout=20)
if __name__=='__main__':main()
