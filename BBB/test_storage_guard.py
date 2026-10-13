import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from storage_guard import StorageGuard, TABLES

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

if __name__=='__main__':unittest.main()
