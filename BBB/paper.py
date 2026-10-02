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
from strategy_cells import POLICIES, classify_regime, directional_scores, projected_edge_bps, size_decision, policies_json

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

    def on_snapshot(self,symbol:str,head:str,f:dict,book:LocalBook,exchange_ts_ms:int,receive_ts_ms:int,bucket:str="FAST",allow_new_entries:bool=True):
        if not self.enabled or not book.synced or not book.mid:
            return
        self._update_elc_shadows(symbol,book)
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
                            (now_ms(),exchange_ts_ms,self.session_id,head,symbol,"SIZING_REJECT","LOW_SIZING_SCORE",
                             json.dumps({"side":side,"bucket":bucket,"regime":regime,**sizing},separators=(",",":"))))
            return
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
                      "history_penalty":sizing["history_penalty"],"sizing_violations":sizing["sizing_violations"]},
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
                 "history_penalty":sizing["history_penalty"],"sizing_violations":sizing["sizing_violations"]},
                 separators=(",",":")),"OPEN",pos.opened_ms))
        self._ledger("TRADING",head,pid,"OPEN_MARGIN",-margin-pos.entry_fee_usd,self.book.wallets.trading_cash_usd,
                     {"side":side,"symbol":symbol,"entry_vwap":entry_vwap,"notional":notional,
                      "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence,
                      "winner_score":sizing["winner_score"],"loser_risk":sizing["loser_risk"],
                      "sizing_score":sizing["sizing_score"],"edge_ratio":sizing["edge_ratio"]})
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
        try:
            ef=p.signal.features or {}
            bucket=str(ef.get("vol_bucket","FAST"))
        except Exception:
            bucket="FAST"
        pol=POLICIES[(bucket,p.side)]
        age=now-p.opened_ms
        mfe_bps=p.mfe_pct*100.0
        mae_bps=p.mae_pct*100.0

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
            self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (now,book.exchange_ts_ms,self.session_id,p.head.value,p.symbol,"TAIL_ARM","RIGHT_TAIL",
                             json.dumps({"side":p.side,"bucket":bucket,"mfe_bps":mfe_bps,
                                         "move_bps":move_bps,"flow":current_flow,"imbalance":current_imb},
                                        separators=(",",":"))))

        from_peak_bps=mfe_bps-move_bps
        tail_trail_bps=max(0.30*pol.take_profit_bps,0.26*max(mfe_bps,pol.take_profit_bps))
        # Once armed, do not allow a captured winner to decay back through costs.
        expected_cost=float(ef.get("required_cost_edge_bps",0) or 0)
        tail_profit_floor_bps=max(0.18*pol.take_profit_bps,0.65*expected_cost)
        tail_timeout=pol.max_hold_ms*3

        reason=None
        if move_bps<=-pol.stop_loss_bps:
            reason="STOP_LOSS"
        elif early_loser_cut:
            reason="EARLY_LOSER_CUT"
        elif p.tail_armed and move_bps<=tail_profit_floor_bps:
            reason="RIGHT_TAIL_FLOOR"
        elif p.tail_armed and from_peak_bps>=tail_trail_bps:
            reason="RIGHT_TAIL_TRAIL"
        elif (not p.tail_armed) and move_bps>=pol.take_profit_bps:
            reason="TAKE_PROFIT"
        elif p.tail_armed and age>=tail_timeout:
            reason="RIGHT_TAIL_TIME"
        elif (not p.tail_armed) and age>=pol.max_hold_ms:
            reason="TIME_EXIT"
        if not reason:return

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
        self._ledger("TRADING",p.head.value,p.position_id,"CLOSE",w.trading_cash_usd-before,w.trading_cash_usd,
                     {"reason":reason,"exit_vwap":exit_vwap,"net_pnl":net,
                      "mfe_bps":mfe_bps,"mae_bps":mae_bps,"move_bps":move_bps,
                      "tail_armed":p.tail_armed,"tail_trail_bps":tail_trail_bps,
                      "tail_profit_floor_bps":tail_profit_floor_bps,
                      "early_loser_confirmed":early_loser_cut,"winner_veto":winner_veto})
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
