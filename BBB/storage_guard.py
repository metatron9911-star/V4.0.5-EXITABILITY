"""Fail-closed bounded storage; public market processing stays in memory."""
import json
import os
import sqlite3
import time
import shutil
from pathlib import Path

TABLES = ("events", "trades", "wallet_ledger", "equity_samples",
          "feature_samples", "microflow_episodes", "agent_council_decisions")
RESEARCH_TABLES = {"microflow_episodes", "agent_council_decisions"}
WARNING_BYTES = 150_000_000
HARD_BYTES = 250_000_000
RESEARCH_SOFT_BYTES = 25_000_000
ROW_CAP = 1000
RESEARCH_ROW_CAP = {"microflow_episodes": 2000, "agent_council_decisions": 3000}
FREE_RESERVE_BYTES = 64 * 1024 * 1024
FILE_WARNING_BYTES = 64_000_000
GROWTH_WARNING_BPS = 5_000_000 / 60
TEMP_HEADER = b"BBB_GUARD_TEMP_V1 COMPLETE\n"
PAYLOAD_CAP = 16_384
RESEARCH_PAYLOAD_CAP = 12_288

def enabled(name):
    return os.getenv(name, "0").lower() in {"1", "true", "yes", "on"}

class StorageGuard:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.requested = enabled("BBB_MARKET_PERSIST_ENABLED")
        self.research_requested = enabled("BBB_RESEARCH_PERSIST_ENABLED")
        self.active = False
        self.used_bytes = 0
        self.free_bytes = None
        self.volume_total_bytes = None
        self.volume_used_bytes = None
        self.allocated_file_bytes = 0
        self.largest_file_bytes = 0
        self.growth_bps = 0.0
        self._last_sample = None
        self.housekeeping_runs = 0
        self.temp_removed_bytes = 0
        self.last_scan_error = None

    def scan(self):
        try:
            total = allocated = largest = 0
            for p in self.directory.rglob("*"):
                if p.is_symlink():
                    raise OSError("storage symlink requires review")
                if p.is_file():
                    st = p.stat()
                    total += st.st_size
                    allocated += getattr(st, "st_blocks", (st.st_size + 511) // 512) * 512
                    largest = max(largest, st.st_size)
            usage = shutil.disk_usage(self.directory)
            self.used_bytes = total
            self.allocated_file_bytes = allocated
            self.largest_file_bytes = largest
            self.free_bytes = usage.free
            self.volume_total_bytes = usage.total
            self.volume_used_bytes = usage.used
            self.last_scan_error = None
            if total >= HARD_BYTES or usage.free < FREE_RESERVE_BYTES:
                self.active = True
        except OSError:
            self.last_scan_error = "STORAGE_SCAN_UNKNOWN"
            self.free_bytes = None
            self.active = True
        return self.used_bytes

    def rotate_owned_temps(self, now=None):
        # Only completed files explicitly marked as this guard's disposable output.
        # No general *.tmp/log glob, database journal, research file or SQL cleanup.
        self.housekeeping_runs += 1
        owned = self.directory / "storage_guard_tmp"
        if not owned.exists():
            return 0
        if owned.is_symlink() or not owned.is_dir():
            self.active = True
            return 0
        now = time.time() if now is None else now
        removed = 0
        for p in owned.iterdir():
            if p.is_symlink() or not p.is_file() or not p.name.startswith("guard-") or p.suffix != ".tmp":
                continue
            st = p.stat()
            if now - st.st_mtime < 3600 or st.st_size > 262144:
                continue
            with p.open("rb") as f:
                if f.read(len(TEMP_HEADER)) != TEMP_HEADER:
                    continue
            p.unlink()
            removed += st.st_size
        self.temp_removed_bytes += removed
        return removed

    def sample_growth(self):
        stamp = time.monotonic()
        if self._last_sample is not None:
            elapsed = stamp - self._last_sample[0]
            if elapsed > 0:
                self.growth_bps = max(0, self.used_bytes - self._last_sample[1]) / elapsed
        self._last_sample = (stamp, self.used_bytes)

    def deleted_open_bytes(self):
        # Report only aggregate bytes on this volume; never fd names/content.
        seen = set()
        total = 0
        unknown = False
        try:
            device = self.directory.stat().st_dev
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    for fd in (proc / "fd").iterdir():
                        try:
                            if not os.readlink(fd).endswith(" (deleted)"):
                                continue
                            st = fd.stat()
                            identity = (st.st_dev, st.st_ino)
                            if st.st_dev == device and identity not in seen:
                                total += st.st_size
                                seen.add(identity)
                        except FileNotFoundError:
                            pass
                        except PermissionError:
                            unknown = True
                except FileNotFoundError:
                    pass
                except PermissionError:
                    unknown = True
        except OSError:
            unknown = True
        return total, unknown

    def allow(self):
        if not self.requested:
            return 0
        self.scan()
        return int(self.requested and not self.active)

    def allow_research(self):
        if not self.research_requested:
            return 0
        self.scan()
        return int(self.research_requested and not self.active and self.used_bytes < RESEARCH_SOFT_BYTES)

    def install(self, cx):
        prior = cx.execute("SELECT value FROM meta WHERE key='storage_guard_state'").fetchone()
        self.active = bool(prior and prior[0] == "STORAGE_GUARD_ACTIVE")
        # DELETE journal has no endlessly growing WAL under dashboard readers.
        cx.execute("PRAGMA journal_mode=DELETE")
        cx.execute("PRAGMA busy_timeout=1000")
        page_size = cx.execute("PRAGMA page_size").fetchone()[0]
        cx.execute(f"PRAGMA max_page_count={128_000_000 // page_size}")
        cx.create_function("bbb_market_allowed", 0, self.allow)
        cx.create_function("bbb_research_allowed", 0, self.allow_research)
        cx.create_function("bbb_trade_allowed", 0,
                           lambda: int(enabled("BBB_ROCKET_ENTRY_ENABLED") or
                                       enabled("BBB_MARKET_DNA_ENTRY_ENABLED")))
        for table in TABLES:
            # Rebuild guards on every boot so a mode change (CORE_ONLY -> bounded research)
            # cannot be blocked by stale trigger definitions from the prior deployment.
            cx.execute(f"DROP TRIGGER IF EXISTS bounded_{table}_insert")
            cx.execute(f"DROP TRIGGER IF EXISTS bounded_{table}_rows")
            cx.execute(f"DROP TRIGGER IF EXISTS bounded_{table}_update")
            columns = {r[1] for r in cx.execute(f"PRAGMA table_info({table})")}
            if table in RESEARCH_TABLES:
                conditions = ["bbb_research_allowed()=0"]
                row_cap = RESEARCH_ROW_CAP[table]
                payload_cap = RESEARCH_PAYLOAD_CAP
            else:
                conditions = ["bbb_market_allowed()=0"]
                row_cap = ROW_CAP
                payload_cap = PAYLOAD_CAP
            if table in {"trades", "wallet_ledger"}:
                conditions.append("bbb_trade_allowed()=0")
            if "payload_json" in columns:
                conditions.append(f"length(CAST(NEW.payload_json AS BLOB))>{payload_cap}")
            if "entry_features_json" in columns:
                conditions.append(f"length(CAST(NEW.entry_features_json AS BLOB))>{payload_cap}")
            cx.execute(f"""CREATE TRIGGER IF NOT EXISTS bounded_{table}_insert
                BEFORE INSERT ON {table} WHEN {' OR '.join(conditions)}
                BEGIN SELECT RAISE(IGNORE); END""")
            if table not in {"trades", "wallet_ledger"}:
                cx.execute(f"""CREATE TRIGGER IF NOT EXISTS bounded_{table}_rows
                    AFTER INSERT ON {table} BEGIN
                    DELETE FROM {table} WHERE id IN
                      (SELECT id FROM {table} ORDER BY id DESC LIMIT -1 OFFSET {row_cap});
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
        self.rotate_owned_temps()
        self.scan()
        self.sample_growth()
        deleted_bytes, deleted_unknown = self.deleted_open_bytes()
        state = "STORAGE_GUARD_ACTIVE" if self.active else (
            "STORAGE_WARNING" if self.used_bytes >= WARNING_BYTES or self.largest_file_bytes >= FILE_WARNING_BYTES or self.growth_bps >= GROWTH_WARNING_BPS else "OK")
        values = {
            "storage_mode": "BOUNDED" if self.requested else ("CORE+RESEARCH" if self.research_requested else "CORE_ONLY"),
            "market_persist_enabled": str(int(self.requested and not self.active)),
            "research_persist_enabled": str(int(self.research_requested and not self.active and self.used_bytes < RESEARCH_SOFT_BYTES)),
            "storage_guard_state": state,
            "storage_used_bytes": str(self.used_bytes),
            "storage_file_bytes": str(self.used_bytes),
            "storage_allocated_file_bytes": str(self.allocated_file_bytes),
            "storage_volume_used_bytes": str(self.volume_used_bytes),
            "storage_volume_total_bytes": str(self.volume_total_bytes),
            "storage_free_bytes": str(self.free_bytes),
            "storage_free_reserve_bytes": str(FREE_RESERVE_BYTES),
            "storage_largest_file_bytes": str(self.largest_file_bytes),
            "storage_growth_bytes_per_second": str(round(self.growth_bps, 2)),
            "storage_growth_warning_bps": str(GROWTH_WARNING_BPS),
            "storage_deleted_open_bytes": str(deleted_bytes),
            "storage_deleted_open_unknown": str(int(deleted_unknown)),
            "storage_housekeeping_runs": str(self.housekeeping_runs),
            "storage_temp_removed_bytes": str(self.temp_removed_bytes),
            "storage_scan_error": str(self.last_scan_error or "NONE"),
            "storage_measurement_scope": "FILES_AND_MOUNT; RAILWAY_SERVICE_DISK_IS_SEPARATE",
            "storage_warning_bytes": str(WARNING_BYTES),
            "storage_hard_stop_bytes": str(HARD_BYTES),
            "research_soft_stop_bytes": str(RESEARCH_SOFT_BYTES),
            "retention_policy": "MARKET_OFF; RESEARCH microflow<=2000 council<=3000; RESEARCH_PAYLOAD<=12KiB",
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

