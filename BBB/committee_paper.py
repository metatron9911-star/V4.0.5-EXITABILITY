"""BBB expert committee PAPER trader.

Research/PAPER only. No private Bybit API and no live order submission.

Entry is allowed only after a completed microflow SIGNAL and a separate
multi-agent committee decision. Execution is emulated against the current L50
book using VWAP and Bybit innovation taker fees.
"""
from __future__ import annotations
import json, os, uuid
from typing import Dict
from engine import now_ms, BYBIT_FEES, ProductClass

TP_BPS=float(os.getenv("BBB_COMMITTEE_TP_BPS","80"))
SL_BPS=float(os.getenv("BBB_COMMITTEE_SL_BPS","30"))
TIMEOUT_MS=int(os.getenv("BBB_COMMITTEE_TIMEOUT_MS","20000"))
POSITION_FRACTION=float(os.getenv("BBB_COMMITTEE_POSITION_FRACTION","0.05"))
MAX_OPEN=int(os.getenv("BBB_COMMITTEE_MAX_OPEN","2"))
COOLDOWN_MS=int(os.getenv("BBB_COMMITTEE_COOLDOWN_MS","20000"))

# Evidence-informed V1 thresholds from overnight matched WIN/LOSS review.
IMPULSE_MIN=float(os.getenv("BBB_COMMITTEE_IMPULSE_MIN","0.45"))
ABSORPTION_MIN=float(os.getenv("BBB_COMMITTEE_ABSORPTION_MIN","0.20"))
TAPE_MIN=float(os.getenv("BBB_COMMITTEE_TAPE_MIN","0.30"))
BOOK_MIN=float(os.getenv("BBB_COMMITTEE_BOOK_MIN","-0.05"))
WEIGHTED_MIN=float(os.getenv("BBB_COMMITTEE_WEIGHTED_MIN","0.40"))
AGREEMENT_MIN=float(os.getenv("BBB_COMMITTEE_AGREEMENT_MIN","0.69"))
DISPERSION_MAX=float(os.getenv("BBB_COMMITTEE_DISPERSION_MAX","0.61"))
RET250_MIN=float(os.getenv("BBB_COMMITTEE_RET250_MIN","1.5"))
RET250_MAX=float(os.getenv("BBB_COMMITTEE_RET250_MAX","10.5"))
RET1_MIN=float(os.getenv("BBB_COMMITTEE_RET1_MIN","-2.0"))
RET1_MAX=float(os.getenv("BBB_COMMITTEE_RET1_MAX","14.0"))
SPREAD_MAX=float(os.getenv("BBB_COMMITTEE_SPREAD_MAX","7.0"))
IMB_MIN=float(os.getenv("BBB_COMMITTEE_IMB_MIN","0.15"))
ALLOWED_REGIMES={"ACTIVE","QUIET"}

class ExpertCommitteePaper:
    def __init__(self,cx,session_id,start_usd=500.0):
        self.cx=cx; self.session_id=session_id
        self.start_usd=float(start_usd)
        self.cash=self.start_usd
        self.realized=0.0
        self.fees=0.0
        self.positions:Dict[str,dict]={}
        self.cooldown_until:Dict[str,int]={}
        self.signals=0; self.approved=0; self.rejected=0
        self._meta()

    def _meta(self):
        vals={
          "active_strategy":"COMMITTEE_V2",
          "committee_paper_enabled":"1",
          "committee_cash_usd":f"{self.cash:.8f}",
          "committee_realized_usd":f"{self.realized:.8f}",
          "committee_fees_usd":f"{self.fees:.8f}",
          "committee_open_positions":str(len(self.positions)),
          "committee_signals":str(self.signals),
          "committee_approved":str(self.approved),
          "committee_rejected":str(self.rejected),
          "committee_selector":(
            f"REGIME ACTIVE|QUIET; VETO=0; IMPULSE>={IMPULSE_MIN}; "
            f"ABSORPTION>={ABSORPTION_MIN}; TAPE>={TAPE_MIN}; "
            f"BOOK>={BOOK_MIN}; WEIGHTED>={WEIGHTED_MIN}; AGREE>={AGREEMENT_MIN}; "
            f"DISP<={DISPERSION_MAX}; RET250={RET250_MIN}..{RET250_MAX}; "
            f"RET1={RET1_MIN}..{RET1_MAX}; SPREAD<={SPREAD_MAX}; IMB>={IMB_MIN}"
          ),
          "committee_exit":f"TP{TP_BPS:g}bps_STOP{SL_BPS:g}bps_TIMEOUT{TIMEOUT_MS/1000:g}s",
          "committee_execution_mode":"LIVE_WS_L50_VWAP_PAPER",
          "live_submission_enabled":"0",
        }
        for k,v in vals.items():
            self.cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",(k,str(v)))

    @staticmethod
    def _vote(council,name):
        try:return float((council.get("votes") or {}).get(name,{}).get("score") or 0.0)
        except Exception:return 0.0

    def decision(self,council):
        regime=((council.get("regime") or {}).get("regime")
                if isinstance(council.get("regime"),dict) else council.get("regime"))
        veto=bool((council.get("risk_veto") or {}).get("veto"))
        impulse=self._vote(council,"IMPULSE")
        absorption=self._vote(council,"ABSORPTION")
        tape=self._vote(council,"TAPE")
        book=self._vote(council,"BOOK")
        weighted=.40*impulse+.30*absorption+.20*tape+.10*book
        frame=council.get("frame") or {}
        agreement=float(council.get("agreement") or 0.0)
        dispersion=float(council.get("dispersion") or 99.0)
        ret250=float(frame.get("ret250") or 0.0)
        ret1=float(frame.get("ret1") or 0.0)
        spread=float(frame.get("spread") or 999.0)
        imb=float(frame.get("imb") or 0.0)
        checks={
          "REGIME":regime in ALLOWED_REGIMES,
          "NO_VETO":not veto,
          "IMPULSE":impulse>=IMPULSE_MIN,
          "ABSORPTION":absorption>=ABSORPTION_MIN,
          "TAPE":tape>=TAPE_MIN,
          "BOOK":book>=BOOK_MIN,
          "WEIGHTED":weighted>=WEIGHTED_MIN,
          "AGREEMENT":agreement>=AGREEMENT_MIN,
          "DISPERSION":dispersion<=DISPERSION_MAX,
          "RET250_WINDOW":RET250_MIN<=ret250<=RET250_MAX,
          "RET1_WINDOW":RET1_MIN<=ret1<=RET1_MAX,
          "SPREAD":spread<=SPREAD_MAX,
          "IMBALANCE":imb>=IMB_MIN,
        }
        return all(checks.values()),{
          "regime":regime,"veto":veto,"impulse":impulse,"absorption":absorption,
          "tape":tape,"book":book,"weighted":weighted,"checks":checks,
          "coordinator_score":council.get("coordinator_score"),
          "agreement":agreement,"dispersion":dispersion,
          "ret250":ret250,"ret1":ret1,"spread":spread,"imb":imb
        }

    def on_microflow_signal(self,symbol,f,book,exchange_ts_ms,council):
        self.signals+=1
        ok,detail=self.decision(council)
        now=now_ms()
        if symbol in self.positions or len(self.positions)>=MAX_OPEN or now<self.cooldown_until.get(symbol,0):
            ok=False
            detail["capacity_block"]=True
        if not ok:
            self.rejected+=1; self._meta(); return False
        notional=min(self.start_usd*POSITION_FRACTION,self.cash*0.95)
        if notional<1:
            self.rejected+=1; self._meta(); return False
        vwap,filled=book.walk_vwap("BUY",notional)
        if not vwap or filled<notional*.999:
            self.rejected+=1; self._meta(); return False
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        fee=notional*fee_bps/10000.0
        if self.cash<notional+fee:
            self.rejected+=1; self._meta(); return False
        pid=str(uuid.uuid4())
        self.cash-=notional+fee; self.fees+=fee
        self.positions[symbol]={
          "position_id":pid,"entry_vwap":float(vwap),"notional":notional,
          "entry_fee":fee,"opened_ms":now,"detail":detail
        }
        features={"committee_v1":detail,"microflow":{
          "mid":f.get("mid"),"ret250":f.get("ret250"),"ret1":f.get("ret1"),
          "flow250":f.get("250_flow"),"flow1s":f.get("1s_flow"),
          "imb":f.get("imb"),"spread":f.get("spread")
        }}
        self.cx.execute("""INSERT INTO trades(
          session_id,position_id,head,venue,product_class,symbol,side,leverage,margin_usd,notional_usd,
          signal_exchange_ts_ms,receive_ts_ms,signal_ts_ms,order_ts_ms,fill_ts_ms,
          signal_mid,signal_bid,signal_ask,expected_entry_vwap,entry_vwap,entry_fee_usd,
          source_age_ms,processing_ms,signal_to_fill_ms,entry_score,entry_gate,risk_state,
          entry_features_json,status,opened_ms)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (self.session_id,pid,"VOL","BYBIT","INNOVATION",symbol,"BUY",1.0,notional,notional,
           int(exchange_ts_ms or 0),now,now,now,now,float(f.get("mid") or vwap),
           float(f.get("bid") or vwap),float(f.get("ask") or vwap),float(vwap),float(vwap),fee,
           0,0,0,float(detail["weighted"]),WEIGHTED_MIN,"COMMITTEE_V2",
           json.dumps(features,separators=(",",":")),"OPEN",now))
        self.approved+=1; self._meta()
        return True

    def _close(self,symbol,reason,book):
        p=self.positions.get(symbol)
        if not p:return
        vwap,filled=book.walk_vwap("SELL",float(p["notional"]))
        if not vwap or filled<float(p["notional"])*.999:return
        p=self.positions.pop(symbol)
        gross=float(p["notional"])*(float(vwap)/float(p["entry_vwap"])-1.0)
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        exit_fee=float(p["notional"])*fee_bps/10000.0
        net=gross-float(p["entry_fee"])-exit_fee
        self.cash+=float(p["notional"])+gross-exit_fee
        self.realized+=net; self.fees+=exit_fee
        hold=max(0,now_ms()-int(p["opened_ms"]))
        self.cx.execute("""UPDATE trades SET status='CLOSED',exit_vwap=?,exit_fee_usd=?,
                          gross_pnl_usd=?,net_pnl_usd=?,exit_reason=?,hold_ms=?,closed_ms=?
                          WHERE session_id=? AND position_id=?""",
                        (float(vwap),exit_fee,gross,net,reason,hold,now_ms(),
                         self.session_id,p["position_id"]))
        if net<0:self.cooldown_until[symbol]=now_ms()+COOLDOWN_MS
        self._meta()

    def on_tick(self,symbol,book):
        p=self.positions.get(symbol)
        if not p or not book.mid:return
        move=(float(book.mid)/float(p["entry_vwap"])-1.0)*10000.0
        age=now_ms()-int(p["opened_ms"])
        if move<=-SL_BPS:self._close(symbol,"COMMITTEE_STOP",book)
        elif move>=TP_BPS:self._close(symbol,"COMMITTEE_TP",book)
        elif age>=TIMEOUT_MS:self._close(symbol,"COMMITTEE_TIMEOUT",book)
