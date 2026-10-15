"""BBB adaptive microflow dynamic filter (SHADOW / research only).

The filter observes every publicTrade and L50 book update, evaluates at 250ms,
and models a long reversal sequence:

SELL SHOCK -> ABSORPTION -> TURN -> 2-frame CONFIRM -> SHADOW SIGNAL.

Thresholds are adaptive to each symbol's own recent microflow baseline.
It never places orders. Every completed episode is labeled from future bid
movement and persisted for later WIN-vs-LOSS research.
"""
from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass, field
from storage_guard import enabled
import json, math, statistics, time
from typing import Deque

STEP_MS=250
BASELINE_MS=60_000
PRE_FRAME_MS=5_000
POST_LABEL_MS=20_000
STATE_TTL_MS=6_000
TP_BPS=80.0
SL_BPS=-30.0

def now_ms():
    return int(time.time()*1000)

def clamp(x,a,b):
    return max(a,min(b,x))

def median(vals,default=0.0):
    vals=[float(x) for x in vals if x is not None and math.isfinite(float(x))]
    return statistics.median(vals) if vals else default

def mad(vals,default=1.0):
    vals=[float(x) for x in vals if x is not None and math.isfinite(float(x))]
    if not vals:return default
    m=statistics.median(vals)
    d=statistics.median([abs(x-m) for x in vals])
    return d if d>1e-9 else default

def rz(value, vals):
    if value is None:return 0.0
    m=median(vals,0.0); d=mad(vals,1.0)
    return clamp((float(value)-m)/(1.4826*d),-6.0,6.0)

@dataclass
class TradeTick:
    ts:int
    side:str
    price:float
    notional:float

@dataclass
class BookTick:
    ts:int
    mid:float
    bid:float
    ask:float
    spread_bps:float
    bid1:float
    ask1:float
    bid5:float
    ask5:float
    imbalance:float

@dataclass
class ActiveState:
    stage:str
    shock_ms:int
    shock_mid:float
    shock_low:float
    shock_score:float
    absorb_score:float=0.0
    turn_score:float=0.0
    confirm_frames:int=0
    best_score:float=0.0
    reason:str=""
    pre_frames:list=field(default_factory=list)

@dataclass
class PendingEpisode:
    symbol:str
    shock_ms:int
    decision_ms:int
    decision_stage:str
    entry_ask:float
    shock_score:float
    absorb_score:float
    turn_score:float
    dynamic_score:float
    frames:list
    deadline_ms:int
    first_touch_ms:int|None=None
    outcome_label:str|None=None
    max_fav_bps:float=-1e9
    max_adv_bps:float=1e9
    exit_probe_bid:float|None=None
    council_snapshot:dict|None=None

class MicroFlowDynamic:
    def __init__(self,cx,session_id:str):
        self.cx=cx; self.session_id=session_id
        self.trades:dict[str,Deque[TradeTick]]=defaultdict(lambda:deque(maxlen=10000))
        self.books:dict[str,Deque[BookTick]]=defaultdict(lambda:deque(maxlen=20000))
        self.frames:dict[str,Deque[dict]]=defaultdict(lambda:deque(maxlen=600))
        self.active:dict[str,ActiveState]={}
        self.pending:list[PendingEpisode]=[]
        self.last_step=defaultdict(int)
        self.counts=defaultdict(int)
        self.signal_handler=None
        from broker_council import BrokerCouncil
        self.council=BrokerCouncil(cx,session_id)

    def _trim(self,symbol,ts):
        cut=ts-BASELINE_MS-10_000
        tr=self.trades[symbol]
        while tr and tr[0].ts<cut:tr.popleft()
        bk=self.books[symbol]
        while bk and bk[0].ts<cut:bk.popleft()

    def on_trade(self,symbol,ts_ms,price,size,side):
        try:
            p=float(price); q=float(size)
            if p<=0 or q<0:return
            self.trades[symbol].append(TradeTick(int(ts_ms),str(side).upper(),p,p*q))
        except Exception:
            return

    def on_book(self,symbol,ts_ms,book):
        try:
            if not book.synced or not book.mid:return
            b1=book.top("bid",1); a1=book.top("ask",1)
            self.books[symbol].append(BookTick(
                int(ts_ms),float(book.mid),float(book.best_bid),float(book.best_ask),
                float(book.spread_bps or 0.0),
                sum(p*q for p,q in b1),sum(p*q for p,q in a1),
                float(book.depth_usd("bid",5)),float(book.depth_usd("ask",5)),
                float(book.imbalance(5))
            ))
        except Exception:
            return

    def _trade_agg(self,symbol,ts,window):
        xs=[x for x in self.trades[symbol] if x.ts>=ts-window]
        buy=[x for x in xs if x.side=="BUY"]; sell=[x for x in xs if x.side=="SELL"]
        bu=sum(x.notional for x in buy); se=sum(x.notional for x in sell); tot=bu+se
        maxn=max((x.notional for x in xs),default=0.0)
        # longest same-side run in event order
        run=best=0; prev=None; best_side=None
        for x in xs:
            if x.side==prev:run+=1
            else:prev=x.side;run=1
            if run>best:best=run;best_side=x.side
        return {
          "count":len(xs),"buy_usd":bu,"sell_usd":se,"total_usd":tot,
          "flow":(bu-se)/tot if tot>0 else 0.0,
          "buy_share":bu/tot if tot>0 else .5,
          "sell_share":se/tot if tot>0 else .5,
          "avg_usd":tot/len(xs) if xs else 0.0,
          "max_usd":maxn,"run":best,"run_side":best_side or ""
        }

    def _book_at(self,symbol,target):
        arr=self.books[symbol]
        if not arr:return None
        best=None
        for x in reversed(arr):
            if x.ts<=target:
                best=x;break
        return best or arr[0]

    def _frame(self,symbol,ts):
        b=self.books[symbol][-1] if self.books[symbol] else None
        if not b:return None
        a250=self._trade_agg(symbol,ts,250)
        a1=self._trade_agg(symbol,ts,1000)
        a3=self._trade_agg(symbol,ts,3000)
        p250=self._book_at(symbol,ts-250); p1=self._book_at(symbol,ts-1000)
        def ret(old):
            return (b.mid/old.mid-1)*10000 if old and old.mid>0 else 0.0
        def pct(cur,old):
            return (cur/old-1) if old and old>0 else 0.0
        f={
          "ts":ts,"mid":b.mid,"bid":b.bid,"ask":b.ask,"spread":b.spread_bps,
          "imb":b.imbalance,"bid1":b.bid1,"ask1":b.ask1,"bid5":b.bid5,"ask5":b.ask5,
          "ret250":ret(p250),"ret1":ret(p1),
          "bid5_d250":pct(b.bid5,p250.bid5 if p250 else 0),
          "ask5_d250":pct(b.ask5,p250.ask5 if p250 else 0),
          "bid5_d1":pct(b.bid5,p1.bid5 if p1 else 0),
          "ask5_d1":pct(b.ask5,p1.ask5 if p1 else 0),
          "imb_d250":b.imbalance-(p250.imbalance if p250 else b.imbalance),
          "spread_d250":b.spread_bps-(p250.spread_bps if p250 else b.spread_bps),
        }
        for tag,a in (("250",a250),("1s",a1),("3s",a3)):
            for k,v in a.items():
                if k!="run_side":f[f"{tag}_{k}"]=v
            f[f"{tag}_run_side"]=a["run_side"]
        return f

    def _baseline(self,symbol,ts):
        arr=[x for x in self.frames[symbol] if x["ts"]<ts-1000 and x["ts"]>=ts-BASELINE_MS]
        return arr

    def _scores(self,symbol,f,base):
        def zs(k):
            return rz(f.get(k),[x.get(k) for x in base])
        absret=[abs(x.get("ret1",0.0)) for x in base]
        down_z=rz(abs(min(0.0,f["ret1"])),absret)
        activity_z=rz(math.log1p(f["1s_total_usd"]),[math.log1p(x.get("1s_total_usd",0)) for x in base])
        sell_z=rz(math.log1p(f["1s_sell_usd"]),[math.log1p(x.get("1s_sell_usd",0)) for x in base])
        flow_neg_z=rz(-f["1s_flow"],[-x.get("1s_flow",0) for x in base])
        shock=.32*max(0,sell_z)+.25*max(0,activity_z)+.28*max(0,down_z)+.15*max(0,flow_neg_z)

        bid_hold=rz(f["bid5_d250"],[x.get("bid5_d250",0) for x in base])
        imb_rec=rz(f["imb_d250"],[x.get("imb_d250",0) for x in base])
        flow_rec=rz(f["250_flow"]-f["1s_flow"],[(x.get("250_flow",0)-x.get("1s_flow",0)) for x in base])
        impact_ease=rz(f["ret250"]-f["ret1"]/4.0,[(x.get("ret250",0)-x.get("ret1",0)/4.0) for x in base])
        absorb=.28*max(0,bid_hold)+.24*max(0,imb_rec)+.28*max(0,flow_rec)+.20*max(0,impact_ease)

        ret_turn=rz(f["ret250"],[x.get("ret250",0) for x in base])
        flow_turn=rz(f["250_flow"],[x.get("250_flow",0) for x in base])
        ask_take=rz(-f["ask5_d250"],[-x.get("ask5_d250",0) for x in base])
        spread_ok=-max(0,rz(f["spread"],[x.get("spread",0) for x in base]))
        turn=.34*max(0,ret_turn)+.32*max(0,flow_turn)+.20*max(0,imb_rec)+.14*max(0,ask_take)+.10*spread_ok

        return {
          "shock":shock,"absorb":absorb,"turn":turn,
          "activity_z":activity_z,"sell_z":sell_z,"down_z":down_z,
          "flow_neg_z":flow_neg_z,"bid_hold_z":bid_hold,"imb_rec_z":imb_rec,
          "flow_rec_z":flow_rec,"impact_ease_z":impact_ease,
          "ret_turn_z":ret_turn,"flow_turn_z":flow_turn,"ask_take_z":ask_take,
          "spread_z":rz(f["spread"],[x.get("spread",0) for x in base]),
        }

    def _emit(self,symbol,stage,reason,payload):
        if not enabled("BBB_MARKET_PERSIST_ENABLED"):return
        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (now_ms(),0,self.session_id,"VOL",symbol,stage,reason,json.dumps(payload,separators=(",",":"))))

    def _finalize_decision(self,symbol,decision_stage,f,scores,reason,council=None):
        st=self.active.pop(symbol,None)
        if not st:return
        dyn=.30*st.shock_score+.30*st.absorb_score+.40*st.turn_score
        pre=[x for x in self.frames[symbol] if x["ts"]>=f["ts"]-PRE_FRAME_MS]
        ep=PendingEpisode(
          symbol=symbol,shock_ms=st.shock_ms,decision_ms=f["ts"],
          decision_stage=decision_stage,entry_ask=float(f["ask"]),
          shock_score=st.shock_score,absorb_score=st.absorb_score,
          turn_score=st.turn_score,dynamic_score=dyn,
          frames=list(pre),deadline_ms=f["ts"]+POST_LABEL_MS,
          council_snapshot=council
        )
        self.pending.append(ep)
        self.counts["decisions"]+=1
        self.counts["signals"]+=1 if decision_stage=="SIGNAL" else 0
        self.counts["rejects"]+=1 if decision_stage!="SIGNAL" else 0
        self._emit(symbol,"MICROFLOW_"+decision_stage,reason,{
            "shock_score":st.shock_score,"absorb_score":st.absorb_score,
            "turn_score":st.turn_score,"dynamic_score":dyn,
            "frame":f,"scores":scores
        })

    def _compact_episode_payload(self,frames,council_snapshot=None):
        if not frames:
            return {"frames":[],"council":council_snapshot or {}}
        keep=sorted(set([0,len(frames)//5,2*len(frames)//5,3*len(frames)//5,4*len(frames)//5,len(frames)-1]))
        fields=("ts","mid","bid","ask","spread","imb","ret250","ret1",
                "250_flow","1s_flow","250_total_usd","1s_total_usd",
                "250_sell_share","1s_sell_share","bid5_d250","ask5_d250","imb_d250")
        return {
            "frames":[{k:frames[i].get(k) for k in fields if k in frames[i]} for i in keep],
            "council":council_snapshot or {}
        }

    def _update_pending(self,symbol,f):
        remain=[]
        for ep in self.pending:
            if ep.symbol!=symbol:
                remain.append(ep);continue
            bid=float(f["bid"])
            bps=(bid/ep.entry_ask-1)*10000 if ep.entry_ask>0 else 0
            ep.exit_probe_bid=bid
            ep.max_fav_bps=max(ep.max_fav_bps,bps)
            ep.max_adv_bps=min(ep.max_adv_bps,bps)
            if ep.outcome_label is None:
                if bps<=SL_BPS:
                    ep.outcome_label="LOSS";ep.first_touch_ms=f["ts"]
                elif bps>=TP_BPS:
                    ep.outcome_label="WIN";ep.first_touch_ms=f["ts"]
            ep.frames.append(f)
            if f["ts"]<ep.deadline_ms:
                remain.append(ep);continue
            if ep.outcome_label is None:ep.outcome_label="NEUTRAL"
            if enabled("BBB_RESEARCH_PERSIST_ENABLED"):
                # Keep every actual SIGNAL, every rejected episode that later became WIN
                # (valuable missed-winner evidence), and a 10% deterministic control sample
                # of the remaining rejects. This preserves learning value across the day.
                keep_episode=(
                    ep.decision_stage=="SIGNAL" or
                    ep.outcome_label=="WIN" or
                    ((int(ep.decision_ms)//1000) % 10 == 0)
                )
                if keep_episode:
                    compact_payload=self._compact_episode_payload(ep.frames,ep.council_snapshot)
                    self.cx.execute("""INSERT INTO microflow_episodes(
                      session_id,symbol,shock_ms,decision_ms,completed_ms,decision_stage,outcome_label,
                      first_touch_ms,entry_ask,exit_probe_bid,max_favorable_bps,max_adverse_bps,
                      shock_score,absorb_score,turn_score,dynamic_score,payload_json)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (self.session_id,ep.symbol,ep.shock_ms,ep.decision_ms,f["ts"],ep.decision_stage,
                       ep.outcome_label,ep.first_touch_ms,ep.entry_ask,ep.exit_probe_bid,ep.max_fav_bps,
                       ep.max_adv_bps,ep.shock_score,ep.absorb_score,ep.turn_score,ep.dynamic_score,
                       json.dumps(compact_payload,separators=(",",":"))))
            self.counts["labeled"]+=1
            self.counts["wins"]+=1 if ep.outcome_label=="WIN" and ep.decision_stage=="SIGNAL" else 0
            self.counts["losses"]+=1 if ep.outcome_label=="LOSS" and ep.decision_stage=="SIGNAL" else 0
            self._emit(ep.symbol,"MICROFLOW_LABEL",ep.outcome_label,{
                "decision_stage":ep.decision_stage,"dynamic_score":ep.dynamic_score,
                "mfe_bps":ep.max_fav_bps,"mae_bps":ep.max_adv_bps
            })
        self.pending=remain

    def _publish_meta(self):
        for k,v in {
          "microflow_shadow_enabled":"1",
          "microflow_state_machine":"SELL_SHOCK>ABSORB>TURN>CONFIRM",
          "microflow_shocks":self.counts["shocks"],
          "microflow_absorptions":self.counts["absorptions"],
          "microflow_turns":self.counts["turns"],
          "microflow_signals":self.counts["signals"],
          "microflow_rejects":self.counts["rejects"],
          "microflow_labeled":self.counts["labeled"],
          "microflow_signal_wins":self.counts["wins"],
          "microflow_signal_losses":self.counts["losses"],
          "research_persist_enabled":"1" if enabled("BBB_RESEARCH_PERSIST_ENABLED") else "0",
          "council_support":self.counts["council_support"],
          "council_veto":self.counts["council_veto"],
          "council_reject":self.counts["council_reject"],
          "council_wait":self.counts["council_wait"],
        }.items():
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,str(v)))

    def maybe_step(self,symbol,ts_ms,book):
        ts=int(ts_ms)
        if ts-self.last_step[symbol]<STEP_MS:return
        self.last_step[symbol]=ts
        self._trim(symbol,ts)
        f=self._frame(symbol,ts)
        if not f:return
        base=self._baseline(symbol,ts)
        self.frames[symbol].append(f)
        self._update_pending(symbol,f)
        if len(base)<40:
            self._publish_meta();return

        scores=self._scores(symbol,f,base)
        st=self.active.get(symbol)
        council_stage=st.stage if st is not None else "IDLE"
        council=self.council.evaluate(symbol,ts,f,scores,council_stage)
        if council["decision"]=="SUPPORT": self.counts["council_support"]+=1
        elif council["decision"]=="VETO": self.counts["council_veto"]+=1
        elif council["decision"]=="REJECT": self.counts["council_reject"]+=1
        else: self.counts["council_wait"]+=1

        if st is None:
            # Adaptive sell-shock: unusual sell aggression + downside response.
            shock_ok=(
              f["1s_sell_share"]>=0.58 and
              scores["sell_z"]>=1.25 and
              scores["activity_z"]>=0.75 and
              scores["down_z"]>=1.0 and
              f["ret1"]<0
            )
            if shock_ok:
                pre=[x for x in self.frames[symbol] if x["ts"]>=ts-PRE_FRAME_MS]
                self.active[symbol]=ActiveState("SHOCK",ts,f["mid"],f["mid"],scores["shock"],
                                                best_score=scores["shock"],pre_frames=list(pre))
                self.counts["shocks"]+=1
                self._emit(symbol,"MICROFLOW_SHOCK","SELL_SHOCK",{"frame":f,"scores":scores})
            self._publish_meta();return

        age=ts-st.shock_ms
        prior_low=st.shock_low
        noise=max(0.5,median([abs(x.get("ret250",0)) for x in base],0.5)*2.0)
        new_low_bps=(f["mid"]/prior_low-1)*10000 if prior_low>0 else 0
        st.shock_low=min(st.shock_low,f["mid"])

        # Hard vetoes: renewed sell impulse or book/spread deterioration.
        hard_veto=(
          (scores["spread_z"]>=3.0) or
          (scores["sell_z"]>=2.5 and f["ret250"]<0 and scores["down_z"]>=1.5) or
          (new_low_bps < -noise)
        )
        if hard_veto:
            self._finalize_decision(symbol,"REJECT",f,scores,"RENEWED_DOWNSIDE_OR_SPREAD",council)
            self._publish_meta();return

        if age>STATE_TTL_MS:
            self._finalize_decision(symbol,"REJECT",f,scores,"STATE_TIMEOUT",council)
            self._publish_meta();return

        if st.stage=="SHOCK":
            absorb_ok=(
              scores["absorb"]>=0.75 and
              scores["bid_hold_z"]>=-0.25 and
              scores["flow_rec_z"]>=0.25 and
              scores["impact_ease_z"]>=0.0
            )
            if absorb_ok:
                st.stage="ABSORB";st.absorb_score=scores["absorb"]
                self.counts["absorptions"]+=1
                self._emit(symbol,"MICROFLOW_ABSORB","SELL_PRESSURE_ABSORBED",{"frame":f,"scores":scores})
        elif st.stage=="ABSORB":
            st.absorb_score=max(st.absorb_score,scores["absorb"])
            turn_ok=(
              scores["turn"]>=0.90 and
              scores["ret_turn_z"]>=0.50 and
              scores["flow_turn_z"]>=0.50 and
              f["ret250"]>0 and f["250_flow"]>0 and
              scores["spread_z"]<2.0
            )
            if turn_ok:
                st.stage="TURN";st.turn_score=scores["turn"];st.confirm_frames=1
                self.counts["turns"]+=1
                self._emit(symbol,"MICROFLOW_TURN","BUY_FLOW_TURN",{"frame":f,"scores":scores})
        elif st.stage=="TURN":
            st.turn_score=max(st.turn_score,scores["turn"])
            alive=(f["ret250"]>=0 and f["250_flow"]>=0 and scores["spread_z"]<2.0)
            st.confirm_frames=st.confirm_frames+1 if alive else 0
            if st.confirm_frames>=2:
                if self.signal_handler is not None:
                    try:
                        self.signal_handler(symbol,f,book,getattr(book,"exchange_ts_ms",0),council)
                    except Exception as e:
                        print("COMMITTEE_SIGNAL_HANDLER_FAILED",repr(e),flush=True)
                self._finalize_decision(symbol,"SIGNAL",f,scores,"TWO_FRAME_REVERSAL_CONFIRM",council)

        self._publish_meta()

