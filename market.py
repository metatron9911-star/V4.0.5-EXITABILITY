"""Read-only market APIs. No signing or transaction submission."""
import os, time, math
from decimal import Decimal
from typing import Dict, Any, Optional, List, Tuple
import requests
from engine import CFG, USDC_MINT, now_ms

class MarketData:
    def __init__(self):
        self.birdeye_key = os.getenv("BIRDEYE_API_KEY", "").strip()
        self.helius_key = os.getenv("HELIUS_API_KEY", "").strip()
        self.jupiter_key = os.getenv("JUPITER_API_KEY", "").strip()
        self.s = requests.Session()
        self.last_birdeye = {"ok": False, "status": "NOT TESTED", "ts_ms": 0, "items": 0, "detail": ""}
        self.last_dex = {"ok": False, "status": "NOT TESTED", "ts_ms": 0, "items": 0, "detail": ""}
        self._last_birdeye_poll_ms = 0

    def birdeye_new_listings(self, force=False) -> List[Dict[str, Any]]:
        interval = int(os.getenv("BIRDEYE_POLL_INTERVAL_SEC", CFG.get("birdeye_poll_interval_sec", 1800)))
        if not force and self._last_birdeye_poll_ms and now_ms() - self._last_birdeye_poll_ms < interval * 1000:
            return []
        self._last_birdeye_poll_ms = now_ms()
        if not self.birdeye_key:
            self.last_birdeye = {"ok": False, "status": "NO KEY", "ts_ms": now_ms(), "items": 0, "detail": "BIRDEYE_API_KEY missing"}
            return []
        try:
            url = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"
            r = self.s.get(url, headers={"X-API-KEY": self.birdeye_key, "x-chain": "solana"},
                           params={"limit": 20, "meme_platform_enabled": "true"}, timeout=10)
            r.raise_for_status(); j = r.json(); data = j.get("data", {})
            items = data.get("items") or data.get("tokens") or data.get("data") or []
            self.last_birdeye = {"ok": True, "status": f"HTTP {r.status_code}", "ts_ms": now_ms(), "items": len(items), "detail": "new_listing"}
            return items
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            self.last_birdeye = {"ok": False, "status": f"HTTP {code}" if code else "ERROR", "ts_ms": now_ms(), "items": 0, "detail": type(exc).__name__}
            return []

    def dexscreener_discovery(self) -> List[Dict[str, Any]]:
        endpoints = []
        d = CFG.get("discovery", {})
        if d.get("dexscreener_profiles", True): endpoints.append(("profiles", "https://api.dexscreener.com/token-profiles/latest/v1"))
        if d.get("dexscreener_recent_updates", True): endpoints.append(("recent", "https://api.dexscreener.com/token-profiles/recent-updates/v1"))
        if d.get("dexscreener_boosts", True): endpoints.append(("boosts", "https://api.dexscreener.com/token-boosts/latest/v1"))
        found, errs = {}, []
        for label, url in endpoints:
            try:
                r = self.s.get(url, timeout=8); r.raise_for_status(); j = r.json()
                items = j if isinstance(j, list) else ([j] if isinstance(j, dict) else [])
                for x in items:
                    if (x.get("chainId") or "").lower() != "solana": continue
                    addr = x.get("tokenAddress") or x.get("address")
                    if addr: found[addr] = {"address": addr, "symbol": x.get("symbol") or "", "source": f"dexscreener:{label}"}
            except Exception as exc:
                errs.append(f"{label}:{type(exc).__name__}")
        ok = bool(endpoints) and len(errs) < len(endpoints)
        self.last_dex = {"ok": ok, "status": "OK" if ok else "ERROR", "ts_ms": now_ms(), "items": len(found), "detail": ", ".join(errs) if errs else "profiles/recent/boosts"}
        return list(found.values())

    def discovery(self) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        dex = self.dexscreener_discovery()
        bir = self.birdeye_new_listings(force=False)
        merged = {}
        for x in dex + bir:
            addr = x.get("address") or x.get("mint") or x.get("tokenAddress")
            if addr: merged[addr] = x
        return list(merged.values()), {"dex_discovered": len(dex), "birdeye_discovered": len(bir), "unique_discovered": len(merged)}

    def dex_pair(self, mint: str) -> Optional[Dict[str, Any]]:
        try:
            r = self.s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8)
            r.raise_for_status(); pairs = r.json().get("pairs") or []
            sol = [p for p in pairs if (p.get("chainId") or "").lower() == "solana" and (p.get("baseToken") or {}).get("address") == mint]
            if not sol: return None
            return max(sol, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        except Exception:
            return None

    def token_decimals(self, mint: str) -> Optional[int]:
        if not self.helius_key:
            return None
        try:
            r = self.s.post(f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}",
                json={"jsonrpc":"2.0","id":1,"method":"getAsset","params":{"id":mint}}, timeout=8)
            j=r.json() if r.content else {}
            ti=((j.get("result") or {}).get("token_info") or {})
            d=ti.get("decimals")
            return int(d) if d is not None else None
        except Exception:
            return None

    def quote(self, input_mint, output_mint, raw):
        started = now_ms()
        if not self.jupiter_key:
            return {"ok": False, "reason": "JUPITER_API_KEY_MISSING"}
        if int(raw) <= 0:
            return {"ok": False, "reason": "INVALID_AMOUNT"}
        try:
            r = self.s.get("https://api.jup.ag/swap/v1/quote",
                headers={"x-api-key": self.jupiter_key},
                params={"inputMint": input_mint, "outputMint": output_mint,
                        "amount": str(int(raw)), "swapMode": "ExactIn",
                        "slippageBps": int(CFG["quote_slippage_bps"]),
                        "restrictIntermediateTokens": "true", "instructionVersion": "V2"}, timeout=8)
            if not r.ok:
                return {"ok": False, "reason": f"JUPITER_HTTP_{r.status_code}"}
            j = r.json()
            out = int(j.get("outAmount") or 0)
            minimum = int(j.get("otherAmountThreshold") or 0)
            impact = abs(float(j["priceImpactPct"])) * 100
            if (not j.get("routePlan") or not 0 < minimum <= out
                or j.get("inputMint") != input_mint or j.get("outputMint") != output_mint
                or int(j.get("inAmount", 0)) != int(raw) or not math.isfinite(impact)):
                return {"ok": False, "reason": "INVALID_QUOTE"}
            if now_ms() - started > CFG["quote_max_age_ms"]:
                return {"ok": False, "reason": "STALE_QUOTE"}
            return {"ok": True, "ts_ms": started, "out_raw": out,
                    "min_raw": minimum, "impact_pct": impact, "response": j}
        except Exception as exc:
            return {"ok": False, "reason": type(exc).__name__}

    def exitability_quote(self, mint, token_amount):
        dec = self.token_decimals(mint)
        if dec is None:
            return {"ok": False, "reason": "DECIMALS_UNAVAILABLE"}
        raw = int(Decimal(str(token_amount)) * (10 ** dec))
        q = self.quote(mint, USDC_MINT, raw)
        if q.get("ok"):
            q.update(out_usdc=q["out_raw"] / 1e6, min_usdc=q["min_raw"] / 1e6,
                     qty=raw / (10 ** dec))
        return q

    def helius_health(self) -> Dict[str, Any]:
        if not self.helius_key:
            return {"ok": False, "status": "NO KEY", "detail": "HELIUS_API_KEY missing"}
        try:
            r = self.s.post(f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}",
                            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=8)
            j = r.json() if r.content else {}
            ok = r.ok and j.get("result") == "ok"
            return {"ok": ok, "status": "CONNECTED" if ok else f"HTTP {r.status_code}", "detail": str(j.get("result") or j.get("error") or "")[:180]}
        except Exception as exc:
            return {"ok": False, "status": "ERROR", "detail": repr(exc)[:180]}


