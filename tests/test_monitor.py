import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.db as db
import services.monitor as monitor


class MonitorRuleTest(unittest.TestCase):
    def test_effective_threshold_distinguishes_average_from_bands(self):
        for label in ("分时均价", "黄线", "分时均价114.305", "分时均价（黄线）"):
            with self.subTest(label=label):
                rule = {"indicator_label": label, "message": "", "threshold": 106.8616,
                        "rule_type": "breakdown"}
                self.assertEqual(monitor._effective_threshold(rule, 114.305), 114.305)
                self.assertEqual(monitor._effective_threshold(rule, None), 106.8616)
        for label in ("分时均价下轨106.8616", "分时均价上轨", "黄线下轨", "黄线上轨",
                      "分时均价下方2%", "MA5"):
            for explicit in (True, False):
                # Legacy rows have no indicator label; band inference must not
                # discard the suffix and turn the band back into the average.
                with self.subTest(label=label, explicit=explicit):
                    rule = {"indicator_label": label if explicit else "",
                            "message": f"跌破{label}执行止损离场", "threshold": 106.8616,
                            "rule_type": "breakdown"}
                    self.assertEqual(monitor._effective_threshold(rule, 114.305), 106.8616)
                    self.assertIn("106.862｜当前价", monitor._evidence(rule, 113.69, 114.305))

    def test_lower_band_does_not_trigger_below_average_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True)
                db.save_portfolio("000001", 110, 0, 0, 115, 100)
                db.save_watch_plans([{
                    "code": "000001", "name": "测试股", "rules": [{
                        "type": "breakdown", "price": 106.8616,
                        "indicator": "分时均价下轨106.8616", "confirmation_minutes": 2,
                        "priority": "risk", "action": "exit",
                        "message": "跌破分时均价下轨106.86执行止损离场",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")
                with sqlite3.connect(path) as conn:
                    conn.execute(
                        "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("000001", "2026-08-19", "11:11", 113.69, 115, 115, 113, 1000, 114.305),
                    )
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "11:12")
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute("SELECT consecutive_hits FROM watch_rules").fetchone()[0], 0)
                self.advance(path, "11:13", 106.8)
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "11:14", 106.8)
                events = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(events), 1)
                self.assertIn("建议卖出/止损", events[0]["message"])
                self.assertIn("106.862｜当前价 106.8", events[0]["message"])

    def advance(self, path, minute, price=None):
        conn = sqlite3.connect(path)
        row = list(conn.execute("SELECT code,date,time,price,open,high,low,vol,amount FROM intraday_snapshots ORDER BY time DESC LIMIT 1").fetchone())
        row[2] = minute
        if price is not None: row[3] = price
        conn.execute("INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)", row)
        conn.commit(); conn.close()

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
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])  # 同分钟不能重复计数
                self.advance(path, "09:32")
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
                self.advance(path, "10:02")
                first = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(first), 1)
                self.advance(path, "10:03", 10.01)
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "10:04", 10.03)
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "10:05", 10.03)
                recovered = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(recovered[0]["event_type"], "recovered")
                self.assertIn("提醒解除", recovered[0]["message"])
                self.assertIn("无需成交操作", recovered[0]["message"])
                self.assertNotIn("额外检查", recovered[0]["message"])
                self.assertNotIn("。。", recovered[0]["message"])
                self.advance(path, "10:06", 9.97)
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "10:07", 9.97)
                repeated = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(repeated), 1)
                self.assertEqual(repeated[0]["id"], first[0]["id"])
                self.assertTrue(repeated[0]["silent_update"])

    def test_near_buy_is_observation_then_confirms_above_dynamic_average(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True); db.save_available_cash(50_000)
                db.save_portfolio("000001", 10, 0, 0, 10, 100)  # 补仓规则需要已有持仓。
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
                self.advance(path, "10:02")
                observed = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(observed), 1)
                self.assertIn("尚未形成操作确认", observed[0]["message"])
                self.assertIn("分时均价 10", observed[0]["message"])

                # 用真正连续的两个分钟确认，而非重复轮询一个快照。
                self.advance(path, "10:03", 10.03)
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])
                self.advance(path, "10:04", 10.03)
                confirmed = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(confirmed[0]["event_type"], "action_confirmed")
                self.assertIn("建议买入/补仓", confirmed[0]["message"])

    def test_near_observe_never_turns_negative_buy_wording_into_add(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True); db.save_available_cash(50_000)
                db.save_watch_plans([{
                    "code": "000001", "name": "测试股", "rules": [{
                        "type": "near", "price": 10, "confirmation_minutes": 1,
                        "priority": "observe", "action": "add",
                        "message": "到支撑附近仅观察是否止跌，不主动补仓",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("000001", "2026-08-19", "10:01", 10.0, 10, 10.1, 9.9, 1000, 10),
                ); conn.commit(); conn.close()

                events = monitor.evaluate_watch_rules("2026-08-19")
                self.assertEqual(len(events), 1)
                self.assertIn("已到达观察区", events[0]["message"])
                self.assertNotIn("建议买入", events[0]["message"])
                self.assertNotIn("最多可买", events[0]["message"])
                conn = sqlite3.connect(path)
                action, state = conn.execute("SELECT action,state FROM watch_rules").fetchone()
                conn.close()
                self.assertEqual((action, state), ("observe", "triggered"))

    def test_exit_rules_blocked_when_no_sellable_tplus1_shares(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "monitor.db")
            with patch.object(db, "DB_PATH", path), patch.object(monitor, "DB_PATH", path):
                db.init_db(); db.set_watch_enabled("000001", True)
                db.save_watch_plans([{
                    "code":"000001", "name":"测试股", "rules":[{
                        "type":"breakdown", "price":10, "priority":"risk", "action":"exit", "message":"T+1止损",
                    }],
                }], "2026-08-19")
                db.activate_watch_plans("2026-08-19")
                db.save_portfolio("000001", 10, 0, 0, 10, 100)
                with sqlite3.connect(path) as conn:
                    conn.execute(
                        "INSERT INTO trade_log(code,name,trade_time,direction,price,volume,thought,emotion)"
                        " VALUES(?,?,?,?,?,?,?,'冷静')",
                        ("000001", "测试股", "2026-08-19 10:00:00", "买入", 10, 100, "同日买入"),
                    )
                    conn.execute(
                        "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("000001", "2026-08-19", "10:01", 9.5, 10, 10.2, 9.4, 100, 1),
                    )
                    conn.commit()
                self.assertEqual(monitor.evaluate_watch_rules("2026-08-19"), [])



if __name__ == "__main__":
    unittest.main()
