import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.db as db
import services.monitor as monitor


class MonitorRuleTest(unittest.TestCase):
    def test_breakout_requires_confirmation_and_only_triggers_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db()
                db.set_watch_enabled("000001", True)
                db.save_watch_plans([{
                    "code": "000001", "name": "测试股", "rules": [{
                        "type": "breakout", "price": 10, "confirmation_minutes": 2,
                        "priority": "opportunity", "message": "确认突破",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")

                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("000001", "2026-08-19", "09:31", 10.1, 9.8, 10.1, 9.8, 100, 1),
                )
                conn.commit()
                conn.close()

                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                events = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["message"], "确认突破")
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])


if __name__ == "__main__":
    unittest.main()
