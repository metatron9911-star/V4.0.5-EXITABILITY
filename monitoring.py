import os, time, threading
from typing import Dict, Any
import requests
from telegram_notify import notify, get_me, get_updates
from engine import now_ms, age_text, CFG


def _status(ok: bool, status: str, detail: str = ""):
    return {"ok": bool(ok), "status": status, "detail": detail}


class RuntimeSupervisor:
    def __init__(self, engine):
        self.engine = engine
        self.stop_event = threading.Event()
        self.thread = None
        self.started_ms = 0
        self.last_heartbeat_ms = 0
        self.last_tg_poll_ms = 0
        self.telegram_offset = 0
        self.last_external = {}
        self._lock = threading.RLock()

    def start(self):
        with self._lock:
            if self.thread and self.thread.is_alive(): return self
            self.started_ms = now_ms()
            self.thread = threading.Thread(target=self._run, name="v4-runtime-supervisor", daemon=True)
            self.thread.start()
        return self

    def _run(self):
        scan_interval = max(15, int(os.getenv("AUTO_SCAN_INTERVAL_SEC", CFG.get("auto_scan_interval_sec", 30))))
        heartbeat_sec = max(300, int(os.getenv("STATUS_HEARTBEAT_MINUTES", CFG.get("status_heartbeat_minutes", 60))) * 60)
        next_scan = time.time() + 5
        next_heartbeat = time.time() + heartbeat_sec
        while not self.stop_event.is_set():
            now = time.time()
            if now >= next_scan:
                try: self.engine.scan_once(trigger="AUTO")
                except Exception as exc: print("V4_AUTO_SCAN_ERROR", repr(exc), flush=True)
                next_scan = now + scan_interval
            if now >= next_heartbeat:
                try: self.send_status(reason="hourly heartbeat")
                except Exception as exc: print("V4_HEARTBEAT_ERROR", repr(exc), flush=True)
                next_heartbeat = now + heartbeat_sec
            if now - (self.last_tg_poll_ms / 1000 if self.last_tg_poll_ms else 0) >= 5:
                self._poll_telegram_commands(); self.last_tg_poll_ms = now_ms()
            self.stop_event.wait(1.0)

    def _poll_telegram_commands(self):
        updates = get_updates(self.telegram_offset, timeout=0)
        allowed_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        for u in updates:
            self.telegram_offset = max(self.telegram_offset, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}; chat = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or "").strip().lower()
            if not chat or chat != allowed_chat: continue
            if text in ("/status", "/health", "status", "статус"):
                self.send_status(reason="Telegram /status")
            elif text in ("/help", "help"):
                notify("V4 Momentum Hunter commands:\n/status — sources & modules\n/help — this message\nTrading commands are disabled in PAPER build.")

    def external_status(self, refresh=False) -> Dict[str, Any]:
        # Birdeye status is taken from the scanner's last real API request to avoid burning CU on health checks.
        b = self.engine.md.last_birdeye
        if not self.engine.md.birdeye_key:
            birdeye = _status(False, "NO KEY", "BIRDEYE_API_KEY missing")
        elif b.get("ts_ms"):
            birdeye = _status(bool(b.get("ok")), "CONNECTED" if b.get("ok") else b.get("status", "ERROR"),
                              f"last new_listing: {age_text(int(b.get('ts_ms') or 0))}; items={b.get('items',0)}; {b.get('detail','')}")
        else:
            birdeye = _status(True, "CONFIGURED", "waiting for first budgeted new_listing poll")

        d = self.engine.md.last_dex
        if d.get("ts_ms"):
            dex = _status(bool(d.get("ok")), "CONNECTED" if d.get("ok") else "ERROR",
                          f"last discovery: {age_text(int(d.get('ts_ms') or 0))}; tokens={d.get('items',0)}; {d.get('detail','')}")
        else:
            dex = _status(True, "CONFIGURED", "waiting for first scan")

        if refresh or "helius" not in self.last_external:
            self.last_external["helius"] = self.engine.md.helius_health()
        h = self.last_external.get("helius") or {}
        helius = _status(bool(h.get("ok")), h.get("status", "UNKNOWN"), (h.get("detail") or "") + " • health/RPC layer")

        if self.engine.md.jupiter_key:
            jupiter = _status(True, "CONFIGURED", "read-only sell-route quotes enabled")
        else:
            jupiter = _status(False, "NO KEY", "JUPITER_API_KEY missing • entries fail closed")

        if refresh or "telegram" not in self.last_external:
            ok, detail = get_me(); self.last_external["telegram"] = {"ok": ok, "detail": detail}
        t = self.last_external.get("telegram") or {}
        telegram = _status(bool(t.get("ok")), "CONNECTED" if t.get("ok") else "ERROR", t.get("detail", ""))

        db_ok, db_detail = self.engine.db.health()
        safety = self.engine.hard_safety()
        runtime_ok = bool(self.thread and self.thread.is_alive())
        return {
            "Birdeye": birdeye,
            "DEX Screener": dex,
            "Helius": helius,
            "Jupiter exit route": jupiter,
            "Telegram bot": telegram,
            "SQLite /data": _status(db_ok, "CONNECTED" if db_ok else "ERROR", db_detail),
            "AUTO scanner": _status(runtime_ok, "RUNNING" if runtime_ok else "STOPPED", f"last scan: {age_text(self.engine.last_scan)}"),
            "PAPER engine": _status(True, "PAPER", f"v{CFG.get('version','4.0.2')}; execution={safety['execution']}"),
            "AUTO hard lock": _status(safety.get("execution") == "DISABLED_IN_CODE", "LOCKED", "real orders cannot be signed/sent in this build")
        }

    def format_status(self, reason="status") -> str:
        statuses = self.external_status(refresh=True)
        stats = self.engine.stats(); d = self.engine.scan_diag or {}
        lines = [f"🛰 V4 Momentum Hunter v{CFG.get('version','4.0.2')} STATUS", f"Reason: {reason}"]
        for name, s in statuses.items():
            icon = "✅" if s.get("ok") else "❌"
            lines.append(f"{icon} {name}: {s.get('status')} — {s.get('detail','')}")
        lines += [
            "",
            f"Scan: {d.get('status','NOT RUN')} • {age_text(int(d.get('ts_ms') or 0))}",
            f"Discovery: {d.get('discovered',0)} • pairs {d.get('pairs_found',0)} • eligible {d.get('eligible',0)} • opened {d.get('paper_opened',0)}",
            f"Filters: no_pair {d.get('no_pair',0)} • low_liq {d.get('low_liquidity',0)} • low_vol {d.get('low_volume_m5',0)} • flow {d.get('buy_flow_not_positive',0)} • low_score {d.get('low_score',0)} • high_risk {d.get('high_risk',0)} • exit_fail {d.get('exitability_failed',0)} • fallback {d.get('dex_fallback_entries',0)} • open_block {d.get('paper_open_blocked',0)} • open_exit_bad {d.get('open_exitability_bad',0)}",
            f"PAPER: candidates {stats['candidates']} • open {stats['open']} • closed {stats['closed']} • realized P&L ${stats['realized']:.2f}",
            f"PAPER equity: cash ${stats.get('cash',0):.2f} • open value ${stats.get('open_value',0):.2f} • equity ${stats.get('equity',0):.2f} • total P&L ${stats.get('total_pnl',0):+.2f}",
            "AUTO trading: HARD LOCKED"
        ]
        return "\n".join(lines)

    def send_status(self, reason="manual"):
        ok = notify(self.format_status(reason=reason))
        if ok: self.last_heartbeat_ms = now_ms()
        return ok
