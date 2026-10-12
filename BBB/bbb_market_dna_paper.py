"""BBB MARKET-DNA V1 PAPER emulator.

Fresh BUY selector derived from BBB's own retained Bybit history.
No legacy ROCKET breakout/level/touch/quorum filters are used.
"""
from __future__ import annotations
from collections import defaultdict, deque
import json, uuid

from engine import now_ms, BYBIT_FEES, ProductClass

T5_MIN=203.0
ACTIVITY_MIN=117.0219576116804
D10_T5_MIN=63.0
TP_BPS=80.0
STOP_BPS=30.0
TIMEOUT_MS=20_000
POSITION_FRACTION=0.02
POSITION_MULTIPLIER=3.0
MAX_OPEN_POSITIONS=2
COOLDOWN_MS=20_000

class MarketDnaPaper:
    def __init__(self,cx,session_id,start_usd):
        self.cx=cx; self.session_id=session_id; self.start_usd=float(start_usd)
        self.cash=self.start_usd; self.realized=0.0; self.fees=0.0
        self.positions={}; self.cooldown_until={}
        self.hist=defaultdict(lambda:deque(maxlen=40))
        self.signals=0; self.rejected=0
        self._meta()

    def _set(self,k,v):
        self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,str(v)))

    def _meta(self):
        vals={
          "market_dna_paper_enabled":"1","market_dna_cash_usd":f"{self.cash:.8f}",
          "market_dna_realized_usd":f"{self.realized:.8f}","market_dna_fees_usd":f"{self.fees:.8f}",
          "market_dna_open_positions":len(self.positions),"market_dna_signals":self.signals,
          "market_dna_rejected":self.rejected,"market_dna_selector":"T5>=203 & ACTIVITY>=117.02 & D10_T5>=63",
          "market_dna_exit":"TP80bps_STOP30bps_TIMEOUT20s","active_strategy":"MARKET_DNA_V1"
        }
        for k,v in vals.items():self._set(k,v)

    def _event(self,symbol,stage,reason,payload,exchange_ts_ms=0):
        self.cx.execute("""INSERT INTO events(ts_ms,exchange_ts_ms,session_id,head,symbol,stage,reason,payload_json)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (now_ms(),int(exchange_ts_ms or 0),self.session_id,"VOL",symbol,stage,reason,
                         json.dumps(payload,separators=(",",":"))))

    def _d10_t5(self,symbol,now,t5):
        h=self.hist[symbol]
        ref=None
        for x in h:
            if x["ts"]<=now-10_000:
                ref=x
        if ref is None:return None
        return float(t5)-float(ref["t5"])

    def _open(self,symbol,f,book,exchange_ts_ms,d10):
        if symbol in self.positions or len(self.positions)>=MAX_OPEN_POSITIONS:return False
        notional=min(self.start_usd*POSITION_FRACTION*POSITION_MULTIPLIER,self.cash)
        if notional<1:return False
        vwap,filled=book.walk_vwap("BUY",notional)
        if not vwap or filled<notional*.999:return False
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        fee=notional*fee_bps/10000.0
        if self.cash<notional+fee:return False
        ts=now_ms();pid=str(uuid.uuid4())
        self.cash-=notional+fee;self.fees+=fee
        p={"position_id":pid,"entry_vwap":float(vwap),"notional":notional,
           "entry_fee":fee,"opened_ms":ts}
        self.positions[symbol]=p
        features=dict(f)
        features["market_dna_v1"]={"t5":float(f.get("trade_count_5s",0) or 0),
            "activity":float(f.get("live_activity_score",0) or 0),"d10_t5":float(d10),
            "thresholds":{"t5":T5_MIN,"activity":ACTIVITY_MIN,"d10_t5":D10_T5_MIN}}
        self.cx.execute("""INSERT INTO trades(
          session_id,position_id,head,venue,product_class,symbol,side,leverage,margin_usd,notional_usd,
          signal_exchange_ts_ms,receive_ts_ms,signal_ts_ms,order_ts_ms,fill_ts_ms,
          signal_mid,signal_bid,signal_ask,expected_entry_vwap,entry_vwap,entry_fee_usd,
          source_age_ms,processing_ms,signal_to_fill_ms,entry_score,entry_gate,risk_state,
          entry_features_json,status,opened_ms)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (self.session_id,pid,"VOL","BYBIT","INNOVATION",symbol,"BUY",1.0,notional,notional,
           int(exchange_ts_ms or 0),ts,ts,ts,ts,float(f.get("mid") or vwap),
           float(f.get("bid") or vwap),float(f.get("ask") or vwap),float(vwap),float(vwap),fee,
           0,0,0,0,0,"MARKET_DNA_V1",json.dumps(features,separators=(",",":")),"OPEN",ts))
        self._event(symbol,"MARKET_DNA_FILL","ENTRY",{"vwap":vwap,"notional":notional,
                    "t5":f.get("trade_count_5s"),"activity":f.get("live_activity_score"),"d10_t5":d10},exchange_ts_ms)
        self._meta();return True

    def _close(self,symbol,reason,book,exchange_ts_ms):
        p=self.positions.get(symbol)
        if not p:return
        vwap,filled=book.walk_vwap("SELL",float(p["notional"]))
        if not vwap or filled<float(p["notional"])*.999:return
        p=self.positions.pop(symbol)
        gross_ret=float(vwap)/float(p["entry_vwap"])-1.0
        gross=float(p["notional"])*gross_ret
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        exit_fee=float(p["notional"])*fee_bps/10000.0
        net=gross-float(p["entry_fee"])-exit_fee
        self.cash+=float(p["notional"])+gross-exit_fee
        self.realized+=net;self.fees+=exit_fee
        hold=max(0,now_ms()-int(p["opened_ms"]))
        self.cx.execute("""UPDATE trades SET status='CLOSED',exit_vwap=?,exit_fee_usd=?,gross_pnl_usd=?,
                           net_pnl_usd=?,exit_reason=?,hold_ms=?,closed_ms=?
                           WHERE session_id=? AND position_id=?""",
                        (float(vwap),exit_fee,gross,net,reason,hold,now_ms(),self.session_id,p["position_id"]))
        self._event(symbol,"MARKET_DNA_FILL","EXIT",{"reason":reason,"vwap":vwap,
                    "gross_pnl_usd":gross,"net_pnl_usd":net,"hold_ms":hold},exchange_ts_ms)
        self.cooldown_until[symbol]=now_ms()+COOLDOWN_MS
        self._meta()

    def _manage(self,symbol,book,exchange_ts_ms):
        p=self.positions.get(symbol)
        if not p:return
        vwap,filled=book.walk_vwap("SELL",float(p["notional"]))
        if not vwap or filled<float(p["notional"])*.999:return
        move=(float(vwap)/float(p["entry_vwap"])-1.0)*10000.0
        age=now_ms()-int(p["opened_ms"])
        if move>=TP_BPS:self._close(symbol,"MARKET_DNA_TP",book,exchange_ts_ms)
        elif move<=-STOP_BPS:self._close(symbol,"MARKET_DNA_STOP",book,exchange_ts_ms)
        elif age>=TIMEOUT_MS:self._close(symbol,"MARKET_DNA_TIMEOUT",book,exchange_ts_ms)

    def on_snapshot(self,symbol,f,book,exchange_ts_ms=0):
        if not book.synced or not book.mid:return
        self._manage(symbol,book,exchange_ts_ms)
        now=now_ms(); t5=float(f.get("trade_count_5s",0) or 0)
        d10=self._d10_t5(symbol,now,t5)
        activity=float(f.get("live_activity_score",0) or 0)
        self.hist[symbol].append({"ts":now,"t5":t5,"activity":activity})
        if symbol in self.positions:return
        if now<self.cooldown_until.get(symbol,0):return
        if d10 is None:return
        checks={"T5":t5>=T5_MIN,"ACTIVITY":activity>=ACTIVITY_MIN,"D10_T5":d10>=D10_T5_MIN}
        if all(checks.values()):
            self.signals+=1
            self._event(symbol,"MARKET_DNA_SIGNAL","WIN_PORTRAIT_3_OF_3",
                        {"t5":t5,"activity":activity,"d10_t5":d10,"checks":checks},exchange_ts_ms)
            if not self._open(symbol,f,book,exchange_ts_ms,d10):
                self.rejected+=1
                self._event(symbol,"MARKET_DNA_REJECT","ENTRY_BLOCKED",
                            {"t5":t5,"activity":activity,"d10_t5":d10},exchange_ts_ms)
            self._meta()
