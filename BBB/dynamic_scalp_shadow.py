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


class DynamicScalpShadow:
    def __init__(self, cx, session_id: str):
        self.cx=cx
        self.session_id=session_id
        self.hist: Dict[str, deque] = defaultdict(lambda: deque(maxlen=180))
        self.wall_arms: Dict[tuple[str,str],dict] = {}
        self.positions: Dict[str,dict] = {}
        self.cooldown_until: Dict[str,int] = {}

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
        if f.get("dynamic_segment")!="HYPE_TREND" or symbol in self.positions:
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

        if hi_touches>=HYPE.min_level_touches and cur>hi and speed_ratio>=HYPE.tape_speed_ratio_min and directional_hype_trigger("BUY",f):
            px=float(book.best_ask or cur)
            self._open(symbol,"HYPE","BUY",px,f,exchange_ts_ms,{"level":hi,"touches":hi_touches,"tape_speed_ratio":speed_ratio})
        elif lo_touches>=HYPE.min_level_touches and cur<lo and speed_ratio>=HYPE.tape_speed_ratio_min and directional_hype_trigger("SELL",f):
            px=float(book.best_bid or cur)
            self._open(symbol,"HYPE","SELL",px,f,exchange_ts_ms,{"level":lo,"touches":lo_touches,"tape_speed_ratio":speed_ratio})

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
