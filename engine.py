"""V7.6 BASELINE + MAE/MFE PAPER experiment; deliberately no wallet/execution API."""
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text())
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POLICIES = ('V7_9_WAVE_CURVE_DEX',)
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022_PROGRAM = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
SUPPORTED_TOKEN_PROGRAMS = {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}
def now_ms(): return int(time.time() * 1000)
def encode(value): return json.dumps(value, ensure_ascii=False, allow_nan=False)
def db_path():
    d = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    d.mkdir(parents=True, exist_ok=True)
    return d / 'v7_9_4_security.sqlite3'

class Store:
    def __init__(self, path=None, cfg=None):
        self.cfg = copy.deepcopy(cfg or CFG); c=self.cfg
        if not (0 < c['risk_fraction'] <= 1 and 1 <= c['max_open_positions'] <= 100): raise ValueError('Invalid risk limits')
        if c['network_fee_usd'] < 0 or c['quote_slippage_bps'] < 0: raise ValueError('Invalid expenses')
        self.lock=threading.RLock(); self.cx=sqlite3.connect(path or db_path(),check_same_thread=False,timeout=15)
        self.cx.execute('PRAGMA journal_mode=WAL')
        self.cx.executescript('''CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT); CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts_ms INTEGER,mint TEXT,stage TEXT,reason TEXT,payload TEXT); CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY,ts_ms INTEGER,policy TEXT,cash REAL,equity REAL,unknown INTEGER); CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY,ts_ms INTEGER,action TEXT,status TEXT,detail TEXT);''')
        digest=hashlib.sha256(encode(c).encode()).hexdigest(); row=self.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row:
            self.s=json.loads(row[0])
            if self.s['config_hash'] != digest: raise RuntimeError('CONFIG_CHANGED: use a new DATA_DIR; preserve old experiment')
        else:
            self.s=dict(config_hash=digest,config=c,started_ms=now_ms(),watch={},positions=[],seen=[],paused=False,heartbeat={},accounts={k:dict(cash=c['paper_capital_usd'],peak=c['paper_capital_usd'],max_drawdown=0) for k in POLICIES}); self.save()
        self.cx.commit()
    def save(self): self.cx.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(encode(self.s),))
    def event(self,mint,stage,reason,payload=None):
        with self.lock,self.cx:self.cx.execute('INSERT INTO events(ts_ms,mint,stage,reason,payload) VALUES(?,?,?,?,?)',(now_ms(),mint,stage,reason,encode(payload or {})))
    def snapshot(self):
        with self.lock:return copy.deepcopy(self.s)
    def request_scan(self):
        with self.lock,self.cx:
            self.cx.execute('BEGIN IMMEDIATE'); row=self.cx.execute("SELECT id,status FROM commands WHERE action='SCAN' AND status IN ('QUEUED','RUNNING') ORDER BY id DESC LIMIT 1").fetchone()
            if row:return row[0],row[1]
            cur=self.cx.execute("INSERT INTO commands(ts_ms,action,status,detail) VALUES(?,'SCAN','QUEUED','')",(now_ms(),)); return cur.lastrowid,'QUEUED'
    def claim_scan(self):
        with self.lock,self.cx:
            self.cx.execute('BEGIN IMMEDIATE'); row=self.cx.execute("SELECT id FROM commands WHERE action='SCAN' AND status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if not row:return None
            self.cx.execute("UPDATE commands SET status='RUNNING' WHERE id=?",(row[0],)); return row[0]
    def finish_scan(self,command_id,status):
        if command_id is not None:
            with self.lock,self.cx:self.cx.execute('UPDATE commands SET status=?,detail=? WHERE id=?',(status,str(now_ms()),command_id))
    def stats(self,s=None):
        s=s or self.snapshot(); result=[]
        for policy,a in s['accounts'].items():
            positions=[p for p in s['positions'] if p['policy']==policy]; opened=[p for p in positions if int(p['remaining_raw'])>0]; closed=[p for p in positions if not int(p['remaining_raw'])]
            unknown=sum(p['mark_net'] is None or now_ms()-p['mark_ms']>self.cfg['mark_max_age_ms'] for p in opened); eq=None if unknown else a['cash']+sum(p['mark_net'] for p in opened)
            result.append(dict(policy=policy,cash=a['cash'],equity=eq,net_pnl=None if eq is None else eq-self.cfg['paper_capital_usd'],closed_pnl=sum(p['received']-p['cost'] for p in closed),open=len(opened),closed=len(closed),unknown=unknown,winners=sum(p['received']>p['cost'] for p in closed),network_fees=sum(p['fees'] for p in positions),max_observed_drawdown_pct=100*a['max_drawdown']))
        return result
    def sample_equity(self):
        with self.lock,self.cx:
            for r in self.stats(self.s):
                self.cx.execute('INSERT INTO equity(ts_ms,policy,cash,equity,unknown) VALUES(?,?,?,?,?)',(now_ms(),r['policy'],r['cash'],r['equity'],r['unknown']))
                if r['equity'] is not None:
                    a=self.s['accounts'][r['policy']]; a['peak']=max(a['peak'],r['equity']); a['max_drawdown']=max(a['max_drawdown'],1-r['equity']/a['peak'])
                    if r['equity']<=self.cfg['paper_capital_usd']*(1-self.cfg['experiment_loss_limit_fraction']):self.s['paused']=True
            self.save()
    def allocation(self,strength=1.0):
        s=self.snapshot()
        if s['paused']:return 0,'EXPERIMENT_LOSS_PAUSE'
        r=self.stats(s)[0]
        if r['open']>=self.cfg['max_open_positions']:return 0,'POSITION_LIMIT'
        reserved=sum(p['reserve'] for p in s['positions'] if int(p['remaining_raw'])); free=max(0,r['cash']-reserved); equity=r['equity'] if r['equity'] is not None else self.cfg['paper_capital_usd']
        frac=self.cfg['strong_position_fraction'] if strength>=1.5 else self.cfg['base_position_fraction']; frac=min(frac,self.cfg['max_position_fraction']); target=min(free,max(self.cfg['min_position_usd'],equity*frac)); reserve_slots=max(0,min(5,self.cfg['max_open_positions']-r['open']-1)); keep=reserve_slots*self.cfg['min_position_usd']; budget=math.floor(min(target,max(0,free-keep))*1e6)/1e6
        if budget<=3*self.cfg['network_fee_usd']:return 0,'COST_EXCEEDS_BUDGET'
        return budget,'ADAPTIVE_RISK_OK'

def fresh(q,cfg):return q.get('ok') and 0<=now_ms()-q.get('ts_ms',0)<=cfg['quote_max_age_ms']

class Experiment:
    def __init__(self,db,md):self.db,self.md,self.c=db,md,db.cfg
    def quote(self,mint,purpose,raw,buy=False):
        q=self.md.quote(USDC_MINT if buy else mint,mint if buy else USDC_MINT,raw); self.db.event(mint,'QUOTE',purpose,q); return q
    def open_cohort(self,mint,symbol,price,decimals,strength=1.0):
        budget,reason=self.db.allocation(strength)
        if not budget:return reason
        fee=self.c['network_fee_usd']; cost=round(budget-2*fee,6); buy_raw=math.floor(round(cost-fee,6)*1e6); buy=self.quote(mint,'ENTRY_BUY',buy_raw,True)
        if not fresh(buy,self.c):return 'BUY_QUOTE_UNAVAILABLE'
        raw=int(buy['min_raw'])
        if raw<2:return 'QUANTITY_TOO_SMALL'
        sell=self.quote(mint,'ENTRY_ROUNDTRIP',raw)
        if not fresh(sell,self.c) or not fresh(buy,self.c):return 'ROUNDTRIP_QUOTE_UNAVAILABLE'
        net=sell['min_raw']/1e6-fee; loss=100*(1-net/cost)
        if loss>self.c['max_roundtrip_loss_pct']:return 'ROUNDTRIP_COST_HIGH'
        if max(buy['impact_pct'],sell['impact_pct'])>self.c['max_price_impact_pct']:return 'IMPACT_HIGH'
        execution_price=buy_raw/1e6/(raw/10**decimals)
        if price<=0 or abs(execution_price/price-1)*100>self.c['max_entry_chase_pct']:return 'SOURCE_DIVERGENCE'
        with self.db.lock,self.db.cx:
            allowed,reason=self.db.allocation(strength)
            if allowed+1e-8<budget:return reason if not allowed else 'RISK_CHANGED'
            if mint in self.db.s['seen']:return 'ALREADY_TRADED'
            if not fresh(buy,self.c) or not fresh(sell,self.c):return 'QUOTE_EXPIRED'
            for policy in POLICIES:
                self.db.s['positions'].append(dict(id=f'{mint}:{policy}',mint=mint,symbol=symbol,policy=policy,entry_ms=now_ms(),initial_raw=str(raw),remaining_raw=str(raw),decimals=decimals,cost=cost,reserve=2*fee,received=0,fees=fee,half_done=False,peak_total=cost,entry_mark_gross=sell['min_raw']/1e6,entry_baseline_net=net,mark_net=net,mark_ms=sell['ts_ms'],mae_pct=0.0,mfe_pct=0.0,mark_count=1,pending='',exit_ms=None)); self.db.s['accounts'][policy]['cash']-=cost
            self.db.s['seen'].append(mint); self.db.s['watch'].pop(mint,None); self.db.save()
        self.db.event(mint,'ENTRY','COHORT_OPENED',dict(budget=budget,cost=cost,raw=str(raw),roundtrip_pct=loss)); return 'COHORT_OPENED'
    def exit_one(self,position_id):
        p=next(x for x in self.db.snapshot()['positions'] if x['id']==position_id); raw=int(p['remaining_raw'])
        if not raw:return
        fee=self.c['network_fee_usd']; mark=self.quote(p['mint'],'MARK_'+p['policy'],raw)
        if not fresh(mark,self.c):
            with self.db.lock,self.db.cx:
                dest=next(x for x in self.db.s['positions'] if x['id']==p['id']); dest['mark_net']=None; self.db.save()
            self.db.event(p['mint'],'EXIT','QUOTE_UNAVAILABLE',dict(policy=p['policy'])); return
        net=mark['min_raw']/1e6-fee; total=p['received']+net; peak=max(p['peak_total'],total); reason=p['pending']; pnl_pct=100*(total/p['cost']-1) if p['cost']>0 else -100
        baseline=float(p.get('entry_baseline_net') or p.get('mark_net') or net)
        move_pct=100*(net/baseline-1) if baseline>0 else 0.0
        mae=min(float(p.get('mae_pct',0.0)),move_pct); mfe=max(float(p.get('mfe_pct',0.0)),move_pct)
        self.db.event(p['mint'],'PATH','EXECUTABLE_MARK',dict(policy=p['policy'],net=net,pnl_pct=pnl_pct,move_from_baseline_pct=move_pct,mae_pct=mae,mfe_pct=mfe,quote_latency_ms=max(0,now_ms()-mark.get('ts_ms',now_ms()))))
        if not reason and move_pct<=-float(self.c.get('baseline_stop_pct',4.0)):reason='BASELINE_STOP'
        if not reason and self.c.get('max_hold_sec',0)>0 and now_ms()-p['entry_ms']>=self.c['max_hold_sec']*1000:reason='TIME_EXIT'
        sale,qty=None,raw
        if reason:sale=mark
        elif not p['half_done'] and pnl_pct>=self.c.get('take_profit_net_pct',1.5):reason='NET_TAKE_PROFIT'; sale=mark; qty=raw
        with self.db.lock,self.db.cx:
            dest=next(x for x in self.db.s['positions'] if x['id']==p['id'])
            if dest['remaining_raw']!=p['remaining_raw']:return
            dest.update(mark_net=net,mark_ms=mark['ts_ms'],peak_total=peak,mae_pct=mae,mfe_pct=mfe,mark_count=int(dest.get('mark_count',0))+1)
            if reason in ('BASELINE_STOP','TIME_EXIT','NET_TAKE_PROFIT'):dest['pending']=reason
            if sale and fresh(sale,self.c):
                proceeds=sale['min_raw']/1e6-fee; dest['received']+=proceeds; dest['fees']+=fee; dest['reserve']=max(0,dest['reserve']-fee); dest['remaining_raw']=str(raw-qty); dest['half_done']=True; dest['mark_net']=None if raw>qty else 0; dest['pending']=''; self.db.s['accounts'][p['policy']]['cash']+=proceeds
                if raw==qty:dest['exit_ms']=now_ms(); dest['reserve']=0
                self.db.cx.execute('INSERT INTO events(ts_ms,mint,stage,reason,payload) VALUES(?,?,?,?,?)',(now_ms(),p['mint'],'SALE',reason,encode(dict(policy=p['policy'],raw=str(qty),net=proceeds,fee=fee))))
            self.db.save()

def export_excel(db,hours=None):
    from openpyxl import Workbook
    if hours is not None and (not isinstance(hours,(int,float)) or not math.isfinite(hours) or hours<=0):raise ValueError('Period must be positive hours')
    end=now_ms(); start=0 if hours is None else end-int(hours*3600000)
    with db.lock:
        db.cx.execute('BEGIN')
        try:
            s=json.loads(db.cx.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]); events=db.cx.execute('SELECT ts_ms,mint,stage,reason,payload FROM events WHERE ts_ms BETWEEN ? AND ? ORDER BY id',(start,end)).fetchall(); eq=db.cx.execute('SELECT ts_ms,policy,cash,equity,unknown FROM equity WHERE ts_ms BETWEEN ? AND ? ORDER BY id',(start,end)).fetchall()
        finally:db.cx.rollback()
        tables={'Report':[dict(period_hours=hours or 'ALL',from_ms=start or s['started_ms'],to_ms=end,timezone='UTC',summary_scope='CURRENT / SINCE EXPERIMENT START; NOT PERIOD PNL',positions_scope='Positions held at any time in selected interval; lifetime totals')],'Summary':db.stats(s),'Positions':[p for p in s['positions'] if p['entry_ms']<=end and (p['exit_ms'] is None or p['exit_ms']>=start)],'Watch':[dict(mint=k,**v) for k,v in s['watch'].items()],'Config':[dict(key=k,value=v) for k,v in db.cfg.items()],'Events':[dict(zip(('ts_ms','mint','stage','reason','payload'),r)) for r in events],'Equity':[dict(zip(('ts_ms','policy','cash','equity','unknown'),r)) for r in eq]}
    wb=Workbook(); wb.remove(wb.active)
    for name,rows in tables.items():
        ws=wb.create_sheet(name); headers=list(dict.fromkeys(k for r in rows for k in r)) or ['empty']; ws.append(headers)
        for row in rows:
            ws.append([encode(row.get(k)) if isinstance(row.get(k),(dict,list)) else row.get(k) for k in headers])
            for cell in ws[ws.max_row]:
                if isinstance(cell.value,str):cell.data_type='s'
        ws.freeze_panes='A2'; ws.auto_filter.ref=ws.dimensions
    b=io.BytesIO(); wb.save(b); return b.getvalue()
