import contextlib
import datetime as dt
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core import db
from core.strategy import calculate_strategy
from services.mcp_data import SnapshotReader, TZ, normalize_code


class MCPDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "business.db"
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text("COMMON_STOCK_CODES=600519,000001\n")
        with patch.object(db, "DB_PATH", str(self.path)), contextlib.redirect_stdout(io.StringIO()):
            db.init_db()
        self.reader = SnapshotReader(self.path, self.env)
        self.day = dt.datetime.now(TZ).date().isoformat()
        self.stamp = dt.datetime.now(TZ).strftime("%H:%M")
        yesterday = (dt.datetime.now(TZ).date() - dt.timedelta(days=1)).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO daily_records(date,code,name,close,high,low,avg_price) VALUES(?,?,?,?,?,?,?)",
                         (yesterday, "600519", "测试股", 9, 11, 8, 9.5))
            conn.execute("""INSERT INTO intraday_snapshots(code,date,time,price,high,low,vol,amount)
                VALUES(?,?,?,?,?,?,?,?)""", ("600519", self.day, self.stamp, 10, 11, 8, 1000, 10))
            conn.execute("""INSERT INTO portfolio(code,cost_price,stage_high,stage_low,max_price,quantity,updated_at)
                VALUES('600519',9,12,8,10,100,1)""")
            conn.execute("INSERT INTO account_settings VALUES('available_cash',1000,1)")
            for number in range(1, 6):
                conn.execute("""INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at)
                    VALUES(1,'600519','测试','breakout','normal',10,?,?)""", (f"event {number}", self.day + " 10:00:00"))

    def test_normalization_and_invalid_input(self):
        for code in ("600519", "sh600519", "600519.SH"):
            self.assertEqual(normalize_code(code), ("600519", "600519.SH"))
        self.assertEqual(normalize_code("588170")[1], "588170.SH")
        self.assertEqual(normalize_code("920001")[1], "920001.BJ")
        for code in ("../.env", "600519';DELETE", "random", "123"):
            with self.assertRaises(ValueError):
                self.reader.stock(code)

    def test_business_connection_enforces_read_only_and_missing_db_not_created(self):
        with self.reader.connect() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM portfolio")
        path = Path(self.tmp.name) / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            SnapshotReader(path, self.env).status()
        self.assertFalse(path.exists())

    def test_shared_signal_and_units_without_business_side_effects_or_network(self):
        before = self.path.read_bytes()
        with patch("tushare.get_realtime_quotes", side_effect=AssertionError("network forbidden")), \
                patch("services.ai.call_ai_model", side_effect=AssertionError("AI forbidden")):
            stock = self.reader.stock("sh600519")["data"]
            overview = self.reader.portfolio()["data"]
            self.reader.watchlist()
            self.reader.events()
            self.reader.status()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(stock["strategy"], calculate_strategy(10, 9, 11, 12, 8, True))
        self.assertEqual(stock["avg_price"], 10)
        self.assertEqual(stock["source"], "intraday_snapshots")
        self.assertFalse(stock["stale"])
        self.assertTrue(stock["observed_at"].endswith("+08:00"))
        self.assertEqual(overview["total_pnl_abs"], 100)
        self.assertEqual(overview["account_equity"], 2000)
        self.assertTrue(overview["valuation_complete"])
        self.assertEqual(overview["stocks"][0]["today_pnl_pct"], 11.11)

    def test_daily_fallback_is_never_labeled_realtime_and_missing_remains_missing(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM intraday_snapshots")
        stock = self.reader.stock("600519")["data"]
        self.assertTrue(stock["stale"])
        self.assertEqual(stock["timestamp_precision"], "day")
        self.assertEqual(stock["source"], "daily_records")
        self.assertIsNone(self.reader.portfolio()["data"]["stocks"][0]["today_pnl_pct"])
        self.assertEqual(self.reader.stock("000001")["data"]["status"], "missing")

    def test_partial_valuation_does_not_claim_account_equity(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO portfolio(code,cost_price,quantity,updated_at) VALUES('000001',9,100,1)")
        result = self.reader.portfolio()["data"]
        self.assertFalse(result["valuation_complete"])
        self.assertIsNone(result["account_equity"])
        self.assertIsNone(result["total_market_value"])
        self.assertIsNone(result["total_pnl_abs"])
        self.assertTrue(result["stale"])

    def test_event_pagination_has_no_gaps_and_does_not_mark_read(self):
        cursor, ids = 0, []
        while True:
            batch = self.reader.events(cursor, 2)["data"]
            ids.extend(event["id"] for event in batch["events"])
            self.assertTrue(all(event["read_at"] is None for event in batch["events"]))
            cursor = batch["next_after_id"]
            if not batch["has_more"]:
                break
        self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(self.reader.events(cursor)["data"]["next_after_id"], 5)

    def test_watchlist_pagination_and_root_env_reload(self):
        first = self.reader.watchlist(limit=1)["data"]
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(self.reader.watchlist(offset=1)["data"]["stocks"][0]["code"], "000001.SZ")
        self.env.write_text("COMMON_STOCK_CODES=588170\n")
        self.assertEqual(self.reader.watchlist()["data"]["stocks"][0]["code"], "588170.SH")
        for args in ((-1, 10), (0, 101)):
            with self.assertRaises(ValueError):
                self.reader.events(*args)


if __name__ == "__main__":
    unittest.main()
