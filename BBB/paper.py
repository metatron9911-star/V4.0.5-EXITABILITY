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
from strategy_cells import POLICIES, classify_regime, directional_scores, projected_edge_bps, sizing_tier, policies_json

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
        self.sanitizer=Sanitizer(
            cx,train,
            min_n=int(os.getenv("BBB_SANITIZER_MIN_N","12")),
            min_winner_retention=float(os.getenv("BBB_SANITIZER_WINNER_RETENTION","0.90")),
            min_losers_removed=int(os.getenv("BBB_SANITIZER_MIN_LOSERS_REMOVED","2")),
        )
        self.sanitizer_rejects=0
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

    def on_snapshot(self,symbol:str,head:str,f:dict,book:LocalBook,exchange_ts_ms:int,receive_ts_ms:int):
        if not self.enabled or not book.synced or not book.mid:
            return
        p=self._position_for_symbol(symbol)
        if p:
            self._manage_open(p,f,book)
            return

        long_score,short_score=directional_scores(head,f)
        side="BUY" if long_score>=short_score else "SELL"
        score=max(long_score,short_score)
        pol=POLICIES[(head,side)]
        if self._head_count(head)>=pol.max_open:return
        if now_ms()-self.last_close_ms.get(symbol,0)<pol.cooldown_ms:return
        if float(f.get("spread_bps",9999))>pol.max_spread_bps:return
        if int(f.get("trade_count_1s",0))<pol.min_trades_1s:return
        if score<pol.gate:return
        regime=classify_regime(f)
        if regime not in pol.allow_regimes:return
        san_features=dict(f)
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

        tier,size_mult,confidence=sizing_tier(pol,side,score,regime,f,expected_cost,edge)
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
                      "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence},
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
                 "projected_edge_bps":edge,"required_cost_edge_bps":required_cost_edge,
                 "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence},separators=(",",":")),"OPEN",pos.opened_ms))
        self._ledger("TRADING",head,pid,"OPEN_MARGIN",-margin-pos.entry_fee_usd,self.book.wallets.trading_cash_usd,
                     {"side":side,"symbol":symbol,"entry_vwap":entry_vwap,"notional":notional,
                      "sizing_tier":tier,"size_multiplier":size_mult,"confidence":confidence})
        self._write_wallet_meta()

    def _manage_open(self,p,f,book):
        mid=float(book.mid or p.entry_vwap)
        direction=1.0 if p.side=="BUY" else -1.0
        move_bps=direction*(mid/p.entry_vwap-1.0)*10000
        p.mfe_pct=max(p.mfe_pct,move_bps/100)
        p.mae_pct=min(p.mae_pct,move_bps/100)
        pol=POLICIES[(p.head.value,p.side)]
        age=now_ms()-p.opened_ms
        reason=None
        if move_bps>=pol.take_profit_bps:reason="TAKE_PROFIT"
        elif move_bps<=-pol.stop_loss_bps:reason="STOP_LOSS"
        elif age>=pol.max_hold_ms:reason="TIME_EXIT"
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
                     {"reason":reason,"exit_vwap":exit_vwap,"net_pnl":net})
        self.last_close_ms[p.symbol]=now_ms()

        # RHB-style reserve wallet: skim half of trading cash above start+trigger.
        excess=w.trading_cash_usd-(self.start_usd+self.skim_trigger)
        if excess>0 and self.skim_fraction>0:
            skim=excess*self.skim_fraction
            w.skim_profit(skim)
            self._ledger("PROFIT",p.head.value,p.position_id,"PROFIT_SKIM",skim,w.profit_wallet_usd,{"trigger":self.skim_trigger})
        self._write_wallet_meta()
