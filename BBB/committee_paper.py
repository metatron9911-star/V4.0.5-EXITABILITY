"""BBB expert committee PAPER trader.

Two frozen research profiles:
- Variant 1: strict forward-test profile.
- Variant 2: historical WIN-envelope experiment. On the original 37 V1 trades,
  AGREEMENT window + FLOW1S window retained 5/5 WIN and 3/32 LOSS.

PAPER only. No private Bybit API and no live order submission.
"""
from __future__ import annotations
import json, os, uuid
from typing import Dict
from engine import now_ms, BYBIT_FEES, ProductClass

TP_BPS=float(os.getenv("BBB_COMMITTEE_TP_BPS","80"))
SL_BPS=float(os.getenv("BBB_COMMITTEE_SL_BPS","30"))
TIMEOUT_MS=int(os.getenv("BBB_COMMITTEE_TIMEOUT_MS","20000"))
POSITION_FRACTION=float(os.getenv("BBB_COMMITTEE_POSITION_FRACTION","0.20"))
MAX_OPEN=int(os.getenv("BBB_COMMITTEE_MAX_OPEN","2"))
COOLDOWN_MS=int(os.getenv("BBB_COMMITTEE_COOLDOWN_MS","20000"))
VARIANT=str(os.getenv("BBB_COMMITTEE_VARIANT","1")).strip()
ALLOWED_REGIMES={"ACTIVE","QUIET"}

VARIANT_1={
  "impulse_min":0.48,"absorption_min":0.20,"tape_min":0.32,"book_min":0.25,
  "weighted_min":0.42,"agreement_min":0.69,"dispersion_max":0.61,
  "ret250_min":1.5,"ret250_max":10.5,"ret1_min":-2.0,"ret1_max":14.0,
  "spread_max":7.0,"imb_min":0.15,
}

VARIANT_2={
  "impulse_min":0.45,"absorption_min":0.20,"tape_min":0.30,"book_min":-0.05,
  "weighted_min":0.40,
  "agreement_lo":0.6983031332207035,
  "agreement_hi":0.8677086708817653,
  "flow1s_lo":-0.2277099099360245,
  "flow1s_hi":1.0,
}

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
        self.variant="2" if VARIANT=="2" else "1"
        self.strategy_name=f"COMMITTEE_VARIANT_{self.variant}"
        self._meta()

    def _selector_text(self):
        if self.variant=="2":
            p=VARIANT_2
            return (
              "VARIANT2 HISTORICAL_5WIN_3LOSS; REGIME ACTIVE|QUIET; VETO=0; "
              f"IMPULSE>={p['impulse_min']}; ABSORPTION>={p['absorption_min']}; "
              f"TAPE>={p['tape_min']}; BOOK>={p['book_min']}; WEIGHTED>={p['weighted_min']}; "
              f"AGREEMENT={p['agreement_lo']:.6f}..{p['agreement_hi']:.6f}; "
              f"FLOW1S={p['flow1s_lo']:.6f}..{p['flow1s_hi']:.1f}"
            )
        p=VARIANT_1
        return (
          "VARIANT1 STRICT_FORWARD; REGIME ACTIVE|QUIET; VETO=0; "
          f"IMPULSE>={p['impulse_min']}; ABSORPTION>={p['absorption_min']}; "
          f"TAPE>={p['tape_min']}; BOOK>={p['book_min']}; WEIGHTED>={p['weighted_min']}; "
          f"AGREE>={p['agreement_min']}; DISP<={p['dispersion_max']}; "
          f"RET250={p['ret250_min']}..{p['ret250_max']}; RET1={p['ret1_min']}..{p['ret1_max']}; "
          f"SPREAD<={p['spread_max']}; IMB>={p['imb_min']}"
        )

    def _meta(self):
        vals={
          "active_strategy":self.strategy_name,
          "committee_variant":self.variant,
          "committee_paper_enabled":"1",
          "committee_cash_usd":f"{self.cash:.8f}",
          "committee_start_usd":f"{self.start_usd:.8f}",
          "committee_position_fraction":f"{POSITION_FRACTION:.6f}",
          "committee_stake_basis":"CURRENT_WORKING_CASH",
          "committee_realized_usd":f"{self.realized:.8f}",
          "committee_fees_usd":f"{self.fees:.8f}",
          "committee_open_positions":str(len(self.positions)),
          "committee_signals":str(self.signals),
          "committee_approved":str(self.approved),
          "committee_rejected":str(self.rejected),
          "committee_selector":self._selector_text(),
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
        flow1s=float(frame.get("flow1s") or 0.0)
        spread=float(frame.get("spread") or 999.0)
        imb=float(frame.get("imb") or 0.0)

        if self.variant=="2":
            p=VARIANT_2
            checks={
              "REGIME":regime in ALLOWED_REGIMES,
              "NO_VETO":not veto,
              "IMPULSE":impulse>=p["impulse_min"],
              "ABSORPTION":absorption>=p["absorption_min"],
              "TAPE":tape>=p["tape_min"],
              "BOOK":book>=p["book_min"],
              "WEIGHTED":weighted>=p["weighted_min"],
              "AGREEMENT_ENVELOPE":p["agreement_lo"]<=agreement<=p["agreement_hi"],
              "FLOW1S_ENVELOPE":p["flow1s_lo"]<=flow1s<=p["flow1s_hi"],
            }
        else:
            p=VARIANT_1
            checks={
              "REGIME":regime in ALLOWED_REGIMES,
              "NO_VETO":not veto,
              "IMPULSE":impulse>=p["impulse_min"],
              "ABSORPTION":absorption>=p["absorption_min"],
              "TAPE":tape>=p["tape_min"],
              "BOOK":book>=p["book_min"],
              "WEIGHTED":weighted>=p["weighted_min"],
              "AGREEMENT":agreement>=p["agreement_min"],
              "DISPERSION":dispersion<=p["dispersion_max"],
              "RET250_WINDOW":p["ret250_min"]<=ret250<=p["ret250_max"],
              "RET1_WINDOW":p["ret1_min"]<=ret1<=p["ret1_max"],
              "SPREAD":spread<=p["spread_max"],
              "IMBALANCE":imb>=p["imb_min"],
            }

        return all(checks.values()),{
          "variant":self.variant,"regime":regime,"veto":veto,
          "impulse":impulse,"absorption":absorption,"tape":tape,"book":book,
          "weighted":weighted,"checks":checks,
          "coordinator_score":council.get("coordinator_score"),
          "agreement":agreement,"dispersion":dispersion,
          "ret250":ret250,"ret1":ret1,"flow1s":flow1s,"spread":spread,"imb":imb
        }

    def on_microflow_signal(self,symbol,f,book,exchange_ts_ms,council):
        self.signals+=1
        ok,detail=self.decision(council)
        now=now_ms()
        if symbol in self.positions or len(self.positions)>=MAX_OPEN or now<self.cooldown_until.get(symbol,0):
            ok=False
            detail["capacity_block"]=True
        if not ok:
            self.rejected+=1; self._meta()
            return {"approved":False,"detail":detail,"strategy":self.strategy_name}

        notional=self.cash*POSITION_FRACTION
        if notional<1:
            self.rejected+=1; self._meta()
            detail["execution_block"]="NOTIONAL_LT_1"
            return {"approved":False,"detail":detail,"strategy":self.strategy_name}
        vwap,filled=book.walk_vwap("BUY",notional)
        if not vwap or filled<notional*.999:
            self.rejected+=1; self._meta()
            detail["execution_block"]="INSUFFICIENT_L50"
            return {"approved":False,"detail":detail,"strategy":self.strategy_name}
        fee_bps=BYBIT_FEES[ProductClass.INNOVATION].taker_bps
        fee=notional*fee_bps/10000.0
        if self.cash<notional+fee:
            self.rejected+=1; self._meta()
            detail["execution_block"]="INSUFFICIENT_CASH"
            return {"approved":False,"detail":detail,"strategy":self.strategy_name}
        pid=str(uuid.uuid4())
        self.cash-=notional+fee; self.fees+=fee
        self.positions[symbol]={
          "position_id":pid,"entry_vwap":float(vwap),"notional":notional,
          "entry_fee":fee,"opened_ms":now,"detail":detail
        }
        features={"committee":detail,"committee_v1":detail,"microflow":{
          "mid":f.get("mid"),"ret250":f.get("ret250"),"ret1":f.get("ret1"),
          "flow250":f.get("250_flow"),"flow1s":f.get("1s_flow"),
          "imb":f.get("imb"),"spread":f.get("spread")
        }}
        gate=VARIANT_2["weighted_min"] if self.variant=="2" else VARIANT_1["weighted_min"]
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
           0,0,0,float(detail["weighted"]),gate,self.strategy_name,
           json.dumps(features,separators=(",",":")),"OPEN",now))
        self.approved+=1; self._meta()
        return {"approved":True,"detail":detail,"strategy":self.strategy_name,
                "paper_entry_vwap":float(vwap),"paper_notional":float(notional)}

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
