"""Live PAPER-shadow evaluators for the dynamic dual-module scalping profile.

No real orders. No PaperBook mutation. Runs even while BBB_PAPER_ENTRIES=false.
It records hypothetical HYPE breakout and LIQUID DOM-bounce entries/exits to events.
"""
from __future__ import annotations

from collections import defaultdict, deque
import json, statistics
from typing import Dict

from dynamic_scalp_profile import HYPE, LIQUID, RISK, classify_segment, directional_hype_trigger
from engine import now_ms
import os, uuid


class DynamicScalpShadow:
    def __init__(self, cx, session_id: str):
        self.cx=cx
        self.session_id=session_id
        self.paper_enabled=os.getenv("BBB_DYNAMIC_SCALP_PAPER","0").lower() in {"1","true","yes","on"}
        self.start_usd=float(os.getenv("BBB_DYNAMIC_SCALP_START_USD",os.getenv("BBB_PAPER_START_USD","500")))
        self.paper_cash=self.start_usd
        self.paper_realized=0.0
        self.paper_fees=0.0
        self.paper_positions={}
        self._write_meta()
        self.hist: Dict[str, deque] = defaultdict(lambda: deque(maxlen=180))
        self.wall_arms: Dict[tuple[str,str],dict] = {}
        self.hype_arms: Dict[str,dict] = {}
        self.positions: Dict[str,dict] = {}
        self.cooldown_until: Dict[str,int] = {}

    def _write_meta(self):
        meta={
            "dynamic_scalp_paper_enabled":"1" if self.paper_enabled else "0",
            "dynamic_scalp_cash_usd":f"{self.paper_cash:.8f}",
            "dynamic_scalp_realized_usd":f"{self.paper_realized:.8f}",
            "dynamic_scalp_fees_usd":f"{self.paper_fees:.8f}",
            "dynamic_scalp_open_positions":str(len(self.paper_positions)),
        }
        for k,v in meta.items():
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,v))

    def _paper_open(self,symbol,module,side,entry_px,f,exchange_ts_ms,extra):
        if not self.paper_enabled or symbol in self.paper_positions:
            return
        # Fixed 2% notional of starting bank for first validation pass, 1x only.
        margin=max(1.0,self.start_usd*0.02)
        notional=min(margin,self.paper_cash)
        if notional<=0:
            return
        fee=notional*0.001
        if self.paper_cash < notional+fee:
            return
        pid=str(uuid.uuid4())
        self.paper_cash-=notional+fee
        self.paper_fees+=fee
        self.paper_positions[symbol]={
            "position_id":pid,"module":module,"side":side,"entry_px":float(entry_px),
            "notional":notional,"margin":notional,"entry_fee":fee,"opened_ms":now_ms(),
        }
        features={**dict(f),"dynamic_module":module,**(extra or {})}
        self.cx.execute("""INSERT INTO trades(
            session_id,position_id,head,venue,product_class,symbol,side,leverage,margin_usd,notional_usd,
            signal_exchange_ts_ms,receive_ts_ms,signal_ts_ms,order_ts_ms,fill_ts_ms,
            signal_mid,signal_bid,signal_ask,expected_entry_vwap,entry_vwap,
            entry_fee_usd,source_age_ms,processing_ms,signal_to_fill_ms,
            entry_score,entry_gate,risk_state,entry_features_json,status,opened_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id,pid,"VOL","BYBIT","STANDARD",symbol,side,1.0,notional,notional,
             int(exchange_ts_ms or 0),now_ms(),now_ms(),now_ms(),now_ms(),
             float(f.get("mid") or entry_px),float(f.get("bid") or entry_px),float(f.get("ask") or entry_px),
             float(entry_px),float(entry_px),fee,0,0,0,0.0,0.0,f"DYNAMIC_{module}",
             json.dumps(features,separators=(",",":")),"OPEN",now_ms()))
        self._write_meta()

    def _paper_close(self,symbol,reason,exit_px):
        pp=self.paper_positions.pop(symbol,None)
        if not pp:
            return
        side=pp["side"]; entry=float(pp["entry_px"]); notional=float(pp["notional"])
        sign=1.0 if side=="BUY" else -1.0
        gross_ret=sign*(float(exit_px)/entry-1.0)
        gross_pnl=notional*gross_ret
        exit_fee=notional*0.001
        net=gross_pnl-float(pp["entry_fee"])-exit_fee
        self.paper_cash += notional + gross_pnl - exit_fee
        self.paper_fees += exit_fee
        self.paper_realized += net
        self.cx.execute("""UPDATE trades SET status='CLOSED',exit_vwap=?,exit_fee_usd=?,gross_pnl_usd=?,net_pnl_usd=?,
                          exit_reason=?,closed_ms=? WHERE session_id=? AND position_id=?""",
                        (float(exit_px),exit_fee,gross_pnl,net,reason,now_ms(),self.session_id,pp["position_id"]))
        self._write_meta()

    def _event(self, symbol, stage, reason, payload, exchange_ts_ms=0):
        self.cx.execute(
            """INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (now_ms(),int(exchange_ts_ms or 0),self.session_id,"VOL",symbol,stage,reason,
             json.dumps(payload,separators=(",",":")))
        )

    @staticmethod
    def _move_bps(side, entry, px):
        sign=1.0 if side=="BUY" else -1.0
        return sign*(float(px)/float(entry)-1.0)*10000.0

    def _open(self, symbol, module, side, entry_px, f, exchange_ts_ms, extra=None):
        if symbol in self.positions or now_ms()<self.cooldown_until.get(symbol,0):
            return
        if len(self.positions)>=RISK.max_total_open_positions:
            return
        p={
            "module":module,"side":side,"entry_px":float(entry_px),"opened_ms":now_ms(),
            "peak_bps":0.0,"trough_bps":0.0,"breakeven":False,
            "entry_spread_bps":float(f.get("spread_bps",0) or 0),
        }
        if extra: p.update(extra)
        self.positions[symbol]=p
        self._paper_open(symbol,module,side,entry_px,f,exchange_ts_ms,extra or {})
        self._event(symbol,"DYNAMIC_SCALP_SHADOW_ENTRY",module,{
            **p,
            "segment":f.get("dynamic_segment"),
            "trade_count_1s":int(f.get("trade_count_1s",0) or 0),
            "trade_count_5s":int(f.get("trade_count_5s",0) or 0),
            "ret_250ms_bps":float(f.get("ret_250ms_bps",0) or 0),
            "ret_1s_bps":float(f.get("ret_1s_bps",0) or 0),
            "buy_ratio_1s":float(f.get("buy_ratio_1s",.5) or .5),
        },exchange_ts_ms)

    def _close(self, symbol, reason, exit_px, f, exchange_ts_ms):
        p=self.positions.pop(symbol,None)
        if not p:return
        gross=self._move_bps(p["side"],p["entry_px"],exit_px)
        # Conservative research cost: taker-like round trip 20 bps plus entry spread.
        # This intentionally penalizes the shadow rather than assuming perfect maker fills.
        cost=20.0+float(p.get("entry_spread_bps",0) or 0)
        net=gross-cost
        self._paper_close(symbol,reason,exit_px)
        self._event(symbol,"DYNAMIC_SCALP_SHADOW_EXIT",reason,{
            **p,"exit_px":float(exit_px),"gross_bps":gross,"cost_bps":cost,"net_bps":net,
            "age_ms":now_ms()-int(p["opened_ms"]),
        },exchange_ts_ms)
        if net<0:
            self.cooldown_until[symbol]=now_ms()+RISK.cooldown_after_loss_ms

    def _manage(self, symbol, f, book, exchange_ts_ms):
        p=self.positions.get(symbol)
        if not p or not book.mid:return
        mid=float(book.mid)
        move=self._move_bps(p["side"],p["entry_px"],mid)
        p["peak_bps"]=max(float(p.get("peak_bps",move)),move)
        p["trough_bps"]=min(float(p.get("trough_bps",move)),move)
        age=now_ms()-int(p["opened_ms"])

        if p["module"]=="HYPE":
            if move>=HYPE.breakeven_arm_bps:
                p["breakeven"]=True
            stop=-HYPE.stop_bps
            if p["breakeven"]:
                stop=max(stop,0.0)
            if p["peak_bps"]>=HYPE.trail_arm_bps:
                stop=max(stop,p["peak_bps"]-HYPE.trail_giveback_bps)
            if move<=stop:
                self._close(symbol,"HYPE_STOP_OR_TRAIL",mid,f,exchange_ts_ms);return
            if move>=HYPE.take_profit_bps:
                self._close(symbol,"HYPE_TP",mid,f,exchange_ts_ms);return
            if age>=HYPE.timeout_ms:
                self._close(symbol,"HYPE_TIMEOUT",mid,f,exchange_ts_ms);return
        else:
            if move>=LIQUID.breakeven_arm_bps:
                p["breakeven"]=True
            wall=float(p.get("wall_price",p["entry_px"]))
            if p["side"]=="BUY":
                wall_stop=wall*(1-LIQUID.stop_buffer_bps/10000)
                if mid<=wall_stop:
                    self._close(symbol,"LIQUID_WALL_BROKEN",mid,f,exchange_ts_ms);return
            else:
                wall_stop=wall*(1+LIQUID.stop_buffer_bps/10000)
                if mid>=wall_stop:
                    self._close(symbol,"LIQUID_WALL_BROKEN",mid,f,exchange_ts_ms);return
            if move>=LIQUID.take_profit_bps:
                self._close(symbol,"LIQUID_TP",mid,f,exchange_ts_ms);return
            if p["breakeven"] and move<=0:
                self._close(symbol,"LIQUID_BREAKEVEN",mid,f,exchange_ts_ms);return
            if age>=LIQUID.timeout_ms:
                self._close(symbol,"LIQUID_TIMEOUT",mid,f,exchange_ts_ms);return

    def _hype_signal(self, symbol, f, book, exchange_ts_ms):
        if symbol in self.positions:
            self.hype_arms.pop(symbol,None)
            return

        now=now_ms()
        seg=f.get("dynamic_segment")
        arm=self.hype_arms.get(symbol)

        # Existing armed breakout: require persistence/continuation before entry.
        if arm is not None:
            side=str(arm["side"])
            level=float(arm["level"])
            cur=float(book.mid or 0)
            sign=1.0 if side=="BUY" else -1.0
            beyond_bps=sign*(cur/level-1.0)*10000.0 if level>0 and cur>0 else -999.0

            # Recompute current tape ratio against recent local baseline.
            h=self.hist[symbol]
            prior=list(h)[:-1]
            tc_hist=[max(1,int(x["tc1"])) for x in prior[-20:]]
            slow=statistics.median(tc_hist) if tc_hist else 1.0
            speed_ratio=float(f.get("trade_count_1s",0) or 0)/max(1.0,slow)

            # Hard invalidate: breakout lost beyond allowed retest, regime disappeared,
            # or directional tape flipped before confirmation.
            if (
                seg!="HYPE_TREND"
                or beyond_bps < -HYPE.breakout_max_retest_bps
                or not directional_hype_trigger(side,f)
            ):
                self._event(symbol,"DYNAMIC_SCALP_HYPE_REJECT","BREAKOUT_FAILED",{
                    **arm,"age_ms":now-int(arm["armed_ms"]),
                    "beyond_bps":beyond_bps,"tape_speed_ratio_now":speed_ratio,
                    "segment":seg,
                },exchange_ts_ms)
                self.hype_arms.pop(symbol,None)
                return

            if beyond_bps >= HYPE.breakout_hold_buffer_bps:
                arm["confirm_samples"]=int(arm.get("confirm_samples",0))+1
                arm["best_beyond_bps"]=max(float(arm.get("best_beyond_bps",beyond_bps)),beyond_bps)
            arm["min_beyond_bps"]=min(float(arm.get("min_beyond_bps",beyond_bps)),beyond_bps)

            elapsed=now-int(arm["armed_ms"])
            continued=float(arm.get("best_beyond_bps",0.0))>=HYPE.breakout_min_continue_bps
            tape_alive=speed_ratio>=HYPE.breakout_min_tape_ratio_after_arm
            enough_samples=int(arm.get("confirm_samples",0))>=HYPE.breakout_min_confirm_samples

            if elapsed>=HYPE.breakout_confirm_ms:
                if enough_samples and continued and tape_alive:
                    px=float(book.best_ask if side=="BUY" else book.best_bid)
                    if px>0:
                        self._event(symbol,"DYNAMIC_SCALP_HYPE_CONFIRM","PERSISTENT_BREAKOUT",{
                            **arm,"age_ms":elapsed,"beyond_bps":beyond_bps,
                            "tape_speed_ratio_now":speed_ratio,
                        },exchange_ts_ms)
                        self._open(symbol,"HYPE",side,px,f,exchange_ts_ms,{
                            "level":level,
                            "touches":arm["touches"],
                            "tape_speed_ratio_arm":arm["tape_speed_ratio_arm"],
                            "tape_speed_ratio_confirm":speed_ratio,
                            "breakout_confirm_ms":elapsed,
                            "breakout_confirm_samples":arm.get("confirm_samples",0),
                            "breakout_best_beyond_bps":arm.get("best_beyond_bps",0.0),
                            "breakout_min_beyond_bps":arm.get("min_beyond_bps",0.0),
                        })
                else:
                    self._event(symbol,"DYNAMIC_SCALP_HYPE_REJECT","NO_PERSISTENCE",{
                        **arm,"age_ms":elapsed,"beyond_bps":beyond_bps,
                        "tape_speed_ratio_now":speed_ratio,
                        "enough_samples":enough_samples,"continued":continued,"tape_alive":tape_alive,
                    },exchange_ts_ms)
                self.hype_arms.pop(symbol,None)
            return

        if seg!="HYPE_TREND":
            return
        h=self.hist[symbol]
        if len(h)<12:return
        mids=[x["mid"] for x in h if x["mid"]>0]
        if len(mids)<12:return

        # Exclude current point when defining the local level.
        prior=list(h)[:-1]
        if len(prior)<10:return
        prior_mids=[x["mid"] for x in prior]
        hi=max(prior_mids); lo=min(prior_mids)
        tol=HYPE.touch_tolerance_bps/10000.0

        hi_touches=sum(1 for x in prior_mids if abs(x/hi-1)<=tol)
        lo_touches=sum(1 for x in prior_mids if abs(x/lo-1)<=tol)
        cur=float(book.mid or 0)
        if cur<=0:return

        tc_hist=[max(1,int(x["tc1"])) for x in prior[-20:]]
        slow=statistics.median(tc_hist) if tc_hist else 1.0
        speed_ratio=float(f.get("trade_count_1s",0) or 0)/max(1.0,slow)

        side=None;level=None;touches=0
        if hi_touches>=HYPE.min_level_touches and cur>hi and speed_ratio>=HYPE.tape_speed_ratio_min and directional_hype_trigger("BUY",f):
            side="BUY";level=hi;touches=hi_touches
        elif lo_touches>=HYPE.min_level_touches and cur<lo and speed_ratio>=HYPE.tape_speed_ratio_min and directional_hype_trigger("SELL",f):
            side="SELL";level=lo;touches=lo_touches

        if side:
            sign=1.0 if side=="BUY" else -1.0
            beyond_bps=sign*(cur/float(level)-1.0)*10000.0
            self.hype_arms[symbol]={
                "side":side,"level":float(level),"touches":int(touches),
                "armed_ms":now,"tape_speed_ratio_arm":speed_ratio,
                "confirm_samples":1 if beyond_bps>=HYPE.breakout_hold_buffer_bps else 0,
                "best_beyond_bps":beyond_bps,
                "min_beyond_bps":beyond_bps,
            }
            self._event(symbol,"DYNAMIC_SCALP_HYPE_ARM","BREAKOUT_PRINT_THROUGH",{
                **self.hype_arms[symbol],"beyond_bps":beyond_bps,
            },exchange_ts_ms)

    def _liquid_signal(self, symbol, f, book, exchange_ts_ms):
        if f.get("dynamic_segment")!="LIQUID_FLAT" or symbol in self.positions:
            # discard stale arms when regime changes
            self.wall_arms.pop((symbol,"BUY"),None);self.wall_arms.pop((symbol,"SELL"),None)
            return
        now=now_ms()
        for side,key in (("BUY","dom_bid"),("SELL","dom_ask")):
            ratio=float(f.get(key+"_wall_ratio",0) or 0)
            dist=float(f.get(key+"_wall_distance_bps",999) or 999)
            wall=float(f.get(key+"_wall_price",0) or 0)
            k=(symbol,side)
            qualifies=ratio>=LIQUID.wall_ratio_min and dist<=LIQUID.wall_max_distance_bps and wall>0
            if not qualifies:
                self.wall_arms.pop(k,None);continue
            arm=self.wall_arms.get(k)
            if arm is None or abs(float(arm["wall_price"])/wall-1)*10000>2.0:
                self.wall_arms[k]={"armed_ms":now,"wall_price":wall,"initial_ratio":ratio}
                self._event(symbol,"DYNAMIC_SCALP_WALL_ARM",side,{"side":side,"wall_price":wall,"wall_ratio":ratio,"distance_bps":dist},exchange_ts_ms)
                continue
            if now-int(arm["armed_ms"])<LIQUID.rejection_confirm_ms:
                continue
            # "not eaten >50%" proxy: wall ratio must retain at least half its initial relative density.
            retained=ratio/max(1e-9,float(arm["initial_ratio"]))
            if retained < (1.0-LIQUID.max_wall_consumed_frac):
                self.wall_arms.pop(k,None);continue
            imb=float(f.get("book_imbalance_5",0) or 0)
            if side=="BUY" and imb<LIQUID.min_book_imbalance_abs: continue
            if side=="SELL" and imb>-LIQUID.min_book_imbalance_abs: continue
            # Conservative shadow fill: cross the spread rather than assume a perfect maker fill.
            px=float(book.best_ask if side=="BUY" else book.best_bid)
            if px>0:
                self._open(symbol,"LIQUID",side,px,f,exchange_ts_ms,{
                    "wall_price":wall,"wall_ratio":ratio,"wall_retained_frac":retained,
                })
                self.wall_arms.pop(k,None)
                break

    def on_snapshot(self, symbol, f, book, exchange_ts_ms=0):
        if not book.synced or not book.mid:
            return
        self._manage(symbol,f,book,exchange_ts_ms)
        self.hist[symbol].append({
            "ts":now_ms(),"mid":float(book.mid),"tc1":int(f.get("trade_count_1s",0) or 0),
            "tc5":int(f.get("trade_count_5s",0) or 0),
        })
        if symbol in self.positions:
            return
        self._hype_signal(symbol,f,book,exchange_ts_ms)
        if symbol not in self.positions:
            self._liquid_signal(symbol,f,book,exchange_ts_ms)
