"""BBB PAPER execution engine v0.

Strictly simulation: consumes local order books and writes paper trades to SQLite.
There is no private Bybit API, key, signer, or order submission path here.
"""
from __future__ import annotations
from dataclasses import dataclass
import json, os, sqlite3, uuid
from typing import Dict

from engine import PaperBook, SignalSnapshot, HeadName, ProductClass, BYBIT_FEES, now_ms
from orderbook import LocalBook
from sanitizer import Sanitizer
from strategy_cells import (
    POLICIES, classify_regime, directional_scores, projected_edge_bps,
    size_decision, policies_json,
    classify_tail_accel, classify_tail_accel_delta, tail_milestones_hit, tail_active_floor_bps,
    tail_trail_bps, tail_trail_bps_binding, tail_extreme_giveback_cap_bps, tail_ratchet_stop_bps, tail_timeout_ms, sniper_entry_quality,
)

class PaperTrader:
    def __init__(self, cx: sqlite3.Connection, session_id: str, start_usd: float, launch_symbols:set[str]):
        self.cx=cx
        self.session_id=session_id
        self.start_usd=start_usd
        self.launch_symbols=launch_symbols
        self.book=PaperBook(start_usd)
        self.last_close_ms: Dict[str,int]={}
        self.enabled=os.getenv("BBB_PAPER_ENTRIES","0").lower() in {"1","true","yes","on"}
        self.leverage=max(1.0,float(os.getenv("BBB_PAPER_LEVERAGE","1")))
        self.skim_trigger=float(os.getenv("BBB_SKIM_TRIGGER_USD","10"))
        self.skim_fraction=float(os.getenv("BBB_SKIM_FRACTION","0.5"))
        train=[x.strip() for x in os.getenv("BBB_SANITIZER_TRAIN_SESSIONS","").split(",") if x.strip()]
        self.training_sessions=train
        self.sanitizer=Sanitizer(
            cx,train,
            min_n=int(os.getenv("BBB_SANITIZER_MIN_N","12")),
            min_winner_retention=float(os.getenv("BBB_SANITIZER_WINNER_RETENTION","0.90")),
            min_losers_removed=int(os.getenv("BBB_SANITIZER_MIN_LOSERS_REMOVED","2")),
        )
        self.sanitizer_rejects=0
        self.elc_shadows=[]
        # H3T-B: post-arm tail state, keyed by position_id.
        self.tail_state: Dict[str,dict]={}
        # H3T-D SHADOW: classify post-entry persistence without blocking H3T-C entries.
        self.sniper_shadow: Dict[str,dict]={}
        # H3T-B2 SHADOW: same real PAPER entry/arming, hypothetical post-arm exit only.
        # Never closes or resizes a real position.
        self.b2_tail_shadow: Dict[str,dict]={}
        self._write_wallet_meta()

    def _product(self,symbol:str) -> ProductClass:
        return ProductClass.INNOVATION if symbol in self.launch_symbols else ProductClass.STANDARD

    def _head_enum(self,head:str) -> HeadName:
        return HeadName(head)

    def _head_count(self,head:str) -> int:
        return sum(1 for p in self.book.positions.values() if p.head.value==head)

    def _position_for_symbol(self,symbol:str):
        for p in self.book.positions.values():
            if p.symbol==symbol:return p
        return None

    def _directional_momentum(self, side:str, f:dict) -> float:
        sign=1.0 if side=="BUY" else -1.0
        return (
            sign*float(f.get("ret_1s_bps",0))
            + 0.5*sign*float(f.get("ret_250ms_bps",0))
        )

    def _write_wallet_meta(self):
        w=self.book.wallets
        meta={
            "paper_entries_enabled":"1" if self.enabled else "0",
            "trading_cash_usd":f"{w.trading_cash_usd:.8f}",
            "profit_wallet_usd":f"{w.profit_wallet_usd:.8f}",
            "realized_pnl_usd":f"{w.realized_pnl_usd:.8f}",
            "fees_usd":f"{w.fees_usd:.8f}",
            "funding_usd":f"{w.funding_usd:.8f}",
            "slippage_impact_usd":f"{w.slippage_impact_usd:.8f}",
            "paper_open_positions":str(len(self.book.positions)),
            "sanitizer_rejects":str(self.sanitizer_rejects),
            "sanitizer_rules_json":self.sanitizer.as_json(),
            "strategy_cells_json":policies_json(),
        }
        for k,v in meta.items():
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,v))

    def _ledger(self,wallet,head,position_id,kind,amount,balance,detail):
        self.cx.execute("""INSERT INTO wallet_ledger(ts_ms,session_id,wallet,head,position_id,kind,amount_usd,balance_after_usd,detail_json)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (now_ms(),self.session_id,wallet,head,position_id,kind,amount,balance,json.dumps(detail,separators=(",",":"))))

    def _historical_penalty(self,bucket:str,side:str,regime:str) -> float:
        if not self.training_sessions:
            return 0.0
        qmarks=",".join("?" for _ in self.training_sessions)
        rows=self.cx.execute(f"""SELECT net_pnl_usd,entry_features_json FROM trades
                                WHERE status='CLOSED' AND side=? AND session_id IN ({qmarks})
                                ORDER BY id DESC LIMIT 300""",
                             (side,*self.training_sessions)).fetchall()
        vals=[]
        for pnl,js in rows:
            try: ef=json.loads(js or "{}")
            except Exception: ef={}
            if str(ef.get("vol_bucket","FAST"))!=bucket: continue
            if str(ef.get("regime",""))!=regime: continue
            vals.append(float(pnl or 0))
        if len(vals)<20:
            return 0.0
        exp=sum(vals)/len(vals)
        wr=sum(1 for x in vals if x>0)/len(vals)
        penalty=0.0
        if exp<0: penalty+=12.0
        if wr<0.35: penalty+=8.0
        return penalty

    def _emit_b2_pair(self, pid:str, st:dict):
        payload={
            "impl":"b2-shadow-v4.1-extreme-cap",
            "position_id":pid,
            "symbol":st["symbol"],
            "side":st["side"],
            "bucket":st["bucket"],
            "a_exited_before_b2":bool(st.get("a_exit_ms") and st.get("b2_exit_ms") and int(st["a_exit_ms"])<=int(st["b2_exit_ms"])),
            "a_exit_reason":st.get("a_exit_reason"),
            "a_exit_move_bps":st.get("a_exit_move_bps"),
            "a_capture":st.get("a_capture"),
            "a_exit_ms":st.get("a_exit_ms"),
            "b2_exit_reason":st.get("b2_exit_reason"),
            "b2_exit_move_bps":st.get("b2_exit_move_bps"),
            "b2_capture":st.get("b2_capture"),
            "b2_exit_ms":st.get("b2_exit_ms"),
            "b2_peak_bps":st.get("peak_bps"),
            "b2_accel_final":st.get("accel"),
            "b2_accel_seen":sorted(st.get("accel_seen",set())),
            "b2_accel_ticks":st.get("accel_ticks",{}),
            "b2_transitions":st.get("transitions",0),
            "b2_max_delta_m":st.get("max_delta_m",0.0),
            "b2_ratchet_stop_bps":st.get("ratchet_stop_bps"),
            "b2_ratchet_raises":st.get("ratchet_raises",0),
            "b2_extreme_cap_applied":st.get("extreme_cap_applied",False),
            "b2_raw_accel_final":st.get("raw_accel"),
            "b2_warmup_ticks":st.get("warmup_ticks",0),
            "b2_decel_streak":st.get("decel_streak",0),
            "b2_milestones_hit":st.get("milestones_hit",[]),
        }
        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (now_ms(),0,self.session_id,st["head"],st["symbol"],
                         "B2_SHADOW_PAIR","PAIRED",json.dumps(payload,separators=(",",":"))))

    def _finish_b2_shadow(self, pid:str, st:dict, reason:str, move_bps:float, book:LocalBook):
        if st.get("b2_done"):
            return
        exit_side="SELL" if st["side"]=="BUY" else "BUY"
        exit_vwap,filled=book.walk_vwap(exit_side,float(st["notional_usd"]))
        if not exit_vwap or filled<float(st["notional_usd"])*0.999:
            return
        direction=1.0 if st["side"]=="BUY" else -1.0
        vwap_move=direction*(float(exit_vwap)/float(st["entry_vwap"])-1.0)*10000.0
        peak=float(st.get("peak_bps",0.0) or 0.0)
        st["b2_done"]=True
        st["b2_exit_reason"]=reason
        st["b2_exit_move_bps"]=vwap_move
        st["b2_capture"]=(vwap_move/peak) if peak>0 else None
        st["b2_exit_ms"]=now_ms()
        payload={
            "impl":"b2-shadow-v4.1-extreme-cap",
            "position_id":pid,
            "side":st["side"],
            "bucket":st["bucket"],
            "entry_vwap":st["entry_vwap"],
            "exit_vwap":float(exit_vwap),
            "exit_mid":float(book.mid or 0),
            "exit_move_bps":vwap_move,
            "move_bps_mid":move_bps,
            "peak_bps":peak,
            "capture":st["b2_capture"],
            "reason":reason,
            "age_ms":int(st["b2_exit_ms"])-int(st["arm_ms"]),
            "accel_final":st.get("accel"),
            "accel_seen":sorted(st.get("accel_seen",set())),
            "accel_ticks":st.get("accel_ticks",{}),
            "transitions":st.get("transitions",0),
            "max_delta_m":st.get("max_delta_m",0.0),
            "raw_accel_final":st.get("raw_accel"),
            "warmup_ticks":st.get("warmup_ticks",0),
            "warmup_target":st.get("warmup_target",3),
            "decel_streak":st.get("decel_streak",0),
            "m_prev":st.get("m_prev"),
            "m_now":st.get("m_now"),
            "delta_m":st.get("delta_m"),
            "milestones_hit":st.get("milestones_hit",[]),
            "active_floor_bps":st.get("active_floor_bps"),
            "raw_trail_bps":st.get("raw_trail_bps"),
            "trail_before_extreme_cap_bps":st.get("trail_before_extreme_cap_bps"),
            "binding_trail_bps":st.get("trail_bps"),
            "extreme_cap_applied":st.get("extreme_cap_applied",False),
            "candidate_stop_bps":st.get("candidate_stop_bps"),
            "ratchet_stop_bps":st.get("ratchet_stop_bps"),
            "ratchet_raises":st.get("ratchet_raises",0),
            "binding_applied":st.get("binding_applied",False),
            "a_exited_before_b2":bool(st.get("a_exit_ms") and int(st["a_exit_ms"])<=int(st["b2_exit_ms"])),
            "a_exit_reason":st.get("a_exit_reason"),
            "a_exit_move_bps":st.get("a_exit_move_bps"),
            "a_capture":st.get("a_capture"),
        }
        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (now_ms(),book.exchange_ts_ms,self.session_id,st["head"],st["symbol"],
                         "B2_SHADOW_EXIT",reason,json.dumps(payload,separators=(",",":"))))
        if st.get("a_done"):
            self._emit_b2_pair(pid,st)
            self.b2_tail_shadow.pop(pid,None)

    def _update_b2_shadows(self, symbol:str, f:dict, book:LocalBook):
        if not self.b2_tail_shadow or not book.mid:
            return
        now=now_ms()
        for pid,st in list(self.b2_tail_shadow.items()):
            if st.get("symbol")!=symbol or st.get("b2_done"):
                continue

            mid=float(book.mid)
            direction=1.0 if st["side"]=="BUY" else -1.0
            move_bps=direction*(mid/float(st["entry_vwap"])-1.0)*10000.0
            if move_bps>float(st.get("peak_bps",move_bps)):
                st["peak_bps"]=move_bps
            peak=float(st.get("peak_bps",move_bps))

            m_now=self._directional_momentum(st["side"],f)
            m_prev=float(st.get("m_prev",m_now))
            delta=m_now-m_prev
            raw_accel=classify_tail_accel_delta(m_now,m_prev)
            st["raw_accel"]=raw_accel

            # B2-v3 start-condition fix:
            # 1) first 3 post-arm ticks are forced FLAT (warm-up)
            # 2) after warm-up, DECEL requires 2 consecutive raw DECEL ticks
            # 3) a single negative tick never tightens the trail immediately
            warmup_ticks=int(st.get("warmup_ticks",0))
            warmup_target=int(st.get("warmup_target",3))
            decel_streak=int(st.get("decel_streak",0))

            if warmup_ticks < warmup_target:
                accel="FLAT"
                st["warmup_ticks"]=warmup_ticks+1
                st["decel_streak"]=0
            else:
                if raw_accel=="DECEL":
                    decel_streak += 1
                else:
                    decel_streak = 0
                st["decel_streak"]=decel_streak

                if raw_accel=="ACCEL":
                    accel="ACCEL"
                elif raw_accel=="DECEL" and decel_streak>=2:
                    accel="DECEL"
                else:
                    # Hold the prior effective state through one isolated negative
                    # raw tick; FLAT is the neutral fallback.
                    prior=str(st.get("accel","FLAT"))
                    accel=prior if prior in {"ACCEL","FLAT"} else "FLAT"

            tick_total=sum(int(v) for v in st.get("accel_ticks",{}).values())
            if st.get("accel")!=accel and tick_total>0:
                st["transitions"]=int(st.get("transitions",0))+1
            st["accel"]=accel
            st.setdefault("accel_seen",set()).add(accel)
            ticks=st.setdefault("accel_ticks",{"ACCEL":0,"FLAT":0,"DECEL":0})
            ticks[accel]=int(ticks.get(accel,0))+1
            st["m_prev"]=m_now
            st["m_now"]=m_now
            st["delta_m"]=delta
            if abs(delta)>abs(float(st.get("max_delta_m",0.0))):
                st["max_delta_m"]=delta

            tp=float(st["tp_bps"])
            hit=tail_milestones_hit(peak,tp)
            st["milestones_hit"]=hit
            active_floor=tail_active_floor_bps(tp,float(st["base_floor_bps"]),hit)
            raw_trail=tail_trail_bps(peak,tp,accel,st["bucket"])
            trail=tail_trail_bps_binding(raw_trail,peak,active_floor,hit)
            trail_before_extreme_cap=trail
            trail,extreme_cap_applied=tail_extreme_giveback_cap_bps(
                peak,accel,trail,st["bucket"]
            )

            # B2-v4.1 profit-lock ratchet:
            # state/volatility may change the candidate trail width, but the
            # protected profit level can NEVER move backwards after arm.
            prev_stop=st.get("ratchet_stop_bps")
            candidate_stop_bps,ratchet_stop=tail_ratchet_stop_bps(prev_stop,peak,trail,active_floor)
            if prev_stop is not None and ratchet_stop>float(prev_stop)+1e-12:
                st["ratchet_raises"]=int(st.get("ratchet_raises",0))+1
            st["ratchet_stop_bps"]=ratchet_stop

            st["active_floor_bps"]=active_floor
            st["raw_trail_bps"]=raw_trail
            st["trail_before_extreme_cap_bps"]=trail_before_extreme_cap
            st["trail_bps"]=trail
            st["extreme_cap_applied"]=extreme_cap_applied
            st["candidate_stop_bps"]=candidate_stop_bps
            st["binding_applied"]=bool(trail_before_extreme_cap < raw_trail - 1e-12)

            from_peak=peak-move_bps
            since_arm=now-int(st["arm_ms"])
            timeout=tail_timeout_ms(int(st["max_hold_ms"]),accel)
            hard_cap=int(st["hard_cap_ms"])

            reason=None
            if move_bps<=-float(st["sl_bps"]):
                reason="B2_SHADOW_SL"
            elif move_bps<=ratchet_stop:
                reason={
                    "ACCEL":"B2_SHADOW_ACCEL_RATCHET",
                    "FLAT":"B2_SHADOW_RATCHET",
                    "DECEL":"B2_SHADOW_DECEL_RATCHET",
                }[accel]
            elif since_arm>=timeout:
                reason="B2_SHADOW_TIME"
            elif since_arm>=hard_cap:
                reason="B2_SHADOW_HARD_CAP"

            if reason:
                self._finish_b2_shadow(pid,st,reason,move_bps,book)

    def on_snapshot(self,symbol:str,head:str,f:dict,book:LocalBook,exchange_ts_ms:int,receive_ts_ms:int,bucket:str="FAST",allow_new_entries:bool=True):
        if not self.enabled or not book.synced or not book.mid:
            return
        self._update_elc_shadows(symbol,book)
        self._update_b2_shadows(symbol,f,book)
        p=self._position_for_symbol(symbol)
        if p:
            self._manage_open(p,f,book)
            return
        if not allow_new_entries:
            return

        long_score,short_score=directional_scores(head,f)
        side="BUY" if long_score>=short_score else "SELL"
        score=max(long_score,short_score)
        pol=POLICIES[(bucket,side)]
        if self._head_count(head)>=pol.max_open:return
        if now_ms()-self.last_close_ms.get(symbol,0)<pol.cooldown_ms:return
        if float(f.get("spread_bps",9999))>pol.max_spread_bps:return
        if int(f.get("trade_count_1s",0))<pol.min_trades_1s:return
        if score<pol.gate:return
        regime=classify_regime(f)
        if regime not in pol.allow_regimes:return
        san_features=dict(f)
        san_features["vol_bucket"]=bucket
        san_features["entry_score"]=score
        san_features["source_age_ms"]=max(0,int(receive_ts_ms or now_ms())-int(exchange_ts_ms or 0)) if exchange_ts_ms else 0
        san_features["processing_ms"]=0
        allowed,san_reason=self.sanitizer.decision(head,side,san_features)
        if not allowed:
            self.sanitizer_rejects+=1
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now_ms(),exchange_ts_ms,self.session_id,head,symbol,"SANITIZER_REJECT",san_reason,
                             json.dumps({"side":side,"score":score,"features":san_features},separators=(",",":"))))
            self._write_wallet_meta()
            return
        product=self._product(symbol)
        fee_bps=BYBIT_FEES[product].taker_bps
        expected_cost=2*fee_bps+float(f.get("spread_bps",0))
        edge=projected_edge_bps(side,score,pol.gate,f)
        required_cost_edge=expected_cost*pol.cost_mult+pol.cost_buffer_bps
        if edge<required_cost_edge:
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now_ms(),exchange_ts_ms,self.session_id,head,symbol,"COST_GATE_REJECT",pol.cell,
                             json.dumps({"side":side,"regime":regime,"edge_bps":edge,
                                         "required_bps":required_cost_edge,"expected_cost_bps":expected_cost},
                                        separators=(",",":"))))
            return

        history_penalty=self._historical_penalty(bucket,side,regime)
        sizing=size_decision(pol,side,score,regime,f,required_cost_edge,edge,history_penalty)
        if sizing["reject"]:
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now_ms(),exchange_ts_ms,self.session_id,head,symbol,"SIZING_REJECT",
                             str(sizing.get("reject_reason") or "LOW_SIZING_SCORE"),
                             json.dumps({"side":side,"bucket":bucket,"regime":regime,**sizing},separators=(",",":"))))
            return

        # H3T-D SHADOW: score the entry, but DO NOT block or resize it.
        # Persistence is observed for ~1.5s after entry and later compared with PnL.
        sniper=sniper_entry_quality(sizing,regime,f)
        tier=str(sizing["tier"])
        size_mult=float(sizing["size_multiplier"])
        confidence=float(sizing["winner_score"])
        base_margin=max(1.0,self.start_usd*pol.margin_fraction)
        margin=max(1.0,base_margin*size_mult)
        # Never let one PAPER position consume more than 5% of the original bank.
        margin=min(margin,self.start_usd*0.05)
        notional=margin*self.leverage
        entry_vwap,filled=book.walk_vwap(side,notional)
        if not entry_vwap or filled<notional*0.999:return

        sig=SignalSnapshot(
            exchange_ts_ms=int(exchange_ts_ms or 0),
            receive_ts_ms=int(receive_ts_ms or now_ms()),
            signal_ts_ms=now_ms(),
            symbol=symbol,head=self._head_enum(head),product_class=product,
            score=score,gate=pol.gate,bid=float(book.best_bid or 0),ask=float(book.best_ask or 0),
            expected_entry_vwap=float(entry_vwap),spread_bps=float(f.get("spread_bps",0)),
            expected_roundtrip_cost_bps=expected_cost,risk_state=f"{pol.cell}|{regime}|{tier}",
            features={**dict(f),"strategy_cell":pol.cell,"regime":regime,
                      "projected_edge_bps":edge,"required_cost_edge_bps":required_cost_edge,
                      "vol_bucket":bucket,"sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence,
                      "winner_score":sizing["winner_score"],"loser_risk":sizing["loser_risk"],
                      "sizing_score":sizing["sizing_score"],"edge_ratio":sizing["edge_ratio"],
                      "aligned_count":sizing["aligned_count"],"opposing_count":sizing["opposing_count"],
                      "spread_quality":sizing["spread_quality"],"depth_quality":sizing["depth_quality"],
                      "history_penalty":sizing["history_penalty"],"sizing_violations":sizing["sizing_violations"],
                      "sniper_shadow_entry_clear":sniper.get("sniper_clear"),
                      "sniper_shadow_entry_super":sniper.get("sniper_super"),
                      "sniper_shadow_entry_d250":sniper.get("sniper_d250"),
                      "sniper_shadow_entry_d1":sniper.get("sniper_d1"),
                      "sniper_shadow_entry_d5":sniper.get("sniper_d5"),
                      "sniper_shadow_entry_dflow":sniper.get("sniper_dflow"),
                      "sniper_shadow_entry_dimb":sniper.get("sniper_dimb")},
        )
        pid=str(uuid.uuid4())
        pos=self.book.open_position(pid,sig,side,self.leverage,margin,float(entry_vwap),maker=False)
        self.cx.execute("""INSERT INTO trades(
            session_id,position_id,head,venue,product_class,symbol,side,leverage,margin_usd,notional_usd,
            signal_exchange_ts_ms,receive_ts_ms,signal_ts_ms,order_ts_ms,fill_ts_ms,
            signal_mid,signal_bid,signal_ask,expected_entry_vwap,entry_vwap,
            entry_fee_usd,source_age_ms,processing_ms,signal_to_fill_ms,
            entry_score,entry_gate,risk_state,entry_features_json,status,opened_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.session_id,pid,head,"BYBIT",product.value,symbol,side,self.leverage,margin,pos.notional_usd,
             sig.exchange_ts_ms,sig.receive_ts_ms,sig.signal_ts_ms,sig.signal_ts_ms,now_ms(),
             float(f["mid"]),sig.bid,sig.ask,sig.expected_entry_vwap,pos.entry_vwap,
             pos.entry_fee_usd,sig.source_age_ms,sig.processing_ms,max(0,now_ms()-sig.signal_ts_ms),
             score,pol.gate,f"{pol.cell}|{regime}|{tier}",json.dumps({**f,"strategy_cell":pol.cell,"regime":regime,
                 "projected_edge_bps":edge,"required_cost_edge_bps":required_cost_edge,"vol_bucket":bucket,
                 "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence,
                 "winner_score":sizing["winner_score"],"loser_risk":sizing["loser_risk"],
                 "sizing_score":sizing["sizing_score"],"edge_ratio":sizing["edge_ratio"],
                 "aligned_count":sizing["aligned_count"],"opposing_count":sizing["opposing_count"],
                 "spread_quality":sizing["spread_quality"],"depth_quality":sizing["depth_quality"],
                 "history_penalty":sizing["history_penalty"],"sizing_violations":sizing["sizing_violations"],
                 "sniper_shadow_entry_clear":sniper.get("sniper_clear"),
                 "sniper_shadow_entry_super":sniper.get("sniper_super"),
                 "sniper_shadow_entry_d250":sniper.get("sniper_d250"),
                 "sniper_shadow_entry_d1":sniper.get("sniper_d1"),
                 "sniper_shadow_entry_d5":sniper.get("sniper_d5"),
                 "sniper_shadow_entry_dflow":sniper.get("sniper_dflow"),
                 "sniper_shadow_entry_dimb":sniper.get("sniper_dimb")},
                 separators=(",",":")),"OPEN",pos.opened_ms))
        self._ledger("TRADING",head,pid,"OPEN_MARGIN",-margin-pos.entry_fee_usd,self.book.wallets.trading_cash_usd,
                     {"side":side,"symbol":symbol,"entry_vwap":entry_vwap,"notional":notional,
                      "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence,
                      "winner_score":sizing["winner_score"],"loser_risk":sizing["loser_risk"],
                      "sizing_score":sizing["sizing_score"],"edge_ratio":sizing["edge_ratio"],
                      "sniper_shadow_entry_clear":sniper.get("sniper_clear"),
                      "sniper_shadow_entry_super":sniper.get("sniper_super")})
        self.sniper_shadow[pid]={
            "entry_ms":pos.opened_ms,
            "hits":1 if sniper.get("sniper_clear") else 0,
            "super_hits":1 if sniper.get("sniper_super") else 0,
            "first_good_ms":pos.opened_ms if sniper.get("sniper_clear") else None,
            "last_good_ms":pos.opened_ms if sniper.get("sniper_clear") else None,
            "outcome":None,
            "confirm_ms":None,
            "max_hits":1 if sniper.get("sniper_clear") else 0,
        }
        self._write_wallet_meta()

    def _update_elc_shadows(self,symbol:str,book:LocalBook):
        if not self.elc_shadows or not book.mid:
            return
        now=now_ms()
        keep=[]
        checkpoints=((10_000,1,10),(30_000,2,30),(60_000,4,60))
        for s in self.elc_shadows:
            if s["symbol"]!=symbol:
                if now-s["cut_ms"]<=65_000: keep.append(s)
                continue
            sign=1.0 if s["side"]=="BUY" else -1.0
            ret=sign*(float(book.mid)/float(s["entry_vwap"])-1.0)*10000
            s["max_after_bps"]=max(float(s["max_after_bps"]),ret)
            s["min_after_bps"]=min(float(s["min_after_bps"]),ret)
            age=now-s["cut_ms"]
            for ms,bit,sec in checkpoints:
                if age>=ms and not (int(s["mask"]) & bit):
                    s["mask"]=int(s["mask"])|bit
                    recovered=ret>0 or float(s["max_after_bps"])>0
                    self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                                       VALUES(?,?,?,?,?,?,?,?)""",
                                    (now,book.exchange_ts_ms,self.session_id,"VOL",symbol,"ELC_COUNTERFACTUAL",f"{sec}s",
                                     json.dumps({"position_id":s["position_id"],"side":s["side"],
                                                 "cut_move_bps":s["cut_move_bps"],
                                                 "mfe_bps_at_cut":s["mfe_bps_at_cut"],
                                                 "mae_bps_at_cut":s["mae_bps_at_cut"],
                                                 "cf_return_bps":ret,
                                                 "cf_max_after_bps":s["max_after_bps"],
                                                 "cf_min_after_bps":s["min_after_bps"],
                                                 "recovered":recovered},separators=(",",":"))))
            if age<=60_000: keep.append(s)
        self.elc_shadows=keep

    def _manage_open(self,p,f,book):
        mid=float(book.mid or p.entry_vwap)
        direction=1.0 if p.side=="BUY" else -1.0
        move_bps=direction*(mid/p.entry_vwap-1.0)*10000
        now=now_ms()
        old_mfe=p.mfe_pct*100.0
        old_mae=p.mae_pct*100.0
        if move_bps>old_mfe:
            p.mfe_pct=move_bps/100.0
            p.mfe_ms=now
        if move_bps<old_mae:
            p.mae_pct=move_bps/100.0
            p.mae_ms=now
        ef={}
        try:
            ef=p.signal.features or {}
        except Exception:
            ef={}
        bucket=str(ef.get("vol_bucket","FAST"))
        pol=POLICIES[(bucket,p.side)]
        age=now-p.opened_ms
        mfe_bps=p.mfe_pct*100.0
        mae_bps=p.mae_pct*100.0

        # H3T-D SHADOW: observe whether the original hard sniper would confirm
        # shortly AFTER the H3T-C entry. This never changes entry, size, or exit.
        sh=self.sniper_shadow.get(p.position_id)
        if sh is not None and sh.get("outcome") is None:
            try:
                cur_long,cur_short=directional_scores(p.head.value,f)
                cur_score=cur_long if p.side=="BUY" else cur_short
                cur_regime=classify_regime(f)
                product=self._product(p.symbol)
                fee_bps=BYBIT_FEES[product].taker_bps
                cur_expected_cost=2*fee_bps+float(f.get("spread_bps",0) or 0)
                cur_edge=projected_edge_bps(p.side,cur_score,pol.gate,f)
                cur_required=cur_expected_cost*pol.cost_mult+pol.cost_buffer_bps
                cur_sizing=size_decision(
                    pol,p.side,cur_score,cur_regime,f,cur_required,cur_edge,
                    float(ef.get("history_penalty",0) or 0),
                )
                cur_sniper=sniper_entry_quality(cur_sizing,cur_regime,f)
                pre_ok=(
                    float(f.get("spread_bps",9999) or 9999)<=pol.max_spread_bps
                    and int(f.get("trade_count_1s",0) or 0)>=pol.min_trades_1s
                    and cur_score>=pol.gate
                    and cur_regime in pol.allow_regimes
                    and cur_edge>=cur_required
                    and not cur_sizing.get("reject")
                )
                good=bool(pre_ok and cur_sniper.get("sniper_clear"))
                if good:
                    last=sh.get("last_good_ms")
                    if last is None or now-int(last)>1500:
                        sh["hits"]=1
                        sh["first_good_ms"]=now
                    else:
                        sh["hits"]=int(sh.get("hits",0))+1
                    sh["last_good_ms"]=now
                    if cur_sniper.get("sniper_super"):
                        sh["super_hits"]=int(sh.get("super_hits",0))+1
                    sh["max_hits"]=max(int(sh.get("max_hits",0)),int(sh.get("hits",0)))
                    first=int(sh.get("first_good_ms") or now)
                    confirm_ms=now-first
                    if int(sh.get("hits",0))>=3 and confirm_ms>=650:
                        sh["outcome"]="PASS"
                        sh["confirm_ms"]=confirm_ms
                        sh["grade"]="SUPER" if int(sh.get("super_hits",0))>=2 else "CLEAR"
                        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                                           VALUES(?,?,?,?,?,?,?,?)""",
                                        (now,book.exchange_ts_ms,self.session_id,p.head.value,p.symbol,
                                         "SNIPER_SHADOW","PASS",
                                         json.dumps({"position_id":p.position_id,"side":p.side,"bucket":bucket,
                                                     "hits":sh["hits"],"super_hits":sh["super_hits"],
                                                     "confirm_ms":confirm_ms,"grade":sh["grade"],
                                                     "move_bps_at_confirm":move_bps},
                                                    separators=(",",":"))))
                if sh.get("outcome") is None and age>=1500:
                    sh["outcome"]="REJECT"
                    sh["grade"]=None
                    self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                                       VALUES(?,?,?,?,?,?,?,?)""",
                                    (now,book.exchange_ts_ms,self.session_id,p.head.value,p.symbol,
                                     "SNIPER_SHADOW","REJECT",
                                     json.dumps({"position_id":p.position_id,"side":p.side,"bucket":bucket,
                                                 "hits":sh.get("hits",0),"super_hits":sh.get("super_hits",0),
                                                 "max_hits":sh.get("max_hits",0),"age_ms":age},
                                                separators=(",",":"))))
            except Exception as e:
                if age>=1500 and sh.get("outcome") is None:
                    sh["outcome"]="ERROR"
                    sh["error"]=str(e)[:200]

        # ELC adapted from RHB: cut only a confirmed weak trajectory, not a shallow
        # dip that has begun to recover. Thresholds scale to each VOL bucket policy.
        sign=1.0 if p.side=="BUY" else -1.0
        entry_flow=(float(ef.get("buy_ratio_1s",0.5))-0.5)*2.0*sign
        current_flow=(float(f.get("buy_ratio_1s",0.5))-0.5)*2.0*sign
        entry_imb=float(ef.get("book_imbalance_5",0))*sign
        current_imb=float(f.get("book_imbalance_5",0))*sign
        flow_drop=(entry_flow+entry_imb)-(current_flow+current_imb)
        flow_deteriorating=(flow_drop>=0.20 or current_flow<0 or current_imb<-0.15)

        winner_veto=(
            age>=3_000
            and mae_bps>=-0.45*pol.stop_loss_bps
            and (now-p.mae_ms)>=2_000
            and move_bps>=-0.15*pol.stop_loss_bps
            and p.mfe_ms>p.mae_ms
            and mfe_bps>0
        )
        r2_confirm=(
            age>=10_000
            and mfe_bps<=0.35*pol.take_profit_bps
            and move_bps<0
            and mae_bps<=-0.55*pol.stop_loss_bps
            and flow_deteriorating
        )
        early_loser_cut=(not winner_veto) and r2_confirm

        # Right-tail collector. Arm before nominal TP when direction remains strongly
        # aligned, then protect a positive floor and trail the right tail.
        momentum_aligned=max(
            sign*float(f.get("ret_250ms_bps",0)),
            sign*float(f.get("ret_1s_bps",0)),
            0.5*sign*float(f.get("ret_5s_bps",0)),
        )
        continuation=(
            classify_regime(f) in {"TREND","IMPULSE"}
            and momentum_aligned>0
            and current_flow>-0.05
            and current_imb>-0.20
        )
        tail_arm_trigger=0.68*pol.take_profit_bps
        if (not p.tail_armed) and mfe_bps>=tail_arm_trigger and continuation:
            p.tail_armed=True
            p.tail_arm_ms=now
            self.tail_state[p.position_id]={
                "tail_peak_bps":mfe_bps,
                "tail_arm_momentum":self._directional_momentum(p.side,f),
                "tail_accel":"FLAT",
                "tail_milestones_hit":[],
            }
            # Start B2 from the exact same arm event. It is observation-only.
            m_arm=self._directional_momentum(p.side,f)
            self.b2_tail_shadow[p.position_id]={
                "symbol":p.symbol,"side":p.side,"head":p.head.value,"bucket":bucket,
                "entry_vwap":float(p.entry_vwap),"notional_usd":float(p.notional_usd),
                "opened_ms":int(p.opened_ms),"arm_ms":now,
                "tp_bps":float(pol.take_profit_bps),"sl_bps":float(pol.stop_loss_bps),
                "max_hold_ms":int(pol.max_hold_ms),"hard_cap_ms":int(pol.max_hold_ms)*6,
                "base_floor_bps":max(0.18*float(pol.take_profit_bps),0.65*float(ef.get("required_cost_edge_bps",0) or 0)),
                "peak_bps":float(mfe_bps),"m_prev":m_arm,"m_now":m_arm,"delta_m":0.0,
                "max_delta_m":0.0,"accel":"FLAT","raw_accel":"FLAT","accel_seen":set(),
                "accel_ticks":{"ACCEL":0,"FLAT":0,"DECEL":0},"transitions":0,
                "warmup_ticks":0,"warmup_target":3,"decel_streak":0,
                "milestones_hit":[],"ratchet_stop_bps":None,"ratchet_raises":0,
                "a_done":False,"b2_done":False,
            }
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now,book.exchange_ts_ms,self.session_id,p.head.value,p.symbol,
                             "B2_SHADOW_ARM","RIGHT_TAIL",
                             json.dumps({"impl":"b2-shadow-v4.1-extreme-cap","position_id":p.position_id,
                                         "side":p.side,"bucket":bucket,"entry_vwap":float(p.entry_vwap),
                                         "arm_mfe_bps":mfe_bps,"arm_momentum":m_arm},
                                        separators=(",",":"))))
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now,book.exchange_ts_ms,self.session_id,p.head.value,p.symbol,"TAIL_ARM","RIGHT_TAIL",
                             json.dumps({"side":p.side,"bucket":bucket,"mfe_bps":mfe_bps,
                                         "move_bps":move_bps,"flow":current_flow,"imbalance":current_imb,
                                         "tail_arm_momentum":self.tail_state[p.position_id]["tail_arm_momentum"]},
                                        separators=(",",":"))))

        from_peak_bps=mfe_bps-move_bps
        tp_bps=pol.take_profit_bps
        expected_cost=float(ef.get("required_cost_edge_bps",0) or 0)
        base_floor_bps=max(0.18*tp_bps,0.65*expected_cost)

        st=self.tail_state.get(p.position_id)
        # Self-heal H3T-B state after an unusual runtime restore/restart.
        # This does not change arming; it only reconstructs post-arm telemetry/state.
        if p.tail_armed and st is None:
            self.tail_state[p.position_id]={
                "tail_peak_bps":mfe_bps,
                "tail_arm_momentum":self._directional_momentum(p.side,f),
                "tail_accel":"FLAT",
                "tail_milestones_hit":[],
            }
            st=self.tail_state[p.position_id]

        active_floor_bps=base_floor_bps
        trail_bps_eff=0.30*max(mfe_bps,tp_bps)
        timeout_ms_eff=pol.max_hold_ms*3
        accel="FLAT"

        if p.tail_armed and st is not None:
            if mfe_bps>st["tail_peak_bps"]:
                st["tail_peak_bps"]=mfe_bps
            m_now=self._directional_momentum(p.side,f)
            accel=classify_tail_accel(m_now,st["tail_arm_momentum"])
            st["tail_accel"]=accel
            hit=tail_milestones_hit(mfe_bps,tp_bps)
            st["tail_milestones_hit"]=hit
            active_floor_bps=tail_active_floor_bps(tp_bps,base_floor_bps,hit)
            trail_bps_eff=tail_trail_bps(mfe_bps,tp_bps,accel,bucket)
            timeout_ms_eff=tail_timeout_ms(pol.max_hold_ms,accel)

        floor_fired=p.tail_armed and move_bps<=active_floor_bps
        trail_fired=p.tail_armed and from_peak_bps>=trail_bps_eff

        reason=None
        if move_bps<=-pol.stop_loss_bps:
            reason="STOP_LOSS"
        elif early_loser_cut:
            reason="EARLY_LOSER_CUT"
        elif floor_fired:
            reason="RIGHT_TAIL_MILESTONE" if (st and st.get("tail_milestones_hit")) else "RIGHT_TAIL_FLOOR"
        elif trail_fired:
            reason={
                "ACCEL":"RIGHT_TAIL_ACCEL_TRAIL",
                "FLAT":"RIGHT_TAIL_TRAIL",
                "DECEL":"RIGHT_TAIL_DECEL_TRAIL",
            }[accel]
        elif (not p.tail_armed) and move_bps>=tp_bps:
            reason="TAKE_PROFIT"
        elif p.tail_armed and age>=timeout_ms_eff:
            reason="RIGHT_TAIL_TIME"
        elif (not p.tail_armed) and age>=pol.max_hold_ms:
            reason="TIME_EXIT"
        if not reason:return

        secondary=None
        if floor_fired and trail_fired:
            secondary="TRAIL_ALSO"

        exit_side="SELL" if p.side=="BUY" else "BUY"
        exit_vwap,filled=book.walk_vwap(exit_side,p.notional_usd)
        if not exit_vwap or filled<p.notional_usd*0.999:return
        before=self.book.wallets.trading_cash_usd
        net=self.book.close_position(p.position_id,float(exit_vwap))
        w=self.book.wallets
        exit_fee=BYBIT_FEES[p.product_class].fee_usd(p.notional_usd,False)
        gross=net+p.entry_fee_usd+exit_fee
        self.cx.execute("""UPDATE trades SET exit_vwap=?,exit_fee_usd=?,gross_pnl_usd=?,net_pnl_usd=?,
                          mae_pct=?,mfe_pct=?,hold_ms=?,exit_reason=?,status='CLOSED',closed_ms=?
                          WHERE position_id=?""",
                       (exit_vwap,exit_fee,gross,net,p.mae_pct,p.mfe_pct,age,reason,now_ms(),p.position_id))
        detail={
            "reason":reason,
            "exit_vwap":exit_vwap,
            "net_pnl":net,
            "mfe_bps":mfe_bps,
            "mae_bps":mae_bps,
            "move_bps":move_bps,
            "tail_armed":p.tail_armed,
            "tail_trail_bps":trail_bps_eff,
            "tail_profit_floor_bps":base_floor_bps,
            "active_floor_bps":active_floor_bps,
            "tail_peak_bps":st.get("tail_peak_bps") if st else None,
            "tail_capture_ratio":(
                move_bps/st["tail_peak_bps"]
                if st and st.get("tail_peak_bps",0)>0 else None
            ),
            "tail_accel_final":accel if p.tail_armed else None,
            "tail_milestones_hit":st.get("tail_milestones_hit",[]) if st else [],
            "exit_trigger_secondary":secondary,
            "floor_fired":floor_fired,
            "trail_fired":trail_fired,
            "early_loser_confirmed":early_loser_cut,
            "winner_veto":winner_veto,
            "sniper_shadow_outcome":(
                self.sniper_shadow.get(p.position_id,{}).get("outcome")
                if p.position_id in self.sniper_shadow else None
            ),
            "sniper_shadow_grade":(
                self.sniper_shadow.get(p.position_id,{}).get("grade")
                if p.position_id in self.sniper_shadow else None
            ),
            "sniper_shadow_confirm_ms":(
                self.sniper_shadow.get(p.position_id,{}).get("confirm_ms")
                if p.position_id in self.sniper_shadow else None
            ),
            "sniper_shadow_hits":(
                self.sniper_shadow.get(p.position_id,{}).get("max_hits")
                if p.position_id in self.sniper_shadow else None
            ),
        }
        self._ledger("TRADING",p.head.value,p.position_id,"CLOSE",w.trading_cash_usd-before,w.trading_cash_usd,detail)

        b2=self.b2_tail_shadow.get(p.position_id)
        if b2 is not None and not b2.get("a_done"):
            b2["a_done"]=True
            b2["a_exit_reason"]=reason
            b2["a_exit_move_bps"]=move_bps
            b2["a_capture"]=(
                move_bps/float(b2.get("peak_bps",0))
                if float(b2.get("peak_bps",0) or 0)>0 else None
            )
            b2["a_exit_ms"]=now_ms()
            # v2 semantics: A-close NEVER dictates B2 lifetime. B2 has no ELC
            # and continues until its own SL/trail/timeout/hard-cap.
            if b2.get("b2_done"):
                self._emit_b2_pair(p.position_id,b2)
                self.b2_tail_shadow.pop(p.position_id,None)

        self.tail_state.pop(p.position_id,None)
        self.sniper_shadow.pop(p.position_id,None)
        self.last_close_ms[p.symbol]=now_ms()
        if reason=="EARLY_LOSER_CUT":
            self.elc_shadows.append({
                "position_id":p.position_id,"symbol":p.symbol,"side":p.side,
                "entry_vwap":p.entry_vwap,"cut_ms":now_ms(),
                "cut_move_bps":move_bps,"mfe_bps_at_cut":mfe_bps,"mae_bps_at_cut":mae_bps,
                "max_after_bps":move_bps,"min_after_bps":move_bps,"mask":0,
            })

        # RHB-style reserve wallet: skim half of trading cash above start+trigger.
        excess=w.trading_cash_usd-(self.start_usd+self.skim_trigger)
        if excess>0 and self.skim_fraction>0:
            skim=excess*self.skim_fraction
            w.skim_profit(skim)
            self._ledger("PROFIT",p.head.value,p.position_id,"PROFIT_SKIM",skim,w.profit_wallet_usd,{"trigger":self.skim_trigger})
        self._write_wallet_meta()
