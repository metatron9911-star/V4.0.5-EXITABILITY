"""ROCKET live-market PAPER emulator.

Consumes the same live Bybit publicTrade + L50 orderbook snapshots as production
would observe. Entries/exits are emulated through current L50 VWAP. No real orders.
"""
from __future__ import annotations
from collections import defaultdict, deque
import json, os, statistics, uuid
from typing import Dict

from engine import now_ms, BYBIT_FEES, ProductClass
from rocket_profile import ROCKET, live_eligible, directional_trigger

# Rejection events carry failed_checks so the dashboard can expose the exact blocked gate.


class RocketPaper:
    def __init__(self,cx,session_id:str,start_usd:float):
        self.cx=cx; self.session_id=session_id
        self.start_usd=float(start_usd)
        self.cash=self.start_usd; self.realized=0.0; self.fees=0.0
        self.hist:Dict[str,deque]=defaultdict(lambda:deque(maxlen=90))
        self.arms:Dict[str,dict]={}
        self.positions:Dict[str,dict]={}
        self.cooldown_until:Dict[str,int]={}
        self._meta()

    def _meta(self):
        for k,v in {
            "rocket_paper_enabled":"1",
            "rocket_cash_usd":f"{self.cash:.8f}",
            "rocket_realized_usd":f"{self.realized:.8f}",
            "rocket_fees_usd":f"{self.fees:.8f}",
            "rocket_open_positions":str(len(self.positions)),
            "rocket_armed":str(len(self.arms)),
            "rocket_execution_mode":"LIVE_WS_L50_VWAP",
        }.items():
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,v))

    def _event(self,symbol,stage,reason,payload,exchange_ts_ms=0):
        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (now_ms(),int(exchange_ts_ms or 0),self.session_id,"VOL",symbol,stage,reason,
                         json.dumps(payload,separators=(",",":"))))

    @staticmethod
    def _move(side,entry,px):
        sign=1.0 if side=="BUY" else -1.0
        return sign*(float(px)/float(entry)-1.0)*10000.0

    def _open(self,symbol,side,f,book,exchange_ts_ms,extra):
        if symbol in self.positions:
            self._event(symbol,"ROCKET_REJECT","ENTRY_ALREADY_OPEN",{
                "side":side,"open_positions":len(self.positions)
            },exchange_ts_ms)
            return False
        if len(self.positions)>=ROCKET.max_open_positions:
            self._event(symbol,"ROCKET_REJECT","ENTRY_MAX_POSITIONS",{
                "side":side,"open_positions":len(self.positions),
                "max_open_positions":ROCKET.max_open_positions
            },exchange_ts_ms)
            return False
        base_notional=self.start_usd*ROCKET.position_fraction
        notional=min(base_notional*ROCKET.position_multiplier,self.cash)
        if notional<1:
            self._event(symbol,"ROCKET_REJECT","ENTRY_NOTIONAL_TOO_SMALL",{
                "side":side,"cash":self.cash,"notional":notional,
                "base_notional":base_notional,
                "position_fraction":ROCKET.position_fraction,
                "position_multiplier":ROCKET.position_multiplier
            },exchange_ts_ms)
            return False
        vwap,filled=book.walk_vwap(side,notional)
        if not vwap or filled<notional*0.999:
            self._event(symbol,"ROCKET_REJECT","ENTRY_L50_INSUFFICIENT",{
                "side":side,"requested":notional,"filled":filled,
                "book_synced":bool(book.synced),"mid":book.mid
            },exchange_ts_ms)
            return False
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        fee=notional*fee_bps/10000.0
        if self.cash<notional+fee:
            self._event(symbol,"ROCKET_REJECT","ENTRY_CASH_FEE_SHORTFALL",{
                "side":side,"cash":self.cash,"notional":notional,"fee":fee
            },exchange_ts_ms)
            return False
        pid=str(uuid.uuid4()); ts=now_ms()
        self.cash-=notional+fee; self.fees+=fee
        p={"position_id":pid,"side":side,"entry_vwap":float(vwap),"notional":notional,
           "entry_fee":fee,"opened_ms":ts,"peak_bps":0.0,"trough_bps":0.0,"breakeven":False,
           **extra}
        self.positions[symbol]=p
        features={**dict(f),"rocket":True,**extra}
        self.cx.execute("""INSERT INTO trades(
            session_id,position_id,head,venue,product_class,symbol,side,leverage,margin_usd,notional_usd,
            signal_exchange_ts_ms,receive_ts_ms,signal_ts_ms,order_ts_ms,fill_ts_ms,
            signal_mid,signal_bid,signal_ask,expected_entry_vwap,entry_vwap,entry_fee_usd,
            source_age_ms,processing_ms,signal_to_fill_ms,entry_score,entry_gate,risk_state,
            entry_features_json,status,opened_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id,pid,"VOL","BYBIT","INNOVATION",symbol,side,1.0,notional,notional,
             int(exchange_ts_ms or 0),ts,ts,ts,ts,float(f.get("mid") or vwap),
             float(f.get("bid") or vwap),float(f.get("ask") or vwap),float(vwap),float(vwap),fee,
             0,0,0,0,0,"ROCKET_V1",json.dumps(features,separators=(",",":")),"OPEN",ts))
        self._event(symbol,"ROCKET_FILL","ENTRY",{"side":side,"vwap":vwap,"notional":notional,"fee_bps":fee_bps,**extra},exchange_ts_ms)
        self._meta()
        return True

    def _close(self,symbol,reason,book,exchange_ts_ms):
        p=self.positions.get(symbol)
        if not p:return
        exit_side="SELL" if p["side"]=="BUY" else "BUY"
        vwap,filled=book.walk_vwap(exit_side,float(p["notional"]))
        if not vwap or filled<float(p["notional"])*0.999:
            self._event(symbol,"ROCKET_REJECT","EXIT_L50_INSUFFICIENT",{"reason":reason,"filled":filled},exchange_ts_ms)
            return
        p=self.positions.pop(symbol)
        gross_ret=(1 if p["side"]=="BUY" else -1)*(float(vwap)/float(p["entry_vwap"])-1)
        gross=float(p["notional"])*gross_ret
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        exit_fee=float(p["notional"])*fee_bps/10000.0
        net=gross-float(p["entry_fee"])-exit_fee
        self.cash+=float(p["notional"])+gross-exit_fee
        self.realized+=net; self.fees+=exit_fee
        hold=max(0,now_ms()-int(p["opened_ms"]))
        self.cx.execute("""UPDATE trades SET status='CLOSED',exit_vwap=?,exit_fee_usd=?,gross_pnl_usd=?,
                          net_pnl_usd=?,exit_reason=?,hold_ms=?,closed_ms=?
                          WHERE session_id=? AND position_id=?""",
                        (float(vwap),exit_fee,gross,net,reason,hold,now_ms(),self.session_id,p["position_id"]))
        self._event(symbol,"ROCKET_FILL","EXIT",{"reason":reason,"side":exit_side,"vwap":vwap,
                    "gross_pnl_usd":gross,"net_pnl_usd":net,"hold_ms":hold},exchange_ts_ms)
        if net<0:self.cooldown_until[symbol]=now_ms()+ROCKET.cooldown_after_loss_ms
        self._meta()

    def _manage(self,symbol,f,book,exchange_ts_ms):
        p=self.positions.get(symbol)
        if not p or not book.mid:return
        move=self._move(p["side"],p["entry_vwap"],float(book.mid))
        p["peak_bps"]=max(float(p["peak_bps"]),move)
        p["trough_bps"]=min(float(p["trough_bps"]),move)
        age=now_ms()-int(p["opened_ms"])
        if move>=ROCKET.breakeven_arm_bps:p["breakeven"]=True
        stop=-ROCKET.stop_bps
        if p["breakeven"]: stop=max(stop,ROCKET.breakeven_floor_bps)
        if p["peak_bps"]>=ROCKET.trail_arm_bps:
            stop=max(stop,float(p["peak_bps"])-ROCKET.trail_giveback_bps)
        if move<=stop:
            self._close(symbol,"ROCKET_STOP_OR_TRAIL",book,exchange_ts_ms)
        elif move>=ROCKET.hard_take_profit_bps:
            self._close(symbol,"ROCKET_HARD_TP",book,exchange_ts_ms)
        elif age>=ROCKET.timeout_ms:
            self._close(symbol,"ROCKET_TIMEOUT",book,exchange_ts_ms)

    def _signal(self,symbol,f,book,exchange_ts_ms):
        now=now_ms()
        if symbol in self.positions:
            if symbol in self.arms:
                self._event(symbol,"ROCKET_REJECT","ARM_CANCEL_ALREADY_OPEN",{
                    "open_positions":len(self.positions)
                },exchange_ts_ms)
                self.arms.pop(symbol,None); self._meta()
            return
        cooldown_until=self.cooldown_until.get(symbol,0)
        if now<cooldown_until:
            if symbol in self.arms:
                self._event(symbol,"ROCKET_REJECT","ARM_CANCEL_COOLDOWN",{
                    "remaining_ms":max(0,int(cooldown_until-now))
                },exchange_ts_ms)
                self.arms.pop(symbol,None); self._meta()
            return
        arm=self.arms.get(symbol)
        if arm is not None:
            side=arm["side"]; level=float(arm["level"]); cur=float(book.mid or 0)
            sign=1.0 if side=="BUY" else -1.0
            beyond=sign*(cur/level-1)*10000 if cur>0 and level>0 else -999
            prior=list(self.hist[symbol])[:-1]
            base=statistics.median([max(1,int(x["tc1"])) for x in prior[-20:]]) if prior else 1
            tape=float(f.get("trade_count_1s",0) or 0)/max(1.0,base)

            spread=float(f.get("spread_bps",999) or 999)
            t1=int(f.get("trade_count_1s",0) or 0)
            t5=int(f.get("trade_count_5s",0) or 0)
            rv=abs(float(f.get("realized_vol_5s_bps",0) or 0))
            r1=abs(float(f.get("ret_1s_bps",0) or 0))
            r5=abs(float(f.get("ret_5s_bps",0) or 0))
            d250=sign*float(f.get("ret_250ms_bps",0) or 0)
            d1=sign*float(f.get("ret_1s_bps",0) or 0)
            flow=sign*((float(f.get("buy_ratio_1s",0.5) or 0.5)-0.5)*2.0)

            # After ARM we allow normal micro-pauses during the confirmation window.
            # Only a real thesis break kills the arm immediately.
            hard_failed=[]
            if beyond < -ROCKET.max_retest_bps:
                hard_failed.append("RETEST")
            if spread > ROCKET.max_spread_bps * 2.0:
                hard_failed.append("SPREAD_BLOWOUT")
            opposite_impulse=(
                d250 <= -ROCKET.min_directional_ret_250ms_bps
                and d1 <= -ROCKET.min_directional_ret_1s_bps
                and flow <= -ROCKET.min_directional_flow
            )
            if opposite_impulse:
                hard_failed.append("OPPOSITE_IMPULSE")

            if hard_failed:
                self._event(symbol,"ROCKET_REJECT","ARM_BROKEN",{
                    "side":side,"beyond_bps":beyond,"tape_ratio":tape,
                    "failed_checks":hard_failed,
                    "spread_bps":spread,"trades_1s":t1,"trades_5s":t5,
                    "rv5s_bps":rv,"abs_ret1_bps":r1,"abs_ret5_bps":r5,
                    "dir250_bps":d250,"dir1_bps":d1,"directional_flow":flow,
                    "samples":int(arm.get("samples",0))
                },exchange_ts_ms)
                self.arms.pop(symbol,None); self._meta(); return

            # Confirmation quorum: price must remain beyond the level, then
            # any 3 of 4 microstructure votes are enough. A normal micro-pause
            # in one feature no longer kills an otherwise valid continuation.
            tape_vote=(
                tape >= ROCKET.confirm_tape_ratio_min
                and tape >= float(arm["arm_tape_ratio"])*ROCKET.confirm_tape_vs_arm_min
            )
            votes={
                "DIR_250MS": d250>=ROCKET.min_directional_ret_250ms_bps,
                "DIR_1S": d1>=ROCKET.min_directional_ret_1s_bps,
                "FLOW": flow>=ROCKET.min_directional_flow,
                "TAPE": tape_vote,
            }
            vote_count=sum(1 for ok in votes.values() if ok)
            beyond_ok=beyond>=ROCKET.min_beyond_level_bps
            good_snapshot=beyond_ok and vote_count>=ROCKET.confirm_quorum_votes

            arm["total_snapshots"]=int(arm.get("total_snapshots",0))+1
            arm["best_beyond"]=max(float(arm.get("best_beyond",beyond)),beyond)
            arm["best_tape"]=max(float(arm.get("best_tape",0.0)),tape)
            arm["best_vote_count"]=max(int(arm.get("best_vote_count",0)),vote_count)
            misses=arm.setdefault("vote_misses",{"DIR_250MS":0,"DIR_1S":0,"FLOW":0,"TAPE":0,"BEYOND_LEVEL":0})
            for name,ok in votes.items():
                if not ok: misses[name]=int(misses.get(name,0))+1
            if not beyond_ok:
                misses["BEYOND_LEVEL"]=int(misses.get("BEYOND_LEVEL",0))+1
            if good_snapshot:
                arm["samples"]=int(arm.get("samples",0))+1
                arm["consecutive_samples"]=int(arm.get("consecutive_samples",0))+1
                arm["best_consecutive_samples"]=max(
                    int(arm.get("best_consecutive_samples",0)),
                    int(arm["consecutive_samples"])
                )
            else:
                arm["consecutive_samples"]=0

            elapsed=now-int(arm["armed_ms"])
            if elapsed>=ROCKET.confirm_ms:
                samples=int(arm.get("samples",0))
                total=int(arm.get("total_snapshots",0))
                final_alive=(
                    beyond >= ROCKET.min_beyond_level_bps
                    and beyond <= ROCKET.max_entry_chase_bps
                    and vote_count >= ROCKET.final_entry_min_votes
                )
                consecutive=int(arm.get("consecutive_samples",0))
                best_consecutive=int(arm.get("best_consecutive_samples",0))
                ok=(
                    samples>=ROCKET.confirm_samples
                    and consecutive>=ROCKET.confirm_consecutive_samples
                    and final_alive
                )
                if ok:
                    self._event(symbol,"ROCKET_CONFIRM","CONTINUATION",{
                                "side":side,"level":level,
                                "beyond_bps":beyond,"tape_ratio":tape,
                                "samples":samples,"total_snapshots":total,
                                "consecutive_samples":consecutive,
                                "best_consecutive_samples":best_consecutive,
                                "required_consecutive_samples":ROCKET.confirm_consecutive_samples,
                                "best_beyond_bps":arm.get("best_beyond",0),
                                "best_tape_ratio":arm.get("best_tape",0),
                                "best_vote_count":arm.get("best_vote_count",0),
                                "required_quorum_votes":ROCKET.confirm_quorum_votes,
                                "final_vote_count":vote_count,
                                "final_alive":final_alive,
                                "max_entry_chase_bps":ROCKET.max_entry_chase_bps,
                                "vote_misses":arm.get("vote_misses",{})
                                },exchange_ts_ms)
                    opened=self._open(symbol,side,f,book,exchange_ts_ms,{
                               "level":level,
                               "arm_tape_ratio":arm["arm_tape_ratio"],
                               "confirm_tape_ratio":arm.get("best_tape",tape),
                               "confirm_samples":samples,
                               "confirm_consecutive_samples":consecutive,
                               "confirm_total_snapshots":total,
                               "best_vote_count":arm.get("best_vote_count",0),
                               "required_quorum_votes":ROCKET.confirm_quorum_votes,
                               "best_beyond_bps":arm.get("best_beyond",0)})
                    if not opened:
                        self._event(symbol,"ROCKET_REJECT","CONFIRMED_BUT_ENTRY_BLOCKED",{
                            "side":side,"samples":samples,"total_snapshots":total,
                            "open_positions":len(self.positions),"cash":self.cash
                        },exchange_ts_ms)
                else:
                    failed=[]
                    if samples<ROCKET.confirm_samples: failed.append("CONFIRM_SAMPLES")
                    if best_consecutive<ROCKET.confirm_consecutive_samples: failed.append("CONFIRM_NOT_CONSECUTIVE")
                    if int(arm.get("best_vote_count",0))<ROCKET.confirm_quorum_votes: failed.append("QUORUM_NOT_REACHED")
                    if float(arm.get("best_beyond",0.0))<ROCKET.min_beyond_level_bps: failed.append("BEYOND_LEVEL")
                    if samples>=ROCKET.confirm_samples and vote_count<ROCKET.final_entry_min_votes: failed.append("FINAL_MOMENTUM_FADED")
                    if beyond>ROCKET.max_entry_chase_bps: failed.append("ENTRY_TOO_EXTENDED")
                    misses=arm.get("vote_misses",{})
                    if misses.get("TAPE",0)>=max(1,total): failed.append("TAPE_NEVER_PASSED")
                    if misses.get("DIR_250MS",0)>=max(1,total): failed.append("DIR_250MS_NEVER_PASSED")
                    if misses.get("DIR_1S",0)>=max(1,total): failed.append("DIR_1S_NEVER_PASSED")
                    if misses.get("FLOW",0)>=max(1,total): failed.append("FLOW_NEVER_PASSED")
                    self._event(symbol,"ROCKET_REJECT","NO_CONTINUATION",{
                                "side":side,"beyond_bps":beyond,
                                "tape_ratio":tape,"samples":samples,
                                "consecutive_samples":consecutive,
                                "best_consecutive_samples":best_consecutive,
                                "required_consecutive_samples":ROCKET.confirm_consecutive_samples,
                                "total_snapshots":total,
                                "best_tape_ratio":arm.get("best_tape",0),
                                "best_beyond_bps":arm.get("best_beyond",0),
                                "best_vote_count":arm.get("best_vote_count",0),
                                "required_quorum_votes":ROCKET.confirm_quorum_votes,
                                "final_vote_count":vote_count,
                                "final_alive":final_alive,
                                "max_entry_chase_bps":ROCKET.max_entry_chase_bps,
                                "vote_misses":arm.get("vote_misses",{}),
                                "arm_tape_ratio":arm["arm_tape_ratio"],
                                "failed_checks":failed,
                                "required_samples":ROCKET.confirm_samples,
                                "required_tape_ratio":ROCKET.confirm_tape_ratio_min,
                                "required_tape_vs_arm":float(arm["arm_tape_ratio"])*ROCKET.confirm_tape_vs_arm_min,
                                "required_beyond_bps":ROCKET.min_beyond_level_bps
                                },exchange_ts_ms)
                self.arms.pop(symbol,None); self._meta()
            return

        if not live_eligible(f): return
        h=list(self.hist[symbol])
        if len(h)<12:return
        prior=h[:-1]
        mids=[x["mid"] for x in prior if x["mid"]>0]
        if len(mids)<10:return
        hi=max(mids); lo=min(mids); tol=ROCKET.touch_tolerance_bps/10000.0
        hi_t=sum(1 for x in mids if abs(x/hi-1)<=tol); lo_t=sum(1 for x in mids if abs(x/lo-1)<=tol)
        base=statistics.median([max(1,int(x["tc1"])) for x in prior[-20:]])
        tape=float(f.get("trade_count_1s",0) or 0)/max(1.0,base)
        cur=float(book.mid or 0)
        side=None;level=0;touches=0
        if hi_t>=ROCKET.min_level_touches and cur>hi and tape>=ROCKET.arm_tape_ratio_min and directional_trigger("BUY",f):
            side="BUY";level=hi;touches=hi_t
        elif lo_t>=ROCKET.min_level_touches and cur<lo and tape>=ROCKET.arm_tape_ratio_min and directional_trigger("SELL",f):
            side="SELL";level=lo;touches=lo_t
        if side:
            self.arms[symbol]={"side":side,"level":level,"touches":touches,"armed_ms":now,
                               "arm_tape_ratio":tape,"samples":0,"consecutive_samples":0,
                               "best_consecutive_samples":0,"total_snapshots":0,
                               "best_beyond":0.0,"best_tape":0.0,"best_vote_count":0,
                               "vote_misses":{"DIR_250MS":0,"DIR_1S":0,"FLOW":0,"TAPE":0,"BEYOND_LEVEL":0}}
            self._event(symbol,"ROCKET_ARM","BREAKOUT",self.arms[symbol],exchange_ts_ms)
            self._meta()

    def on_snapshot(self,symbol,f,book,exchange_ts_ms=0):
        if not book.synced or not book.mid:return
        self._manage(symbol,f,book,exchange_ts_ms)
        self.hist[symbol].append({"ts":now_ms(),"mid":float(book.mid),"tc1":int(f.get("trade_count_1s",0) or 0)})
        if symbol not in self.positions:self._signal(symbol,f,book,exchange_ts_ms)
