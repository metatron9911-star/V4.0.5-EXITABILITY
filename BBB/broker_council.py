"""BBB electronic broker council — SHADOW only.

Independent agents inspect the same 250ms microflow frame from different views.
Coordinator applies regime-dependent weights and an independent veto.
No trading or order placement exists in this module.
"""
from __future__ import annotations
from collections import defaultdict
from storage_guard import enabled
import json, math

def clamp(x,a=-1.0,b=1.0):
    return max(a,min(b,float(x)))

def posz(z,scale=3.0):
    return clamp(max(0.0,float(z or 0.0))/scale,0.0,1.0)

def signedz(z,scale=3.0):
    return clamp(float(z or 0.0)/scale,-1.0,1.0)

class TapeAgent:
    name="TAPE"
    def evaluate(self,f,s):
        flow=signedz(s.get("flow_turn_z"))
        accel=signedz(s.get("flow_rec_z"))
        activity=posz(s.get("activity_z"))
        run=0.15 if f.get("250_run_side")=="BUY" else (-0.15 if f.get("250_run_side")=="SELL" else 0)
        score=clamp(.38*flow+.32*accel+.20*activity+run)
        conf=clamp((abs(flow)+abs(accel)+activity)/3,0,1)
        return {"score":score,"confidence":conf,
          "evidence":{"flow250":f.get("250_flow"),"flow1s":f.get("1s_flow"),
                      "activity_z":s.get("activity_z"),"run_side":f.get("250_run_side")}}

class BookAgent:
    name="BOOK"
    def evaluate(self,f,s):
        imb=signedz(s.get("imb_rec_z"))
        bid=signedz(s.get("bid_hold_z"))
        ask=signedz(s.get("ask_take_z"))
        spread_pen=-posz(s.get("spread_z"))
        score=clamp(.36*imb+.30*bid+.24*ask+.10*spread_pen)
        conf=clamp((abs(imb)+abs(bid)+abs(ask))/3,0,1)
        return {"score":score,"confidence":conf,
          "evidence":{"imbalance":f.get("imb"),"imb_d250":f.get("imb_d250"),
                      "bid5_d250":f.get("bid5_d250"),"ask5_d250":f.get("ask5_d250"),
                      "spread":f.get("spread")}}

class ImpulseAgent:
    name="IMPULSE"
    def evaluate(self,f,s):
        ret=signedz(s.get("ret_turn_z"))
        ease=signedz(s.get("impact_ease_z"))
        r250=float(f.get("ret250") or 0); r1=float(f.get("ret1") or 0)
        persistence=0.20 if r250>0 and r1>=0 else (-0.20 if r250<0 and r1<0 else 0.0)
        score=clamp(.45*ret+.35*ease+persistence)
        conf=clamp((abs(ret)+abs(ease))/2,0,1)
        return {"score":score,"confidence":conf,
          "evidence":{"ret250":r250,"ret1":r1,"ret_turn_z":s.get("ret_turn_z"),
                      "impact_ease_z":s.get("impact_ease_z")}}

class AbsorptionAgent:
    name="ABSORPTION"
    def evaluate(self,f,s):
        bid=signedz(s.get("bid_hold_z"))
        flow=signedz(s.get("flow_rec_z"))
        ease=signedz(s.get("impact_ease_z"))
        sell=posz(s.get("sell_z"))
        # absorption is bullish only when prior sell pressure existed
        score=clamp((.34*bid+.34*flow+.32*ease)*(0.45+0.55*sell))
        conf=clamp((abs(bid)+abs(flow)+abs(ease)+sell)/4,0,1)
        return {"score":score,"confidence":conf,
          "evidence":{"sell_z":s.get("sell_z"),"bid_hold_z":s.get("bid_hold_z"),
                      "flow_rec_z":s.get("flow_rec_z"),"impact_ease_z":s.get("impact_ease_z")}}

class RegimeAgent:
    name="REGIME"
    def evaluate(self,f,s):
        activity=float(s.get("activity_z") or 0)
        spread=float(s.get("spread_z") or 0)
        down=float(s.get("down_z") or 0)
        if activity>=2.0 and abs(float(f.get("ret1") or 0))>=3:
            regime="FAST"
        elif activity>=.75:
            regime="ACTIVE"
        else:
            regime="QUIET"
        if spread>=2.5: regime="DISLOCATED"
        weights={
          "FAST":{"TAPE":1.25,"BOOK":1.15,"IMPULSE":1.20,"ABSORPTION":1.00},
          "ACTIVE":{"TAPE":1.10,"BOOK":1.10,"IMPULSE":1.00,"ABSORPTION":1.10},
          "QUIET":{"TAPE":.80,"BOOK":1.20,"IMPULSE":.80,"ABSORPTION":1.20},
          "DISLOCATED":{"TAPE":.70,"BOOK":.60,"IMPULSE":.70,"ABSORPTION":.70},
        }[regime]
        return {"regime":regime,"weights":weights,
                "evidence":{"activity_z":activity,"spread_z":spread,"down_z":down}}

class RiskVetoAgent:
    name="RISK_VETO"
    def evaluate(self,f,s):
        reasons=[]
        if float(s.get("spread_z") or 0)>=2.5:reasons.append("SPREAD_DISLOCATION")
        if float(s.get("sell_z") or 0)>=2.3 and float(f.get("ret250") or 0)<0:
            reasons.append("RENEWED_SELL_BURST")
        if float(s.get("down_z") or 0)>=2.0 and float(f.get("250_flow") or 0)<-.20:
            reasons.append("DOWNSIDE_NOT_EXHAUSTED")
        if float(s.get("bid_hold_z") or 0)<-1.5:
            reasons.append("BID_LIQUIDITY_WITHDRAWAL")
        veto=bool(reasons)
        severity=clamp(len(reasons)/3,0,1)
        return {"veto":veto,"severity":severity,"reasons":reasons}

class BrokerCouncil:
    def __init__(self,cx,session_id):
        self.cx=cx; self.session_id=session_id
        self.agents=[TapeAgent(),BookAgent(),ImpulseAgent(),AbsorptionAgent()]
        self.regime_agent=RegimeAgent(); self.veto_agent=RiskVetoAgent()
        self.last_persist=defaultdict(int)

    def evaluate(self,symbol,ts_ms,f,scores,stage):
        regime=self.regime_agent.evaluate(f,scores)
        votes={}
        numerator=denominator=0.0
        for a in self.agents:
            v=a.evaluate(f,scores); votes[a.name]=v
            w=float(regime["weights"].get(a.name,1.0))*max(.20,float(v["confidence"]))
            numerator+=w*float(v["score"]); denominator+=w
        consensus=numerator/denominator if denominator else 0.0
        veto=self.veto_agent.evaluate(f,scores)
        # disagreement lowers effective conviction even without a hard veto
        raw=[float(v["score"]) for v in votes.values()]
        dispersion=(max(raw)-min(raw)) if raw else 0.0
        agreement=max(0.0,1.0-dispersion/2.0)
        coordinator=clamp(consensus*(.55+.45*agreement))
        if veto["veto"]:
            decision="VETO"
        elif coordinator>=.35 and sum(1 for v in raw if v>0)>=3:
            decision="SUPPORT"
        elif coordinator<=-.25:
            decision="REJECT"
        else:
            decision="WAIT"
        payload={
          "stage":stage,"regime":regime,"votes":votes,"risk_veto":veto,
          "consensus":consensus,"agreement":agreement,"dispersion":dispersion,
          "coordinator_score":coordinator,"decision":decision,
          "frame":{"ret250":f.get("ret250"),"ret1":f.get("ret1"),"flow250":f.get("250_flow"),
                   "flow1s":f.get("1s_flow"),"imb":f.get("imb"),"spread":f.get("spread")}
        }
        if enabled("BBB_RESEARCH_PERSIST_ENABLED"):
            key=(symbol,decision)
            last=self.last_persist[key]
            important=stage!="IDLE" and decision in {"SUPPORT","VETO","REJECT"}
            if important and int(ts_ms)-last>=2000:
                compact={
                  "stage":stage,"regime":regime["regime"],"weights":regime["weights"],
                  "votes":{k:{"score":v["score"],"confidence":v["confidence"]} for k,v in votes.items()},
                  "risk_veto":veto,"consensus":consensus,"agreement":agreement,
                  "dispersion":dispersion,"coordinator_score":coordinator,
                  "frame":payload["frame"]
                }
                self.cx.execute("""INSERT INTO agent_council_decisions(
                  session_id,symbol,ts_ms,regime,coordinator_score,decision,veto,payload_json)
                  VALUES(?,?,?,?,?,?,?,?)""",
                  (self.session_id,symbol,int(ts_ms),regime["regime"],coordinator,decision,
                   1 if veto["veto"] else 0,json.dumps(compact,separators=(",",":"))))
                self.last_persist[key]=int(ts_ms)

        return payload

