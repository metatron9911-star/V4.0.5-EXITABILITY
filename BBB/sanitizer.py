"""Adaptive PAPER loser sanitizer for BBB.

Learns only from CLOSED paper trades in explicitly named training sessions.
Goal: improve net expectancy (PESB proxy) while preserving winners.
This module never submits exchange orders.
"""
from __future__ import annotations
import json, math, os, sqlite3
from dataclasses import dataclass, asdict
from statistics import quantiles
from typing import Any

FEATURES = [
    "entry_score","spread_bps","book_imbalance_5","trade_count_1s","trade_count_5s",
    "buy_ratio_1s","ret_250ms_bps","ret_1s_bps","ret_5s_bps","realized_vol_5s_bps",
    "source_age_ms","processing_ms"
]

@dataclass
class Rule:
    head: str
    side: str
    feature: str
    op: str
    threshold: float
    sample_n: int
    winners_before: int
    losers_before: int
    winners_removed: int
    losers_removed: int
    winner_retention: float
    pnl_before: float
    pnl_after: float
    pesb_before: float
    pesb_after: float
    delta_pesb: float

    def blocks(self, row: dict[str,Any]) -> bool:
        v=row.get(self.feature)
        if v is None:
            return False
        try:v=float(v)
        except Exception:return False
        return (v < self.threshold) if self.op=="LT" else (v > self.threshold)

class Sanitizer:
    def __init__(self,cx:sqlite3.Connection,training_sessions:list[str],
                 min_n:int=12,min_winner_retention:float=0.90,min_losers_removed:int=2):
        self.cx=cx
        self.training_sessions=[s for s in training_sessions if s]
        self.min_n=min_n
        self.min_winner_retention=min_winner_retention
        self.min_losers_removed=min_losers_removed
        self.rules:dict[tuple[str,str],list[Rule]]={}
        self.rebuild()
        # Optional frozen rules for clean A/B experiments on an isolated DB.
        # When set, this reproduces the benchmark sanitizer without sharing data.
        raw_static=os.getenv("BBB_SANITIZER_STATIC_RULES_JSON","").strip()
        if raw_static:
            try:
                fixed=json.loads(raw_static)
                rules={}
                for x in fixed:
                    r=Rule(
                        head=str(x["head"]), side=str(x["side"]),
                        feature=str(x["feature"]), op=str(x["op"]),
                        threshold=float(x["threshold"]),
                        sample_n=0,winners_before=0,losers_before=0,
                        winners_removed=0,losers_removed=0,winner_retention=1.0,
                        pnl_before=0.0,pnl_after=0.0,pesb_before=0.0,pesb_after=0.0,delta_pesb=0.0,
                    )
                    rules.setdefault((r.head,r.side),[]).append(r)
                self.rules=rules
            except Exception as e:
                raise RuntimeError(f"invalid BBB_SANITIZER_STATIC_RULES_JSON: {e}")

    def _rows(self):
        if not self.training_sessions:return []
        q=",".join("?" for _ in self.training_sessions)
        rows=self.cx.execute(f"""SELECT head,side,net_pnl_usd,entry_score,source_age_ms,processing_ms,entry_features_json
                                FROM trades WHERE status='CLOSED' AND session_id IN ({q})""",
                             self.training_sessions).fetchall()
        out=[]
        for head,side,pnl,entry_score,src,proc,js in rows:
            try:f=json.loads(js or "{}")
            except Exception:f={}
            f["entry_score"]=entry_score
            f["source_age_ms"]=src
            f["processing_ms"]=proc
            f["_head"]=head; f["_side"]=side; f["_pnl"]=float(pnl or 0)
            out.append(f)
        return out

    @staticmethod
    def _cuts(vals:list[float]) -> list[float]:
        vals=sorted(set(float(v) for v in vals if v is not None and math.isfinite(float(v))))
        if len(vals)<6:return []
        # robust decile-ish cut points, de-duplicated
        idxs={max(1,min(len(vals)-2,int(round((len(vals)-1)*p)))) for p in (0.15,0.25,0.35,0.50,0.65,0.75,0.85)}
        return sorted({vals[i] for i in idxs})

    def rebuild(self):
        rows=self._rows()
        self.rules={}
        groups={}
        for r in rows:
            groups.setdefault((r["_head"],r["_side"]),[]).append(r)
        for key,grp in groups.items():
            if len(grp)<self.min_n:continue
            wins=[r for r in grp if r["_pnl"]>0]; losses=[r for r in grp if r["_pnl"]<=0]
            if not wins or not losses:continue
            base_pnl=sum(r["_pnl"] for r in grp); base_pesb=base_pnl/len(grp)
            cand=[]
            for feat in FEATURES:
                vals=[]
                for r in grp:
                    try:
                        v=float(r.get(feat))
                        if math.isfinite(v): vals.append(v)
                    except Exception: pass
                for cut in self._cuts(vals):
                    for op in ("LT","GT"):
                        blocked=[]
                        for r in grp:
                            try:
                                v=float(r.get(feat))
                                b=(v<cut) if op=="LT" else (v>cut)
                            except Exception:b=False
                            if b:blocked.append(r)
                        kept=[r for r in grp if r not in blocked]
                        if len(kept)<max(8,int(len(grp)*0.5)):continue
                        wr=sum(1 for r in wins if r not in blocked)/len(wins)
                        wrem=sum(1 for r in wins if r in blocked)
                        lrem=sum(1 for r in losses if r in blocked)
                        if wr<self.min_winner_retention or lrem<self.min_losers_removed:continue
                        pnl_after=sum(r["_pnl"] for r in kept)
                        pesb_after=pnl_after/len(kept)
                        delta=pesb_after-base_pesb
                        if delta<=0 or pnl_after<=base_pnl:continue
                        cand.append(Rule(
                            head=key[0],side=key[1],feature=feat,op=op,threshold=cut,
                            sample_n=len(grp),winners_before=len(wins),losers_before=len(losses),
                            winners_removed=wrem,losers_removed=lrem,winner_retention=wr,
                            pnl_before=base_pnl,pnl_after=pnl_after,pesb_before=base_pesb,
                            pesb_after=pesb_after,delta_pesb=delta))
            # keep up to two strongest, with different features, to avoid stacking correlated cuts
            cand.sort(key=lambda r:(r.delta_pesb, r.losers_removed, -r.winners_removed),reverse=True)
            chosen=[]
            for r in cand:
                if any(x.feature==r.feature for x in chosen):continue
                chosen.append(r)
                if len(chosen)>=2:break
            if chosen:self.rules[key]=chosen

    def decision(self,head:str,side:str,features:dict) -> tuple[bool,str|None]:
        rules=self.rules.get((head,side),[])
        for r in rules:
            if r.blocks(features):
                return False,f"{r.feature}_{r.op}_{r.threshold:.6g}"
        return True,None

    def as_json(self) -> str:
        return json.dumps({
            f"{h}:{s}":[asdict(r) for r in rs] for (h,s),rs in self.rules.items()
        },separators=(",",":"))
