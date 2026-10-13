"""Fail-closed bounded storage; public market processing stays in memory."""
import json
import os
import sqlite3
from pathlib import Path

TABLES = ("events", "trades", "wallet_ledger", "equity_samples",
          "feature_samples", "microflow_episodes", "agent_council_decisions")
WARNING_BYTES = 150_000_000
HARD_BYTES = 250_000_000
ROW_CAP = 1000
PAYLOAD_CAP = 16_384

def enabled(name):
    return os.getenv(name, "0").lower() in {"1", "true", "yes", "on"}

class StorageGuard:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.requested = enabled("BBB_MARKET_PERSIST_ENABLED")
        self.active = False
        self.used_bytes = 0

    def scan(self):
        try:
            self.used_bytes = sum(p.stat().st_size for p in self.directory.rglob("*")
                                  if p.is_file())
            if self.used_bytes >= HARD_BYTES:
                self.active = True
        except OSError:
            self.active = True
        return self.used_bytes

    def allow(self):
        if not self.requested:
            return 0
        self.scan()
        return int(self.requested and not self.active)

    def install(self, cx):
        prior = cx.execute("SELECT value FROM meta WHERE key='storage_guard_state'").fetchone()
        self.active = bool(prior and prior[0] == "STORAGE_GUARD_ACTIVE")
        # DELETE journal has no endlessly growing WAL under dashboard readers.
        cx.execute("PRAGMA journal_mode=DELETE")
        cx.execute("PRAGMA busy_timeout=1000")
        page_size = cx.execute("PRAGMA page_size").fetchone()[0]
        cx.execute(f"PRAGMA max_page_count={128_000_000 // page_size}")
        cx.create_function("bbb_market_allowed", 0, self.allow)
        cx.create_function("bbb_trade_allowed", 0,
                           lambda: int(enabled("BBB_ROCKET_ENTRY_ENABLED") or
                                       enabled("BBB_MARKET_DNA_ENTRY_ENABLED")))
        for table in TABLES:
            columns = {r[1] for r in cx.execute(f"PRAGMA table_info({table})")}
            conditions = ["bbb_market_allowed()=0"]
            if table in {"trades", "wallet_ledger"}:
                conditions.append("bbb_trade_allowed()=0")
            if "payload_json" in columns:
                conditions.append(f"length(CAST(NEW.payload_json AS BLOB))>{PAYLOAD_CAP}")
            if "entry_features_json" in columns:
                conditions.append(f"length(CAST(NEW.entry_features_json AS BLOB))>{PAYLOAD_CAP}")
            cx.execute(f"""CREATE TRIGGER IF NOT EXISTS bounded_{table}_insert
                BEFORE INSERT ON {table} WHEN {' OR '.join(conditions)}
                BEGIN SELECT RAISE(IGNORE); END""")
            cx.execute(f"""CREATE TRIGGER IF NOT EXISTS bounded_{table}_rows
                AFTER INSERT ON {table} BEGIN
                DELETE FROM {table} WHERE id IN
                  (SELECT id FROM {table} ORDER BY id DESC LIMIT -1 OFFSET {ROW_CAP});
                END""")
            cx.execute(f"""CREATE TRIGGER IF NOT EXISTS bounded_{table}_update
                BEFORE UPDATE ON {table} WHEN {' OR '.join(conditions)}
                BEGIN SELECT RAISE(IGNORE); END""")
        cx.execute("""CREATE TRIGGER IF NOT EXISTS bounded_meta
            BEFORE INSERT ON meta WHEN length(NEW.key)>128 OR length(NEW.value)>16384
            OR ((SELECT COUNT(*) FROM meta)>=256 AND
                NOT EXISTS(SELECT 1 FROM meta WHERE key=NEW.key))
            BEGIN SELECT RAISE(IGNORE); END""")
        return cx

    def publish(self, cx, feed=None, heartbeat=0):
        self.scan()
        state = "STORAGE_GUARD_ACTIVE" if self.active else (
            "STORAGE_WARNING" if self.used_bytes >= WARNING_BYTES else "OK")
        values = {
            "storage_mode": "BOUNDED" if self.requested else "CORE_ONLY",
            "market_persist_enabled": str(int(self.requested and not self.active)),
            "storage_guard_state": state,
            "storage_used_bytes": str(self.used_bytes),
            "storage_warning_bytes": str(WARNING_BYTES),
            "storage_hard_stop_bytes": str(HARD_BYTES),
            "retention_policy": "ALL_SESSIONS_MAX_1000_ROWS_PER_TABLE; PAYLOAD_MAX_16KiB",
            "live_submission_enabled": "0",
        }
        for k, v in values.items():
            cx.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
        files = [{"path": str(p), "bytes": p.stat().st_size}
                 for p in self.directory.rglob("*") if p.is_file()]
        print("BBB_STORAGE_HEALTH", json.dumps({
            **values, "files": files, "heartbeat_ms": heartbeat,
            "feed_state": "CONNECTED" if feed and feed.health.connected else "RECONNECTING",
            "messages": feed.health.messages if feed else 0,
            "rocket_entries_enabled": int(enabled("BBB_ROCKET_ENTRY_ENABLED")),
            "market_dna_entries_enabled": int(enabled("BBB_MARKET_DNA_ENTRY_ENABLED")),
            "rows": {t: cx.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES},
        }), flush=True)
