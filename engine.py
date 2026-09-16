import json, os, sqlite3, time, math, threading
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import requests
from telegram_notify import notify

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "v4.sqlite3"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def now_ms(): return int(time.time() * 1000)
def clamp(v, a, b): return max(a, min(b, v))

def age_text(ts_ms: int) -> str:
    if not ts_ms: return "never"
    sec = max(0, int((now_ms() - ts_ms) / 1000))
    if sec < 60: return f"{sec}s ago"
    if sec < 3600: return f"{sec//60}m ago"
    return f"{sec//3600}h {(sec%3600)//60}m ago"


class DBStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.cx = sqlite3.connect(DB, check_same_thread=False)
        self.cx.row_factory = sqlite3.Row
        with self.lock:
            self.cx.executescript("""
            CREATE TABLE IF NOT EXISTS candidates(
              mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, first_seen_ms INTEGER,
              liquidity REAL, price REAL, score REAL, risk REAL, raw TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, mint TEXT,
              price REAL, liquidity REAL, volume_m5 REAL, buys_m5 REAL, sells_m5 REAL,
              score REAL, risk REAL
            );
            CREATE TABLE IF NOT EXISTS positions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, status TEXT,
              entry_ms INTEGER, entry_price REAL, initial_usd REAL, token_qty REAL,
              remaining_qty REAL, peak_price REAL, realized_usd REAL DEFAULT 0,
              milestone_idx INTEGER DEFAULT 0, exit_ms INTEGER, exit_price REAL, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS ledger(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, mode TEXT, action TEXT,
              mint TEXT, symbol TEXT, usd REAL, qty REAL, price REAL, fee_usd REAL,
              tx_sig TEXT, note TEXT
            );
            CREATE TABLE IF NOT EXISTS entry_decisions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER, mint TEXT,
              symbol TEXT, decision TEXT, reason TEXT, score REAL, risk REAL,
              liquidity REAL, volume_m5 REAL, buys_m5 REAL, sells_m5 REAL,
              route_status TEXT, route_detail TEXT
            );
            """)
            # v4.0.7 additive migration: preserve the existing Railway SQLite database.
            cols = {r[1] for r in self.cx.execute("PRAGMA table_info(positions)").fetchall()}
            migrations = {
                "active_qty": "REAL",
                "moonbag_qty": "REAL DEFAULT 0",
                "compound_anchor_price": "REAL",
                "working_base_usd": "REAL",
                "harvest_count": "INTEGER DEFAULT 0",
                "moonbag_milestone_idx": "INTEGER DEFAULT 0"
            }
            for col, ddl in migrations.items():
                if col not in cols:
                    self.cx.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
            self.cx.execute("UPDATE positions SET active_qty=remaining_qty WHERE active_qty IS NULL")
            self.cx.execute("UPDATE positions SET moonbag_qty=0 WHERE moonbag_qty IS NULL")
            self.cx.execute("UPDATE positions SET compound_anchor_price=entry_price WHERE compound_anchor_price IS NULL OR compound_anchor_price<=0")
            self.cx.execute("UPDATE positions SET working_base_usd=active_qty*compound_anchor_price WHERE working_base_usd IS NULL OR working_base_usd<=0")
            self.cx.execute("UPDATE positions SET harvest_count=0 WHERE harvest_count IS NULL")
            self.cx.execute("UPDATE positions SET moonbag_milestone_idx=0 WHERE moonbag_milestone_idx IS NULL")
            self.cx.commit()
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.cx.execute(sql, args).fetchall()]
    def execute(self, sql, args=()):
        with self.lock:
            self.cx.execute(sql, args); self.cx.commit()
    def health(self) -> Tuple[bool, str]:
        try:
            row = self.q("SELECT 1 ok")[0]
            return bool(row["ok"] == 1), f"SQLite OK • {DB}"
        except Exception as exc:
            return False, repr(exc)


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
            self.last_birdeye = {"ok": False, "status": f"HTTP {code}" if code else "ERROR", "ts_ms": now_ms(), "items": 0, "detail": repr(exc)[:220]}
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
            sol = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
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

    def exitability_quote(self, mint: str, token_amount: float) -> Dict[str, Any]:
        """Read-only executable-route test: token -> USDC. Never signs or sends."""
        if not self.jupiter_key:
            return {"ok":False,"reason":"JUPITER_API_KEY missing"}
        dec=self.token_decimals(mint)
        if dec is None:
            return {"ok":False,"reason":"token decimals unavailable"}
        raw=max(1,int(token_amount*(10**dec)))
        try:
            r=self.s.get("https://api.jup.ag/swap/v1/quote", headers={"x-api-key":self.jupiter_key},
                params={"inputMint":mint,"outputMint":USDC_MINT,"amount":raw,"slippageBps":300,
                        "restrictIntermediateTokens":"true","instructionVersion":"V2"}, timeout=10)
            if not r.ok:
                return {"ok":False,"reason":f"Jupiter HTTP {r.status_code}"}
            j=r.json()
            if j.get("error") or not j.get("routePlan"):
                return {"ok":False,"reason":str(j.get("error") or "no sell route")[:160]}
            out_usdc=float(j.get("outAmount") or 0)/1_000_000
            impact=float(j.get("priceImpactPct") or 0)*100
            return {"ok":True,"out_usdc":out_usdc,"impact_pct":impact,"route_legs":len(j.get("routePlan") or [])}
        except Exception as exc:
            return {"ok":False,"reason":repr(exc)[:160]}

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


class Scorer:
    def score(self, p: Dict[str, Any], prev: Optional[Dict[str, Any]] = None):
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        price = float(p.get("priceUsd") or 0)
        pc = p.get("priceChange") or {}; vol = p.get("volume") or {}; tx = p.get("txns") or {}
        m5 = float(pc.get("m5") or 0); h1 = float(pc.get("h1") or 0)
        v5 = float(vol.get("m5") or 0); v1 = float(vol.get("h1") or 0)
        t5 = tx.get("m5") or {}; buys = float(t5.get("buys") or 0); sells = float(t5.get("sells") or 0)
        imbalance = (buys + 1) / (sells + 1)
        vol_acc = (v5 * 12) / (v1 + 1) if v1 else (2 if v5 > 1000 else 0)
        momentum = 0
        momentum += clamp((m5 - 1) / 7 * 22, 0, 22)
        momentum += clamp((h1 - 3) / 37 * 16, 0, 16)
        momentum += clamp((vol_acc - 1) / 4 * 20, 0, 20)
        momentum += clamp((imbalance - 1) / 2.5 * 18, 0, 18)
        momentum += clamp(math.log10(max(liq, 1) / 25000) * 12, 0, 12)
        momentum += 12 if buys >= 20 else clamp(buys / 20 * 12, 0, 12)
        risk = 0
        if liq < CFG["min_liquidity_usd"]: risk += 35
        if h1 > 100: risk += 20
        if m5 > 35: risk += 25
        if sells == 0 and buys > 20: risk += 10
        fdv = float(p.get("fdv") or 0); mc = float(p.get("marketCap") or 0); basis = mc or fdv
        if basis and liq / basis < 0.01: risk += 25
        elif basis and liq / basis < 0.03: risk += 12
        return round(clamp(momentum, 0, 100), 1), round(clamp(risk, 0, 100), 1), {
            "price": price, "liquidity": liq, "m5": m5, "h1": h1, "v5": v5, "v1": v1,
            "buys": buys, "sells": sells, "imbalance": imbalance, "vol_acc": vol_acc
        }


class PositionManager:
    """PAPER position manager for v4.0.7 COMPOUND HARVEST + MOONBAG.

    Compound rule: every +15% from the current cycle anchor, harvest 10% of the
    current working base to cash; the remaining +5% becomes the next working base.
    After N compound harvests, a small token reserve is carved out as a moonbag.
    The working leg can later stop/trail independently; the moonbag is kept for
    configured x10/x100-style milestones.
    """
    def __init__(self, db: DBStore):
        self.db = db
        self.lock = threading.RLock()

    def _available_cash(self):
        capital = float(CFG.get("paper_capital_usd", 150))
        buys = float(self.db.q("SELECT COALESCE(SUM(usd),0) n FROM ledger WHERE mode='PAPER' AND action='BUY'")[0]["n"] or 0)
        proceeds = float(self.db.q("SELECT COALESCE(SUM(usd),0) n FROM ledger WHERE mode='PAPER' AND action IN ('HARVEST','MOONBAG_HARVEST','EXIT')")[0]["n"] or 0)
        return capital - buys + proceeds

    def open_paper(self, mint, symbol, price):
        if price <= 0: return False
        with self.lock:
            if self.db.q("SELECT id FROM positions WHERE status IN ('OPEN','MOONBAG') AND mint=? LIMIT 1", (mint,)):
                return False
            n_open = self.db.q("SELECT COUNT(*) n FROM positions WHERE status IN ('OPEN','MOONBAG')")[0]["n"]
            if n_open >= int(CFG.get("max_open_positions", 1)):
                return False
            usd = float(CFG["position_size_usd"])
            # Never create PAPER leverage accidentally: a new stake must be funded by PAPER cash.
            if self._available_cash() + 1e-9 < usd:
                return False
            qty = usd / price
            self.db.execute("""INSERT INTO positions(
                mint,symbol,status,entry_ms,entry_price,initial_usd,token_qty,remaining_qty,peak_price,
                active_qty,moonbag_qty,compound_anchor_price,working_base_usd,harvest_count,moonbag_milestone_idx
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mint, symbol, "OPEN", now_ms(), price, usd, qty, qty, price,
                 qty, 0.0, price, usd, 0, 0))
            self.db.execute("INSERT INTO ledger(ts_ms,mode,action,mint,symbol,usd,qty,price,fee_usd,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (now_ms(), "PAPER", "BUY", mint, symbol, usd, qty, price, 0, "paper fill"))
        notify(f"🟢 V4 PAPER BUY\n{symbol}\nEntry: ${price:.10g}\nPosition: ${usd:.2f}\nMode: PAPER")
        return True

    def open_paper_with_reason(self, mint, symbol, price):
        """Open a simulated position and expose the exact non-open reason."""
        if price <= 0:
            return False, "INVALID_PRICE"
        with self.lock:
            if self.db.q("SELECT id FROM positions WHERE status IN ('OPEN','MOONBAG') AND mint=? LIMIT 1", (mint,)):
                return False, "DUPLICATE_OPEN_MINT"
            n_open = self.db.q("SELECT COUNT(*) n FROM positions WHERE status IN ('OPEN','MOONBAG')")[0]["n"]
            if n_open >= int(CFG.get("max_open_positions", 1)):
                return False, "MAX_OPEN_POSITIONS"
            if self._available_cash() + 1e-9 < float(CFG["position_size_usd"]):
                return False, "INSUFFICIENT_PAPER_CASH"
        return (True, "OPENED") if self.open_paper(mint, symbol, price) else (False, "OPEN_FAILED")

    def _sale(self, p, action, qty, price, note):
        if qty <= 0: return 0.0, 0.0
        gross = qty * price
        friction = gross * float(CFG["paper_friction_pct_per_sale"]) / 100 + float(CFG["paper_network_cost_usd_per_tx"])
        net = max(0.0, gross - friction)
        self.db.execute("INSERT INTO ledger(ts_ms,mode,action,mint,symbol,usd,qty,price,fee_usd,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (now_ms(), "PAPER", action, p["mint"], p["symbol"], net, qty, price, friction, note))
        return net, friction

    def update(self, mint, price, position_id=None):
        if price <= 0: return False
        with self.lock:
            if position_id is None:
                rows = self.db.q("SELECT * FROM positions WHERE status IN ('OPEN','MOONBAG') AND mint=? ORDER BY id LIMIT 1", (mint,))
            else:
                rows = self.db.q("SELECT * FROM positions WHERE status IN ('OPEN','MOONBAG') AND id=? AND mint=?", (position_id, mint))
            if not rows: return False
            p = rows[0]

        peak = max(float(p["peak_price"] or p["entry_price"]), price)
        active = float(p.get("active_qty") if p.get("active_qty") is not None else p["remaining_qty"] or 0)
        moonbag = float(p.get("moonbag_qty") or 0)
        anchor = float(p.get("compound_anchor_price") or p["entry_price"])
        base = float(p.get("working_base_usd") or (active * anchor))
        hcount = int(p.get("harvest_count") or 0)
        mb_idx = int(p.get("moonbag_milestone_idx") or 0)
        realized = float(p.get("realized_usd") or 0)

        trigger_pct = float(CFG.get("compound_trigger_pct", 15))
        cash_pct = float(CFG.get("compound_cash_pct_of_base", 10))
        reserve_after = int(CFG.get("moonbag_reserve_after_harvests", 3))
        reserve_pct = float(CFG.get("moonbag_reserve_pct_of_remaining", 10))

        # Multiple cycles can be crossed in one price jump. Each cycle is anchored
        # at the previous harvest price; gross harvest is 10% of that cycle's base.
        while active > 0 and price >= anchor * (1 + trigger_pct / 100):
            target_gross = base * cash_pct / 100
            sell_qty = min(active, target_gross / price)
            if sell_qty <= 0: break
            net, friction = self._sale(p, "HARVEST", sell_qty, price,
                                       f"compound +{trigger_pct:g}% / cash {cash_pct:g}% of base")
            active -= sell_qty
            realized += net
            hcount += 1
            anchor = price
            base = active * price

            # Once enough profit cycles have been banked, ring-fence a small tail.
            if moonbag <= 0 and hcount >= reserve_after and active > 0 and reserve_pct > 0:
                reserve = active * reserve_pct / 100
                active -= reserve
                moonbag += reserve
                base = active * price
                notify(f"🌙 V4 MOONBAG CREATED\n{p['symbol']}\nReserve: {reserve_pct:g}% of remaining tokens\nWorking leg continues")

            remaining = active + moonbag
            self.db.execute("""UPDATE positions SET remaining_qty=?,active_qty=?,moonbag_qty=?,realized_usd=?,
                compound_anchor_price=?,working_base_usd=?,harvest_count=?,peak_price=? WHERE id=?""",
                (remaining, active, moonbag, realized, anchor, base, hcount, peak, p["id"]))
            notify(f"💰 V4 COMPOUND HARVEST\n{p['symbol']}\nCycle: +{trigger_pct:g}%\nCash NET: ${net:.2f}\nNew working base: ${base:.2f}\nHarvests: {hcount}\nMoonbag: {'ON' if moonbag>0 else 'not yet'}")

        # Moonbag ladder is independent from the working leg and measured from entry.
        mb_levels = CFG.get("moonbag_milestones", [])
        entry = float(p["entry_price"])
        multiple = price / entry if entry > 0 else 0
        while moonbag > 0 and mb_idx < len(mb_levels) and multiple >= float(mb_levels[mb_idx]["multiple"]):
            level = mb_levels[mb_idx]
            sell_pct = float(level.get("sell_pct", 50))
            sell_qty = moonbag * sell_pct / 100
            net, friction = self._sale(p, "MOONBAG_HARVEST", sell_qty, price,
                                       f"moonbag x{level['multiple']:g} / sell {sell_pct:g}%")
            moonbag -= sell_qty
            realized += net
            mb_idx += 1
            remaining = active + moonbag
            self.db.execute("UPDATE positions SET remaining_qty=?,moonbag_qty=?,realized_usd=?,moonbag_milestone_idx=?,peak_price=? WHERE id=?",
                            (remaining, moonbag, realized, mb_idx, peak, p["id"]))
            notify(f"🌙💰 V4 MOONBAG HARVEST\n{p['symbol']}\nLevel: x{level['multiple']:g}\nCash NET: ${net:.2f}\nMoonbag remaining: {moonbag:.6g} tokens")

        # Working-leg protection. Before any compound harvest use the hard stop.
        # Afterwards trail from the local peak. The protected moonbag is NOT sold by this stop.
        first_hit = hcount >= 1
        stop_hit = active > 0 and ((not first_hit and price <= entry * (1 - float(CFG["pre_tp_stop_pct"]) / 100)) or
                    (first_hit and price <= peak * (1 - float(CFG["trailing_drawdown_after_first_milestone_pct"]) / 100)))
        if stop_hit:
            sell_qty = active
            net, friction = self._sale(p, "EXIT", sell_qty, price,
                                       "pre-harvest stop" if not first_hit else "working-leg trailing stop")
            realized += net
            active = 0.0
            remaining = moonbag
            if moonbag > 0:
                status, reason = "MOONBAG", "WORKING_STOP_MOONBAG_REMAINS"
            else:
                status, reason = "CLOSED", ("STOP" if not first_hit else "TRAILING")
            with self.db.lock:
                self.db.cx.execute("""UPDATE positions SET status=?,remaining_qty=?,active_qty=0,moonbag_qty=?,realized_usd=?,
                    exit_ms=?,exit_price=?,reason=?,peak_price=? WHERE id=?""",
                    (status, remaining, moonbag, realized, now_ms(), price, reason, peak, p["id"]))
                self.db.cx.commit()
            pnl_so_far = realized + moonbag * price - float(p["initial_usd"])
            notify(f"🔴 V4 WORKING LEG EXIT\n{p['symbol']}\nExit: ${price:.10g}\nCash proceeds total: ${realized:.2f}\nMoonbag remains: {'YES' if moonbag>0 else 'NO'}\nMarked P&L: ${pnl_so_far:+.2f}")
            return True

        # Fully close after the final moonbag sale if no working leg remains.
        if active <= 0 and moonbag <= 0:
            self.db.execute("UPDATE positions SET status='CLOSED',remaining_qty=0,reason='MOONBAG_COMPLETE',exit_ms=?,exit_price=?,peak_price=? WHERE id=?",
                            (now_ms(), price, peak, p["id"]))
        else:
            status = "OPEN" if active > 0 else "MOONBAG"
            self.db.execute("UPDATE positions SET status=?,remaining_qty=?,active_qty=?,moonbag_qty=?,realized_usd=?,peak_price=?,compound_anchor_price=?,working_base_usd=?,harvest_count=?,moonbag_milestone_idx=? WHERE id=?",
                            (status, active+moonbag, active, moonbag, realized, peak, anchor, base, hcount, mb_idx, p["id"]))
        return True


class V4Engine:
    def __init__(self):
        self.db = DBStore(); self.md = MarketData(); self.sc = Scorer(); self.pm = PositionManager(self.db)
        self.last_error = ""; self.last_scan = 0; self.scan_lock = threading.Lock(); self.exitability_alerted=set()
        self.scan_diag = {"status": "NOT RUN", "ts_ms": 0}

    def hard_safety(self):
        return {"config_mode": CFG.get("mode"), "env_mode": os.getenv("V4_MODE", "PAPER"),
                "auto_env": os.getenv("AUTO_TRADING_ENABLED", "false"),
                "auto_hard_lock": CFG.get("auto_trading_hard_lock", True), "execution": "DISABLED_IN_CODE"}

    def exitability_for_entry(self, mint, price):
        usd=float(CFG.get("position_size_usd",50))
        qty=usd/price if price>0 else 0
        q=self.md.exitability_quote(mint,qty)
        if not q.get("ok"):
            return False,q
        ratio=float(q.get("out_usdc") or 0)/usd if usd else 0
        q["value_ratio"]=ratio
        ok=(ratio>=float(CFG.get("exitability_min_value_ratio",0.82)) and
            float(q.get("impact_pct") or 999)<=float(CFG.get("max_price_impact_pct",5)))
        return ok,q

    def _record_entry_decision(self, mint, symbol, decision, reason, score, risk, m,
                               route_status="NOT_CHECKED", route_detail=""):
        self.db.execute("""INSERT INTO entry_decisions(
            ts_ms,mint,symbol,decision,reason,score,risk,liquidity,volume_m5,
            buys_m5,sells_m5,route_status,route_detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_ms(), mint, symbol, decision, reason, score, risk,
             float(m.get("liquidity") or 0), float(m.get("v5") or 0),
             float(m.get("buys") or 0), float(m.get("sells") or 0),
             route_status, str(route_detail or "")[:300]))

    def _entry_gate(self, mint, symbol, score, risk, m):
        """Transparent PAPER gate. Jupiter is preferred; DEX fallback is PAPER-only."""
        if float(m.get("liquidity") or 0) < float(CFG["min_liquidity_usd"]):
            return False, "LOW_LIQUIDITY", "NOT_CHECKED", ""
        if float(m.get("v5") or 0) < float(CFG.get("min_volume_m5_usd", 100)):
            return False, "LOW_VOLUME_M5", "NOT_CHECKED", ""
        if float(m.get("buys") or 0) <= float(m.get("sells") or 0):
            return False, "BUY_FLOW_NOT_POSITIVE", "NOT_CHECKED", ""
        if score < float(CFG["min_momentum_score"]):
            return False, "LOW_SCORE", "NOT_CHECKED", ""
        if risk > float(CFG["max_risk_score"]):
            return False, "HIGH_RISK", "NOT_CHECKED", ""

        if not CFG.get("require_exitability_quote", True):
            return True, "ENTRY_OK", "DISABLED", "quote not required"
        exit_ok, quote = self.exitability_for_entry(mint, m["price"])
        if exit_ok:
            return True, "ENTRY_OK", "JUPITER_OK", json.dumps(quote, ensure_ascii=False)

        # This never authorizes a real order. It only prevents a transient Jupiter
        # outage from making the PAPER experiment record zero positions.
        fallback_ok = (
            CFG.get("paper_allow_dex_fallback", True)
            and str(os.getenv("V4_MODE", "PAPER")).upper() == "PAPER"
            and float(m.get("liquidity") or 0) >= float(CFG.get("paper_fallback_min_liquidity_usd", 20000))
            and float(m.get("v5") or 0) >= float(CFG.get("paper_fallback_min_volume_m5_usd", 1000))
        )
        detail = str(quote.get("reason") or quote)[:300]
        if fallback_ok:
            return True, "ENTRY_OK_DEX_FALLBACK", "DEX_PROXY", detail
        return False, "NO_EXECUTABLE_EXIT_ROUTE", "JUPITER_FAILED", detail

    def scan_once(self, trigger="manual"):
        if not self.scan_lock.acquire(blocking=False):
            return {"status": "BUSY", "trigger": trigger}
        started = now_ms()
        d = {"status": "RUNNING", "trigger": trigger, "ts_ms": started, "discovered": 0, "pairs_found": 0,
             "no_pair": 0, "candidates_saved": 0, "low_liquidity": 0, "low_score": 0, "high_risk": 0,
             "eligible": 0, "paper_opened": 0, "open_positions_updated": 0, "exitability_failed": 0,
             "open_exitability_bad": 0, "low_volume_m5": 0, "buy_flow_not_positive": 0,
             "paper_open_blocked": 0, "dex_fallback_entries": 0}
        try:
            listings, src = self.md.discovery(); d.update(src); d["discovered"] = len(listings)
            # Always update open PAPER positions even if they are no longer in the discovery feed.
            for p in self.db.q("SELECT id,mint,symbol,remaining_qty FROM positions WHERE status IN ('OPEN','MOONBAG')"):
                pair = self.md.dex_pair(p["mint"])
                if pair:
                    price = float(pair.get("priceUsd") or 0)
                    if price > 0:
                        self.pm.update(p["mint"], price, position_id=p["id"]); d["open_positions_updated"] += 1
                        q=self.md.exitability_quote(p["mint"],float(p.get("remaining_qty") or 0))
                        bad=(not q.get("ok")) or float(q.get("impact_pct") or 999)>float(CFG.get("exitability_emergency_impact_pct",10))
                        if bad:
                            d["open_exitability_bad"] += 1
                            if p["mint"] not in self.exitability_alerted:
                                
                                if not q.get("ok"):
                                    detail = f"SELL ROUTE LOST — NO VALID QUOTE\nReason: {q.get('reason') or 'unknown'}"
                                else:
                                    detail = f"SELL ROUTE DEGRADED\nImpact: {float(q.get('impact_pct')):.2f}%"
                                notify(f"🚨 V4 EXITABILITY ALERT\n{p.get('symbol')}\n{detail}\nPAPER only — inspect immediately")
                                self.exitability_alerted.add(p["mint"])
                        else:
                            self.exitability_alerted.discard(p["mint"])
            for x in listings[:80]:
                mint = x.get("address") or x.get("mint") or x.get("tokenAddress")
                if not mint: continue
                pair = self.md.dex_pair(mint)
                if not pair:
                    d["no_pair"] += 1; continue
                d["pairs_found"] += 1
                score, risk, m = self.sc.score(pair)
                symbol = (pair.get("baseToken") or {}).get("symbol") or x.get("symbol") or mint[:6]
                name = (pair.get("baseToken") or {}).get("name") or x.get("name") or symbol
                self.db.execute("INSERT INTO candidates(mint,symbol,name,first_seen_ms,liquidity,price,score,risk,raw) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET liquidity=excluded.liquidity,price=excluded.price,score=excluded.score,risk=excluded.risk,raw=excluded.raw",
                    (mint, symbol, name, now_ms(), m["liquidity"], m["price"], score, risk, json.dumps(pair)))
                self.db.execute("INSERT INTO snapshots(ts_ms,mint,price,liquidity,volume_m5,buys_m5,sells_m5,score,risk) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now_ms(), mint, m["price"], m["liquidity"], m["v5"], m["buys"], m["sells"], score, risk))
                d["candidates_saved"] += 1
                allowed, reason, route_status, route_detail = self._entry_gate(mint, symbol, score, risk, m)
                if not allowed:
                    if reason == "LOW_LIQUIDITY": d["low_liquidity"] += 1
                    elif reason == "LOW_VOLUME_M5": d["low_volume_m5"] += 1
                    elif reason == "BUY_FLOW_NOT_POSITIVE": d["buy_flow_not_positive"] += 1
                    elif reason == "LOW_SCORE": d["low_score"] += 1
                    elif reason == "HIGH_RISK": d["high_risk"] += 1
                    elif reason == "NO_EXECUTABLE_EXIT_ROUTE": d["exitability_failed"] += 1
                    self._record_entry_decision(mint, symbol, "REJECT", reason, score, risk, m,
                                                route_status, route_detail)
                    continue

                d["eligible"] += 1
                if route_status == "DEX_PROXY": d["dex_fallback_entries"] += 1
                opened, open_reason = self.pm.open_paper_with_reason(mint, symbol, m["price"])
                self._record_entry_decision(mint, symbol, "OPEN" if opened else "BLOCK",
                                            reason if opened else open_reason, score, risk, m,
                                            route_status, route_detail)
                if opened:
                    d["paper_opened"] += 1
                    notify(f"🟢 V4.0.8 PAPER ENTRY\n{symbol}\nRoute: {route_status}\nReason: {reason}\nNo real order was sent")
                else:
                    d["paper_open_blocked"] += 1
            d["status"] = "OK"; self.last_error = ""
        except Exception as exc:
            self.last_error = repr(exc); d["status"] = "ERROR"; d["error"] = repr(exc)[:300]
        finally:
            d["duration_ms"] = now_ms() - started; self.last_scan = now_ms(); self.scan_diag = d
            print("V4_SCAN", json.dumps(d, ensure_ascii=False), flush=True)
            self.scan_lock.release()
        return d

    def stats(self):
        capital = float(CFG.get("paper_capital_usd", 150))
        buys = float(self.db.q("SELECT COALESCE(SUM(usd),0) n FROM ledger WHERE mode='PAPER' AND action='BUY'")[0]["n"] or 0)
        proceeds = float(self.db.q("SELECT COALESCE(SUM(usd),0) n FROM ledger WHERE mode='PAPER' AND action IN ('HARVEST','MOONBAG_HARVEST','EXIT')")[0]["n"] or 0)
        cash = capital - buys + proceeds
        open_rows = self.db.q("SELECT mint,remaining_qty FROM positions WHERE status IN ('OPEN','MOONBAG')")
        open_value = 0.0
        for p in open_rows:
            row = self.db.q("SELECT price FROM candidates WHERE mint=?", (p["mint"],))
            px = float(row[0]["price"] or 0) if row else 0.0
            open_value += float(p["remaining_qty"] or 0) * px
        equity = cash + open_value
        realized_pnl = float(self.db.q("SELECT COALESCE(SUM(realized_usd-initial_usd),0) n FROM positions WHERE status='CLOSED'")[0]["n"] or 0)
        harvested = float(self.db.q("SELECT COALESCE(SUM(usd),0) n FROM ledger WHERE mode='PAPER' AND action IN ('HARVEST','MOONBAG_HARVEST')")[0]["n"] or 0)
        return {
            "candidates": self.db.q("SELECT COUNT(*) n FROM candidates")[0]["n"],
            "open": len(open_rows),
            "closed": self.db.q("SELECT COUNT(*) n FROM positions WHERE status='CLOSED'")[0]["n"],
            "realized": realized_pnl,
            "paper_capital": capital, "cash": cash, "open_value": open_value,
            "equity": equity, "total_pnl": equity-capital, "harvested_proceeds": harvested
        }
