import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.db as db
import services.calibration as calibration


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "calibration.db")
        self.db_patch = patch.object(db, "DB_PATH", self.path)
        self.cal_patch = patch.object(calibration, "DB_PATH", self.path)
        self.db_patch.start(); self.cal_patch.start()
        db.init_db()
        db.set_watch_enabled("000001", True)
        db.save_watch_plans([{
            "code": "000001", "name": "测试股", "_current_price": 10,
            "rules": [
                {"type": "breakout", "price": 10.5, "priority": "opportunity", "confirmation_minutes": 3},
                {"type": "breakdown", "price": 9.5, "priority": "risk", "confirmation_minutes": 2},
            ],
        }], "2026-08-19")
        db.activate_watch_plans("2026-08-19")

    def tearDown(self):
        self.cal_patch.stop(); self.db_patch.stop(); self.folder.cleanup()

    def _insert_market(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO daily_records(date,code,name,close,high,low,avg_price,open) VALUES(?,?,?,?,?,?,?,?)",
            ("2026-08-18", "000001", "测试股", 10, 10, 9.8, 10, 9.9),
        )
        conn.execute(
            "INSERT INTO intraday_snapshots(code,date,time,price,open,high,low,vol,amount) VALUES(?,?,?,?,?,?,?,?,?)",
            ("000001", "2026-08-19", "09:35", 10.45, 10.5, 10.6, 10.4, 100, 1),
        )
        conn.commit(); conn.close()

    def test_open_gap_pauses_only_opportunity_rules(self):
        self._insert_market()
        self.assertEqual(calibration.run_open_calibration("2026-08-19"), 1)
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT priority,paused FROM watch_rules ORDER BY id").fetchall()
        revisions = conn.execute("SELECT decision FROM watch_plan_revisions").fetchall()
        conn.close()
        self.assertEqual(rows, [("opportunity", 1), ("risk", 0)])
        self.assertEqual(revisions, [("pause_opportunity",)])

    def test_risk_threshold_cannot_move_down(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        plan = conn.execute("SELECT * FROM watch_plans").fetchone()
        risk_id = conn.execute("SELECT id FROM watch_rules WHERE priority='risk'").fetchone()[0]
        calibration._apply_ai_decision(conn, plan, {
            "decision": "tighten_risk", "reason": "测试",
            "adjustments": [{"rule_id": risk_id, "action": "update", "threshold": 9.4}],
        }, "10:00")
        conn.commit()
        value = conn.execute("SELECT threshold FROM watch_rules WHERE id=?", (risk_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(value, 9.5)

    def test_recoverable_pause_resumes_but_invalid_is_terminal(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        plan = conn.execute("SELECT * FROM watch_plans").fetchone()
        opportunity_id = conn.execute("SELECT id FROM watch_rules WHERE priority='opportunity'").fetchone()[0]
        calibration._apply_ai_decision(conn, plan, {
            "decision": "pause_opportunity", "reason": "板块短暂转弱", "adjustments": [],
        }, "10:00", market_price=10)
        conn.commit()
        paused = conn.execute("SELECT state,paused FROM watch_rules WHERE id=?", (opportunity_id,)).fetchone()
        self.assertEqual(tuple(paused), ("suspended", 1))

        plan = conn.execute("SELECT * FROM watch_plans").fetchone()
        event = calibration._apply_ai_decision(conn, plan, {
            "decision": "continue", "reason": "板块重新走强", "adjustments": [],
        }, "13:05", market_price=10.1)
        conn.commit()
        resumed = conn.execute("SELECT state,paused FROM watch_rules WHERE id=?", (opportunity_id,)).fetchone()
        self.assertEqual(tuple(resumed), ("waiting", 0))
        self.assertIn("校准恢复", event["message"])

        plan = conn.execute("SELECT * FROM watch_plans").fetchone()
        calibration._apply_ai_decision(conn, plan, {
            "decision": "invalidate", "reason": "有效时间窗口结束",
            "adjustments": [{"rule_id": opportunity_id, "action": "pause"}],
        }, "14:35", market_price=10.2)
        conn.commit()
        invalid = conn.execute("SELECT state,paused,pause_source FROM watch_rules WHERE id=?", (opportunity_id,)).fetchone()
        self.assertEqual(tuple(invalid), ("invalid", 1, "permanent"))

        plan = conn.execute("SELECT * FROM watch_plans").fetchone()
        calibration._apply_ai_decision(conn, plan, {
            "decision": "continue", "reason": "价格反弹", "adjustments": [],
        }, "14:36", market_price=10.3)
        conn.commit()
        still_invalid = conn.execute("SELECT state,paused FROM watch_rules WHERE id=?", (opportunity_id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(still_invalid), ("invalid", 1))

    def test_calibration_slot_claim_is_idempotent(self):
        self.assertTrue(calibration._claim_run("2026-08-19", "10:00"))
        self.assertFalse(calibration._claim_run("2026-08-19", "10:00"))

    def test_close_calibration_receives_position_and_cash_context(self):
        self._insert_market()
        db.save_portfolio("000001", 9.8, 11, 9, 10.6, 200)
        db.save_available_cash(2500)
        captured = {}

        def fake_call(system_prompt, user_prompt):
            captured["system"] = system_prompt
            captured["payload"] = __import__("json").loads(user_prompt)
            return '[{"code":"000001","decision":"tighten_risk","reason":"尾盘减仓风控","adjustments":[]}]'

        with patch.object(calibration, "load_skills", return_value=""), \
             patch.object(calibration, "call_ai_model", side_effect=fake_call):
            self.assertEqual(calibration.run_ai_calibration("2026-08-19", "14:35"), 1)

        item = captured["payload"][0]
        self.assertTrue(item["position"]["holding"])
        self.assertEqual(item["position"]["quantity"], 200)
        self.assertEqual(item["account"]["available_cash"], 2500)
        self.assertEqual(item["account"]["max_buy_lots_at_current_price"], 2)
        self.assertIn("尾盘隔夜决策", captured["system"])
        conn = sqlite3.connect(self.path)
        event = conn.execute(
            "SELECT event_type,priority,message FROM watch_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(event[0], "calibration")
        self.assertEqual(event[1], "risk")
        self.assertIn("14:35 校准", event[2])


if __name__ == "__main__":
    unittest.main()
