"""Breakout continuation is observation, never a second trade confirmation."""
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core.watch_execution import submit_feedback
from services import monitor


class BreakoutFollowupTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "followup.db")
        for module in (db, monitor):
            p = patch.object(module, "DB_PATH", self.path)
            p.start(); self.addCleanup(p.stop)
        for name, value in (("build_watch_context", {
                "daily": {"macd": None, "as_of": None},
                "intraday": {"volume_ratio_3m_20m": None, "vwap": 113}}), ("build_market_context", {})):
            p = patch.object(monitor, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(monitor, "market_warning_text", return_value="")
        p.start(); self.addCleanup(p.stop)
        db.init_db()
        self.day = "2026-09-22"
        db.set_watch_enabled("603629", True)
        db.save_portfolio("603629", 115, 0, 0, 115, 500)
        db.save_available_cash(50000)
        db.save_watch_plans([{"code": "603629", "name": "利通电子", "rules": [{
            "type": "breakout", "price": 113.2899, "confirmation_minutes": 3,
            "priority": "opportunity", "action": "reduce", "message": "反抽上轨减仓",
            "indicator": "分时上轨113.2899",
        }]}], self.day)
        db.activate_watch_plans(self.day)

    def sql(self, query, args=()):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(query, args).fetchall()

    def tick(self, minute, price):
        self.sql("""INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount)
            VALUES('603629',?,?,?,112,118,111,1000,113)""", (self.day, minute, price))
        return monitor.evaluate_watch_rules(self.day)

    def trigger(self):
        self.assertEqual(self.tick("11:19", 113.74), [])
        self.assertEqual(self.tick("11:20", 113.70), [])
        event, = self.tick("11:21", 113.43)
        self.assertEqual(event["event_type"], "breakout")
        return event

    def followup(self):
        self.assertEqual(self.tick("13:20", 114.85), [])
        self.assertEqual(self.tick("13:21", 115.5), [])
        event, = self.tick("13:22", 116)
        self.assertEqual(event["event_type"], "breakout_followup")
        return event

    def test_litong_continues_after_two_minute_pullback_without_rearming(self):
        original = self.trigger()
        for minute, price in (("13:00", 113.02), ("13:01", 112.76), ("13:02", 113.48)):
            self.assertEqual(self.tick(minute, price), [])
        event = self.followup()
        self.assertEqual(event["priority"], "observe")
        self.assertTrue(event["observation_only"])
        self.assertIn("实际执行情况未知", event["message"])
        self.assertIn("账面持仓 500 股", event["message"])
        self.assertEqual(self.sql("SELECT state,execution_status,threshold,active_event_id FROM watch_rules"),
                         [("triggered", "pending", 113.2899, original["id"])])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM watch_executions"), [(0,)])
        for action in ("fill", "snooze", "ignore", "disable"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "状态观察"):
                submit_feedback(self.path, event["id"], {"action": action, "request_id": "followup-cannot-fill"})
        self.assertEqual(db.get_recent_watch_events()[0]["event_type"], "breakout_followup")

    def test_repeat_poll_and_restart_do_not_repeat_alert(self):
        self.trigger(); self.followup()
        for _ in range(3):
            self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        db.init_db()  # Restart/migration must preserve cursor, reference and cooldown.
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        for minute in ("13:23", "13:24", "13:25"):
            self.assertEqual(self.tick(minute, 116), [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM watch_events WHERE event_type='breakout_followup'"), [(1,)])

    def test_recorded_litong_afternoon_gap_delays_followup_until_1325(self):
        self.trigger()
        for minute, price in (("13:19", 113.99), ("13:20", 114.85), ("13:21", 115.62),
                              ("13:23", 116.61), ("13:24", 117.58)):
            self.assertEqual(self.tick(minute, price), [])
        with patch.object(monitor, "_publish") as publish:
            event, = self.tick("13:25", 115.8)
        self.assertEqual(event["event_type"], "breakout_followup")
        self.assertEqual(event["price"], 115.8)
        publish.assert_called_once_with(event)

    def test_price_progress_and_cooldown_are_both_required(self):
        self.trigger(); self.followup()
        for minute in ("13:23", "13:24", "13:25"):
            self.assertEqual(self.tick(minute, 118), [])  # Advance, but cooling.
        for minute in ("13:35", "13:36"):
            self.assertEqual(self.tick(minute, 118), [])
        event, = self.tick("13:37", 118)
        self.assertIn("跟踪基准 116", event["message"])
        for minute in ("14:00", "14:01", "14:02"):
            self.assertEqual(self.tick(minute, 118), [])  # Time alone is insufficient.

    def test_gaps_and_lunch_reset_confirmation(self):
        self.trigger()
        for minute in ("11:28", "11:29", "13:00", "13:02", "13:03"):
            self.assertEqual(self.tick(minute, 116), [])
        event, = self.tick("13:04", 116)
        self.assertEqual(event["event_type"], "breakout_followup")

    def test_interrupted_price_advance_resets_confirmation(self):
        self.trigger()
        for minute, price in (("13:20", 116), ("13:21", 114), ("13:22", 116), ("13:23", 116)):
            self.assertEqual(self.tick(minute, price), [])
        self.assertEqual(self.tick("13:24", 116)[0]["event_type"], "breakout_followup")

    def test_original_recovery_and_retrigger_reset_followup_baseline(self):
        self.trigger(); self.followup()
        for minute in ("13:23", "13:24"):
            self.assertEqual(self.tick(minute, 112), [])
        self.assertEqual(self.tick("13:25", 112)[0]["event_type"], "recovered")
        self.assertEqual(self.tick("13:26", 114), [])
        self.assertEqual(self.tick("13:27", 114), [])
        again, = self.tick("13:28", 114)
        self.assertEqual(again["repeat_count"], 2)  # Original opportunity still merges.
        self.assertEqual(self.sql("SELECT followup_reference_price,followup_notified_at FROM watch_rules"), [(114, None)])
        self.assertEqual(self.tick("13:29", 115.5), [])
        self.assertEqual(self.tick("13:30", 115.5), [])
        self.assertIn("跟踪基准 114", self.tick("13:31", 115.5)[0]["message"])

    def test_completed_entry_still_observed_even_with_no_t1_sellable_shares(self):
        self.sql("UPDATE watch_rules SET action='entry',message='首次建仓'")
        self.sql("UPDATE portfolio SET quantity=0")
        original = self.trigger()
        submit_feedback(self.path, original["id"], {"action": "fill", "request_id": "completed-entry-followup",
            "direction": "买入", "price": 113.43, "quantity": 100, "trade_time": self.day + "T11:22:00"})
        # The observation path must accept a T+1-locked snapshot regardless of
        # how the account provider derives sellability from its trade ledger.
        with patch("core.watch_execution.get_tplus1_position_snapshot", return_value={
                "holding": 100, "today_bought": 100, "sellable_without_tplus1": 0}):
            event = self.followup()
        self.assertEqual(event["execution_status"], "completed")
        self.assertEqual(event["sellable_without_tplus1"], 0)
        self.assertIn("已记录动作完成", event["message"])

    def test_observation_failure_does_not_rollback_trade_confirmation(self):
        self.assertEqual(self.tick("11:19", 113.74), [])
        self.assertEqual(self.tick("11:20", 113.70), [])
        with patch.object(monitor, "_evaluate_breakout_followups", side_effect=ValueError("test")), \
                self.assertLogs(level="ERROR"):
            event, = self.tick("11:21", 113.43)
        self.assertEqual(event["event_type"], "breakout")
        self.assertEqual(self.sql("SELECT state FROM watch_rules"), [("triggered",)])

    def test_partial_execution_is_not_mistaken_for_no_execution(self):
        original = self.trigger()
        submit_feedback(self.path, original["id"], {"action": "fill", "request_id": "partial-reduce-followup",
            "direction": "卖出", "price": 113.43, "quantity": 100, "target_quantity": 200,
            "trade_time": self.day + "T11:22:00"})
        event = self.followup()
        self.assertEqual(event["execution_status"], "partial")
        self.assertIn("账面持仓 400 股", event["message"])

    def test_disabled_paused_ignored_snoozed_and_empty_position_do_not_emit(self):
        self.trigger()
        cases = ["UPDATE watch_rules SET execution_status='disabled'",
                 "UPDATE watch_rules SET paused=1", "UPDATE watch_rules SET ignore_until_recovery=1",
                 "UPDATE watch_rules SET snooze_until='2026-09-22 15:00:00'",
                 "UPDATE portfolio SET quantity=0", "UPDATE stock_watchlist SET enabled=0",
                 "UPDATE watch_plans SET status='expired'"]
        for i, query in enumerate(cases):
            with self.subTest(query=query):
                self.sql("UPDATE watch_rules SET execution_status='pending',paused=0,ignore_until_recovery=0,snooze_until=NULL")
                self.sql("UPDATE portfolio SET quantity=500")
                self.sql("UPDATE stock_watchlist SET enabled=1")
                self.sql("UPDATE watch_plans SET status='active'")
                self.sql(query)
                for offset in range(3):
                    self.assertEqual(self.tick(f"13:{i * 4 + offset:02d}", 116), [])

    def test_upgrade_uses_fresh_baseline_not_old_merged_event_price(self):
        self.trigger()
        self.sql("UPDATE watch_rules SET followup_reference_price=NULL,followup_snapshot_time=NULL")
        for minute in ("13:20", "13:21", "13:22"):
            self.assertEqual(self.tick(minute, 116), [])
        self.assertEqual(self.sql("SELECT followup_reference_price FROM watch_rules"), [(116,)])
        self.assertEqual(self.tick("13:23", 118), [])
        self.assertEqual(self.tick("13:24", 118), [])
        self.assertEqual(self.tick("13:25", 118)[0]["event_type"], "breakout_followup")

    def test_stale_snapshot_does_not_advance_followup_cursor(self):
        self.trigger()
        real_datetime = dt.datetime
        class Clock(real_datetime):
            @classmethod
            def now(cls):
                return cls(2026, 9, 22, 13, 10)
        with patch.object(monitor.dt, "datetime", Clock), patch.object(monitor.dt, "date") as date:
            date.today.return_value = real_datetime(2026, 9, 22).date()
            self.assertEqual(self.tick("13:00", 116), [])
        self.assertEqual(self.sql("SELECT followup_snapshot_time FROM watch_rules"), [("11:21",)])


if __name__ == "__main__":
    unittest.main()
