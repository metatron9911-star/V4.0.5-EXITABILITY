"""Read-only Pump.fun bonding-curve PAPER quotes. Never signs or submits transactions."""
import math, time, requests

COIN_API='https://frontend-api-v3.pump.fun/coins-v2/{}'
SOL_PRICE_API='https://frontend-api-v3.pump.fun/sol-price'
DEFAULT_PUBKEY='11111111111111111111111111111111'

class PumpCurve:
    def __init__(self):
        self.s=requests.Session(); self._sol=(0.0,0.0)
    def coin(self,mint):
        try:
            r=self.s.get(COIN_API.format(mint),timeout=6); r.raise_for_status(); j=r.json()
            if not isinstance(j,dict) or j.get('mint')!=mint:return None
            return j
        except Exception:return None
    def sol_usd(self):
        now=time.time()
        if self._sol[0]>0 and now-self._sol[1]<10:return self._sol[0]
        try:
            r=self.s.get(SOL_PRICE_API,timeout=5); r.raise_for_status(); p=float(r.json().get('solPrice') or 0)
            if math.isfinite(p) and p>0:self._sol=(p,now); return p
        except Exception:pass
        return self._sol[0] or 0.0
    @staticmethod
    def _reserves(c):
        try:
            vt=int(c.get('virtual_token_reserves') or 0); vq=int(c.get('virtual_sol_reserves') or c.get('virtual_quote_reserves') or 0); rt=int(c.get('real_token_reserves') or 0)
            return vt,vq,rt
        except Exception:return 0,0,0
    def is_sol_curve(self,c):
        q=c.get('quote_mint')
        return not q or q==DEFAULT_PUBKEY or q=='So11111111111111111111111111111111111111112'
    def buy_usd(self,mint,usd,fee_bps=125):
        c=self.coin(mint); solp=self.sol_usd()
        if not c:return {'ok':False,'reason':'PUMP_STATE_UNAVAILABLE'}
        if c.get('complete'):return {'ok':False,'reason':'CURVE_COMPLETE','coin':c}
        if not self.is_sol_curve(c):return {'ok':False,'reason':'CURVE_QUOTE_ASSET_UNSUPPORTED','coin':c}
        vt,vs,rt=self._reserves(c)
        if min(vt,vs,rt)<=0 or solp<=0:return {'ok':False,'reason':'CURVE_RESERVES_UNAVAILABLE','coin':c}
        lamports=max(1,int(float(usd)/solp*1e9)); net=max(0,((lamports-1)*10000)//(10000+int(fee_bps)))
        out=min(rt,(net*vt)//(vs+net))
        if out<=0:return {'ok':False,'reason':'CURVE_QUOTE_ZERO','coin':c}
        return {'ok':True,'venue':'PUMP_CURVE','ts_ms':int(time.time()*1000),'token_raw':out,'token_decimals':6,'input_usd':float(usd),'input_sol':lamports/1e9,'sol_usd':solp,'fee_bps':int(fee_bps),'coin':c}
    def sell_raw(self,mint,raw,fee_bps=125):
        c=self.coin(mint); solp=self.sol_usd()
        if not c:return {'ok':False,'reason':'PUMP_STATE_UNAVAILABLE'}
        if c.get('complete'):return {'ok':False,'reason':'CURVE_COMPLETE','coin':c}
        if not self.is_sol_curve(c):return {'ok':False,'reason':'CURVE_QUOTE_ASSET_UNSUPPORTED','coin':c}
        vt,vs,_=self._reserves(c); raw=int(raw)
        if min(vt,vs,raw)<=0 or solp<=0:return {'ok':False,'reason':'CURVE_RESERVES_UNAVAILABLE','coin':c}
        gross=(raw*vs)//(vt+raw); net=(gross*(10000-int(fee_bps)))//10000
        usd=net/1e9*solp
        return {'ok':usd>=0,'venue':'PUMP_CURVE','ts_ms':int(time.time()*1000),'out_usd':usd,'out_sol':net/1e9,'sol_usd':solp,'fee_bps':int(fee_bps),'coin':c}
