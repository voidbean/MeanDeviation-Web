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
                self.assertIn("已连续 2 分钟确认", events[0]["message"])
                self.assertIn("计划关键线 10", events[0]["message"])
                self.assertIn("当前分时均价 10.000", events[0]["message"])
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])

    def test_breakdown_recovers_with_hysteresis_and_can_trigger_again(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True)
                db.save_watch_plans([{
                    "code": "000001", "name": "测试股", "rules": [{
                        "type": "breakdown", "price": 10, "confirmation_minutes": 2,
                        "priority": "opportunity", "message": "跌破后暂停",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("000001", "2026-08-19", "10:01", 9.98, 10.1, 10.1, 9.98, 100, 1),
                ); conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.assertEqual(len(monitor.evaluate_watch_rules("2026-08-19")), 1)

                # 仅贴线站回不满足 0.2% 滞回要求，不发恢复通知。
                conn = sqlite3.connect(path)
                conn.execute("UPDATE intraday_snapshots SET price=10.01 WHERE code='000001'")
                conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])

                conn = sqlite3.connect(path)
                conn.execute("UPDATE intraday_snapshots SET price=10.03 WHERE code='000001'")
                conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                recovered = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(recovered[0]["event_type"], "recovered")

                conn = sqlite3.connect(path)
                conn.execute("UPDATE intraday_snapshots SET price=9.97 WHERE code='000001'")
                conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.assertEqual(len(monitor.evaluate_watch_rules("2026-08-19")), 1)

    def test_near_buy_is_observation_then_confirms_above_dynamic_average(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True)
                db.save_watch_plans([{
                    "code": "000001", "name": "测试股", "rules": [{
                        "type": "near", "price": 10, "indicator": "分时均价",
                        "confirmation_minutes": 2, "priority": "opportunity",
                        "message": "回踩均价不破可补仓",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("000001", "2026-08-19", "10:01", 10.01, 10, 10.1, 9.9, 1000, 10),
                ); conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                observed = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(observed), 1)
                self.assertIn("尚未形成操作确认", observed[0]["message"])
                self.assertIn("分时均价 10", observed[0]["message"])

                # 均价仍为 10，价格站到均价上方 0.2% 后再连续确认两次。
                conn = sqlite3.connect(path)
                conn.execute("UPDATE intraday_snapshots SET price=10.03 WHERE code='000001'")
                conn.commit(); conn.close()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                confirmed = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(confirmed[0]["event_type"], "action_confirmed")
                self.assertIn("建议买入/补仓", confirmed[0]["message"])



if __name__ == "__main__":
    unittest.main()
