import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from storage_guard import StorageGuard, TABLES, TEMP_HEADER, FREE_RESERVE_BYTES

class StorageTests(unittest.TestCase):
    def test_off_blocks_every_telemetry_table_and_keeps_meta(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"BBB_MARKET_PERSIST_ENABLED":"0"}):
            cx=sqlite3.connect(Path(d)/"bbb.sqlite3")
            cx.executescript(Path(__file__).with_name("schema.sql").read_text())
            g=StorageGuard(d);g.install(cx)
            for table in TABLES:
                cols=[r for r in cx.execute(f"PRAGMA table_info({table})") if r[3] and r[4] is None]
                vals=["TRADING" if r[1]=="wallet" else ("x" if "TEXT" in r[2] else 1) for r in cols]
                cx.execute(f"INSERT INTO {table}({','.join(r[1] for r in cols)}) VALUES({','.join('?' for _ in cols)})",vals)
                self.assertEqual(cx.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0)
            cx.execute("INSERT OR REPLACE INTO meta VALUES('heartbeat_ms','1')");cx.commit()
            self.assertEqual(cx.execute("SELECT value FROM meta WHERE key='heartbeat_ms'").fetchone()[0],'1')
            self.assertEqual(cx.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            cx.close()

    def test_caps_and_hard_guard_survive_restart(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"BBB_MARKET_PERSIST_ENABLED":"1"}):
            db=Path(d)/"bbb.sqlite3";cx=sqlite3.connect(db)
            cx.executescript(Path(__file__).with_name("schema.sql").read_text());g=StorageGuard(d);g.install(cx)
            sql="INSERT INTO events(ts_ms,session_id,stage,payload_json) VALUES(1,'s','MARKET_SAMPLE',?)"
            for i in range(1100):cx.execute(sql,('{}',))
            self.assertEqual(cx.execute('SELECT COUNT(*) FROM events').fetchone()[0],1000)
            cx.execute(sql,('x'*17000,))
            self.assertEqual(cx.execute('SELECT MAX(id) FROM events').fetchone()[0],1100)
            cx.commit()
            cap=Path(d)/'cap'
            with cap.open('wb') as f:f.truncate(250_000_000)
            cx.execute(sql,('{}',));self.assertTrue(g.active)
            g.publish(cx);cx.execute("INSERT OR REPLACE INTO meta VALUES('heartbeat_ms','2')");cx.commit()
            cx.close();cap.unlink()
            cx=sqlite3.connect(db);g=StorageGuard(d);g.install(cx)
            self.assertFalse(g.allow());cx.close()

    def test_low_free_space_blocks_research_and_preserves_core(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"BBB_RESEARCH_PERSIST_ENABLED":"1"}):
            g=StorageGuard(d)
            with patch("storage_guard.shutil.disk_usage", return_value=type("Usage",(),{"total":1000000000,"used":990000000,"free":FREE_RESERVE_BYTES-1})()):
                self.assertEqual(g.allow_research(),0)
                self.assertTrue(g.active)

    def test_only_owned_completed_old_temps_rotate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);owned=root/"storage_guard_tmp";owned.mkdir()
            completed=owned/"guard-old.tmp";completed.write_bytes(TEMP_HEADER+b"fixture")
            unmarked=owned/"guard-unknown.tmp";unmarked.write_bytes(b"valuable")
            research=root/"research.tmp";research.write_bytes(b"valuable")
            wal=root/"bbb.sqlite3-wal";wal.write_bytes(b"critical")
            for p in (completed,unmarked,research,wal):os.utime(p,(1,1))
            g=StorageGuard(d);self.assertGreater(g.rotate_owned_temps(now=10000),0)
            self.assertFalse(completed.exists())
            self.assertTrue(unmarked.exists());self.assertTrue(research.exists());self.assertTrue(wal.exists())
            self.assertEqual(g.rotate_owned_temps(now=10000),0)

    def test_trade_and_wallet_history_have_no_evicting_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            cx=sqlite3.connect(Path(d)/"bbb.sqlite3")
            cx.executescript(Path(__file__).with_name("schema.sql").read_text())
            StorageGuard(d).install(cx)
            self.assertEqual(cx.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN ('bounded_trades_rows','bounded_wallet_ledger_rows')").fetchone()[0],0)
            cx.close()

    def test_growth_uses_publish_samples(self):
        with tempfile.TemporaryDirectory() as d:
            g=StorageGuard(d)
            with patch("storage_guard.time.monotonic",side_effect=[10,20]):
                g.used_bytes=100;g.sample_growth()
                g.used_bytes=300;g.sample_growth()
            self.assertEqual(g.growth_bps,20)

if __name__=='__main__':unittest.main()

