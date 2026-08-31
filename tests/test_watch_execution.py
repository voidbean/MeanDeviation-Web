import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import core.db as db
import services.monitor as monitor
import services.calibration as calibration
from core.watch_execution import submit_feedback, undo_execution


class WatchExecutionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "execution.db")
        for module in (db, monitor, calibration):
            p = patch.object(module, "DB_PATH", self.path); p.start(); self.addCleanup(p.stop)
        db.init_db()
        db.save_available_cash(10000)
        db.set_watch_enabled("000001", True)
        self.day = "2026-08-19"
        self.make_plan()

    def sql(self, query, args=()):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(query, args).fetchall()

    def make_plan(self, rules=None, day=None):
        self.day = day or self.day
        db.save_watch_plans([{"code":"000001", "name":"测试", "rules": rules or [
            {"type":"breakout", "price":10, "priority":"opportunity", "message":"首次买入", "action":"entry"}]}], self.day)
        db.activate_watch_plans(self.day)
        self.rule_id = self.sql("SELECT id FROM watch_rules ORDER BY id DESC LIMIT 1")[0][0]
        self.sql("INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at) VALUES(?, '000001','测试','breakout','opportunity',10,'确认',?)",
                 (self.rule_id, self.day + " 10:00:00"))
        self.event_id = self.sql("SELECT MAX(id) FROM watch_events")[0][0]

    def payload(self, **kwargs):
        return {"action":"fill", "request_id":"test-request-0000001", "direction":"买入", "price":10,
                "quantity":100, "target_quantity":200, "fee":1, "trade_time":self.day + "T10:01:00", **kwargs}

    def fill(self, **kwargs):
        return submit_feedback(self.path, self.event_id, self.payload(**kwargs))

    def test_partial_then_complete_updates_one_account_atomically(self):
        first = self.fill()
        self.assertEqual(first["execution_status"], "partial")
        self.assertEqual(db.get_portfolio("000001")["quantity"], 100)
        self.assertEqual(db.get_available_cash(), 8999)
        self.assertEqual(db.get_portfolio("000001")["cost"], 10.01)
        second = self.fill(request_id="test-request-0000002")
        self.assertEqual(second["execution_status"], "completed")
        self.assertEqual(db.get_portfolio("000001")["quantity"], 200)
        self.assertEqual(len(self.sql("SELECT * FROM trade_log")), 2)
        event = db.get_recent_watch_events()[0]
        self.assertEqual(event["filled_quantity"], 200)
        self.assertEqual(event["last_execution_id"], second["execution_id"])
        with self.assertRaises(ValueError):
            self.fill(request_id="test-request-0000003")

    def test_duplicate_and_concurrent_retry_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.fill(), range(2)))
        self.assertEqual(results[0]["execution_id"], results[1]["execution_id"])
        self.assertEqual(db.get_portfolio("000001")["quantity"], 100)
        with self.assertRaises(ValueError):
            self.fill(price=11)

    def test_invalid_fill_rolls_back(self):
        for changes in ({"quantity":-1}, {"quantity":1.2}, {"quantity":True}, {"price":float('nan')},
                        {"quantity":300}, {"direction":"卖出"}, {"trade_time":"2099-01-01T10:00"},
                        {"price":1000}, {"fee":-1}):
            with self.assertRaises(ValueError): self.fill(**changes)
        self.assertEqual(db.get_available_cash(), 10000)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM trade_log")[0][0], 0)

    def test_database_failure_rolls_back_position_cash_and_status(self):
        self.sql("CREATE TRIGGER fail_trade BEFORE INSERT ON trade_log BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.fill()
        self.assertEqual(db.get_available_cash(), 10000)
        self.assertEqual(db.get_portfolio("000001")["quantity"], 0)
        self.assertEqual(self.sql("SELECT execution_status FROM watch_rules")[0][0], "pending")

    def test_undo_and_correct_with_audit(self):
        first = self.fill(target_quantity=100)
        undo_execution(self.path, first["execution_id"])
        undo_execution(self.path, first["execution_id"])
        self.assertEqual(db.get_portfolio("000001")["quantity"], 0)
        self.assertEqual(db.get_available_cash(), 10000)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM trade_log")[0][0], 0)
        self.assertTrue(self.fill(target_quantity=100)["voided"])
        corrected = self.fill(request_id="corrected-request-0001", price=9.9, target_quantity=100)
        self.assertEqual(corrected["execution_status"], "completed")

    def test_undo_refuses_to_overwrite_newer_changes(self):
        first = self.fill()
        second = self.fill(request_id="test-request-0000002")
        with self.assertRaises(ValueError): undo_execution(self.path, first["execution_id"])
        db.save_available_cash(12345)
        with self.assertRaises(ValueError): undo_execution(self.path, second["execution_id"])
        self.assertEqual(db.get_available_cash(), 12345)

    def test_sell_to_zero_disables_old_holding_actions_and_undo_restores(self):
        db.save_portfolio("000001", 8, 12, 7, 12, 100)
        self.make_plan(rules=[
            {"type":"near","price":9,"action":"add","message":"补仓"},
            {"type":"breakdown","price":8,"action":"exit","message":"止损","priority":"risk"}])
        result = self.fill(direction="卖出", target_quantity=100)
        self.assertEqual(db.get_portfolio("000001")["quantity"], 0)
        self.assertEqual(db.get_available_cash(), 10999)
        self.assertEqual(self.sql("SELECT execution_status FROM watch_rules ORDER BY id"), [("disabled",),("completed",)])
        undo_execution(self.path, result["execution_id"])
        self.assertEqual(db.get_portfolio("000001")["quantity"], 100)
        self.assertEqual(self.sql("SELECT execution_status FROM watch_rules ORDER BY id"), [("pending",),("pending",)])

    def test_sell_cannot_exceed_holdings(self):
        self.make_plan(rules=[{"type":"breakdown","price":8,"action":"exit","message":"止损"}])
        with self.assertRaises(ValueError): self.fill(direction="卖出")

    def test_completed_actions_survive_regeneration_edit_and_calibration(self):
        self.fill(target_quantity=100)
        self.assertEqual(db.save_watch_plans([{"code":"000001","rules":[{"type":"breakout","price":11}]}], self.day), 0)
        db.update_watch_rule(self.rule_id, 11, 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(calibration._rules(conn, self.sql("SELECT id FROM watch_plans")[0][0]), [])
        self.assertEqual(self.sql("SELECT execution_status FROM watch_rules")[0][0], "completed")

    def test_read_does_not_mean_executed(self):
        db.mark_watch_events_read()
        self.assertEqual(db.get_recent_watch_events()[0]["execution_status"], "pending")

    def test_feedback_snooze_ignore_disable_and_risk_protection(self):
        self.make_plan(day=dt.date.today().isoformat())
        submit_feedback(self.path, self.event_id, {"action":"snooze"})
        self.assertIsNotNone(db.get_recent_watch_events()[0]["snooze_until"])
        submit_feedback(self.path, self.event_id, {"action":"ignore"})
        self.assertEqual(db.get_recent_watch_events()[0]["ignore_until_recovery"], 1)
        self.sql("UPDATE watch_rules SET priority='risk' WHERE id=?", (self.rule_id,))
        with self.assertRaises(ValueError): submit_feedback(self.path, self.event_id, {"action":"snooze"})
        submit_feedback(self.path, self.event_id, {"action":"disable"})
        self.assertEqual(db.get_recent_watch_events()[0]["execution_status"], "disabled")

    def snap(self, minute, price):
        self.sql("INSERT INTO intraday_snapshots(code,date,time,price,open,vol,amount) VALUES('000001',?,?,?,9,100,1)",
                 (self.day, minute, price))

    def test_same_minute_and_gaps_do_not_count_as_continuous_minutes(self):
        self.sql("UPDATE watch_rules SET confirmation_minutes=2")
        self.snap("10:01", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:03", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:04", 11)
        self.assertEqual(len(monitor.evaluate_watch_rules(self.day)), 1)

    def test_completed_action_never_rearms(self):
        self.fill(target_quantity=100)
        self.snap("10:01", 9)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:02", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])

    def test_position_changes_prevent_repeated_initial_entry(self):
        db.save_portfolio("000001", 10, 0, 0, 10, 100)
        self.snap("10:01", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.assertEqual(self.sql("SELECT execution_status FROM watch_rules")[0][0], "pending")

    def test_migration_is_idempotent(self):
        self.fill()
        db.init_db()
        self.assertEqual(db.get_recent_watch_events()[0]["execution_status"], "partial")

    def test_risk_reentry_is_not_silently_merged(self):
        self.sql("UPDATE watch_rules SET priority='risk',action='observe'")
        self.snap("10:01", 11)
        first = monitor.evaluate_watch_rules(self.day)[0]
        self.snap("10:02", 9)
        self.assertEqual(monitor.evaluate_watch_rules(self.day)[0]["event_type"], "recovered")
        self.snap("10:03", 11)
        second = monitor.evaluate_watch_rules(self.day)[0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second.get("silent_update", False))

    def test_ignore_waits_for_recovery_before_new_alert(self):
        self.sql("UPDATE watch_rules SET ignore_until_recovery=1")
        self.snap("10:01", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:02", 9)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:03", 11)
        self.assertEqual(len(monitor.evaluate_watch_rules(self.day)), 1)

    def test_snooze_resumes_after_expiry(self):
        self.sql("UPDATE watch_rules SET snooze_until=?", (self.day + " 10:03:00",))
        self.snap("10:02", 11)
        self.assertEqual(monitor.evaluate_watch_rules(self.day), [])
        self.snap("10:03", 11)
        self.assertEqual(len(monitor.evaluate_watch_rules(self.day)), 1)

    def test_review_routes_cannot_edit_linked_fill(self):
        import asyncio
        from app import app
        from routes import review
        self.fill()
        trade_id = self.sql("SELECT id FROM trade_log")[0][0]
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/review/delete")
        with patch.object(review, "DB_PATH", self.path):
            result = asyncio.run(endpoint(request=None, trade_id=trade_id))
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM trade_log")[0][0], 1)


if __name__ == '__main__':
    unittest.main()
