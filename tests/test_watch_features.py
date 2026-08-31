import asyncio
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.db as db
import services.monitor as monitor
import services.calibration as calibration
from core.watch_conditions import validate_conditions
from services.watch_features import build_watch_context, intraday_context, evaluate_conditions


class WatchFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "watch.db")
        for module in (db, monitor, calibration):
            patcher = patch.object(module, "DB_PATH", self.path)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.day = "2026-08-19"
        self.condition = {"metric": "volume_ratio_3m_20m", "op": "gte", "value": 3}

    def daily(self):
        start = dt.date(2026, 7, 1)
        for i in range(50):
            self.conn.execute(
                "INSERT INTO daily_records(date,code,close,open,high,low,vol) VALUES(?,?,?,?,?,?,?)",
                ((start + dt.timedelta(days=i)).isoformat(), "000001", 10 + i / 10, 10, 16, 9, 1000),
            )
        self.conn.commit()

    def minutes(self, gap=False):
        start = dt.datetime(2026, 8, 19, 10)
        for i in range(24):
            vol = 100000 if i == 0 else 100 if i < 21 else 200
            minute = start + dt.timedelta(minutes=i + int(gap and i >= 12))
            self.conn.execute(
                "INSERT INTO intraday_snapshots(code,date,time,price,open,vol,amount) VALUES(?,?,?,?,?,?,?)",
                ("000001", self.day, minute.strftime("%H:%M"), 10.1, 9.8, vol, vol * 10 / 1000),
            )
        self.conn.commit()

    def plan(self, priority="opportunity", conditions=None, kind="breakout"):
        db.set_watch_enabled("000001", True)
        db.save_watch_plans([{"code": "000001", "rules": [{
            "type": kind, "price": 10, "priority": priority, "confirmation_minutes": 1,
            "message": "观察买入" if priority != "risk" else "止损",
            "conditions": [self.condition] if conditions is None else conditions,
        }]}], self.day)
        db.activate_watch_plans(self.day)

    def test_closed_daily_cutoff_and_macd(self):
        self.daily()
        ctx = build_watch_context(self.conn, "000001", "2026-08-20", dt.datetime(2026, 8, 19, 10))
        self.assertEqual(ctx["daily"]["as_of"], "2026-08-18")
        self.assertEqual(ctx["daily"]["macd"]["period"], "closed_daily")
        self.assertGreater(ctx["daily"]["macd"]["hist"], 0)
        self.assertEqual(ctx["daily"]["volume_ratio_1d_20d"], 1)
        # At close, today's bar is eligible; replay for today's plan still excludes it.
        after = build_watch_context(self.conn, "000001", "2026-08-20", dt.datetime(2026, 8, 19, 16))
        self.assertEqual(after["daily"]["as_of"], self.day)
        replay = build_watch_context(self.conn, "000001", self.day, dt.datetime(2026, 8, 21, 16))
        self.assertEqual(replay["daily"]["as_of"], "2026-08-18")

    def test_volume_uses_delta_window_not_first_accumulated_snapshot(self):
        self.minutes()
        result = intraday_context(self.conn, "000001", self.day)
        self.assertEqual(result["volume_ratio_3m_20m"], 2)
        self.assertAlmostEqual(result["vwap"], 10)

    def test_gap_and_lunch_are_unknown(self):
        self.minutes(gap=True)
        self.assertIsNone(intraday_context(self.conn, "000001", self.day)["volume_ratio_3m_20m"])
        self.conn.execute("UPDATE intraday_snapshots SET time='13:01' WHERE time='10:24'")
        self.assertIsNone(intraday_context(self.conn, "000001", self.day)["volume_ratio_3m_20m"])

    def test_missing_old_volume_is_not_invented(self):
        self.daily()
        self.conn.execute("UPDATE daily_records SET vol=NULL")
        ctx = build_watch_context(self.conn, "000001", self.day)
        self.assertIsNone(ctx["daily"]["volume_ratio_1d_20d"])
        result = evaluate_conditions([self.condition], ctx, 10, self.day)
        self.assertEqual(result["status"], "unknown")

    def test_macd_stale_and_insufficient_history_unknown(self):
        condition = {"metric": "daily_macd_hist", "op": "gte", "value": 0}
        ctx = build_watch_context(self.conn, "000001", self.day)
        self.assertEqual(evaluate_conditions([condition], ctx, 10, self.day)["status"], "unknown")
        self.daily()
        ctx = build_watch_context(self.conn, "000001", self.day)
        self.assertEqual(evaluate_conditions([condition], ctx, 10, "2026-09-01")["status"], "unknown")

    def test_comparison_and_all_conditions_required(self):
        self.minutes()
        ctx = build_watch_context(self.conn, "000001", self.day)
        conditions = [dict(self.condition, op="lte", value=2),
                      {"metric": "price_vs_vwap", "op": "gte", "value": 1}]
        self.assertEqual(evaluate_conditions(conditions, ctx, 10, self.day)["status"], "met")
        self.assertEqual(evaluate_conditions(conditions, ctx, 9, self.day)["status"], "unmet")
        self.assertEqual(evaluate_conditions([], ctx, 10, self.day)["status"], "not_configured")

    def test_invalid_conditions_rejected_without_replacing_plan(self):
        self.plan()
        for invalid in ([dict(self.condition, metric="magic")], [dict(self.condition, value=float("nan"))],
                        [dict(self.condition, value=True)], [self.condition, self.condition], "bad"):
            with self.assertRaises(ValueError):
                validate_conditions(invalid)
            self.assertEqual(db.save_watch_plans([{"code": "000001", "rules": [{
                "type": "breakout", "price": 11, "conditions": invalid}]}], self.day), 0)
        loaded = db.get_watch_plans(self.day)[0]
        self.assertEqual(loaded["status"], "active")
        self.assertEqual(loaded["rules"][0]["conditions"], [self.condition])

    def test_risk_conditions_rejected(self):
        self.assertEqual(db.save_watch_plans([{"code": "000001", "rules": [{
            "type": "breakdown", "price": 10, "priority": "risk", "conditions": [self.condition]}]}], self.day), 0)

    def test_shadow_does_not_block_and_audit_is_idempotent(self):
        self.plan()
        self.minutes()
        events = monitor.evaluate_watch_rules(self.day)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["shadow_result"]["status"], "unmet")
        self.assertIn("未满足", events[0]["message"])
        monitor.evaluate_watch_rules(self.day)
        row = self.conn.execute("SELECT COUNT(*),MAX(legacy_confirmed) FROM watch_shadow_checks").fetchone()
        self.assertEqual(row, (1, 1))
        self.assertEqual(db.get_watch_plans(self.day)[0]["rules"][0]["shadow_result"]["status"], "unmet")
        self.assertIn("不拦截原规则", db.get_recent_watch_events()[0]["message"])

    def test_technical_failure_does_not_block_risk_exit(self):
        self.plan(priority="risk", conditions=[], kind="breakdown")
        self.minutes()
        self.conn.execute("UPDATE intraday_snapshots SET price=9.8")
        self.conn.commit()
        with patch.object(monitor, "build_watch_context", side_effect=ValueError("bad data")):
            events = monitor.evaluate_watch_rules(self.day)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["priority"], "risk")
        self.assertIn("数据不足", events[0]["message"])

    def test_daily_volume_upsert_keeps_units_and_unknown(self):
        from fetch_history import upsert_daily_record, ensure_tables
        ensure_tables(self.conn)
        upsert_daily_record(self.conn, self.day, "000001", "测试", 10, 11, 9, 10, vol=12300)
        upsert_daily_record(self.conn, self.day, "000001", "测试", 10, 11, 9, 10)
        self.assertEqual(self.conn.execute("SELECT vol FROM daily_records").fetchone()[0], 12300)

    def test_day_vwap_is_not_last_minute_average(self):
        self.minutes()
        self.conn.execute("UPDATE intraday_snapshots SET amount=4 WHERE time='10:23'")
        self.conn.commit()
        expected = (1026 + 2) * 1000 / 102600
        self.assertAlmostEqual(monitor._intraday_avg(self.conn, "000001", self.day), expected)

    def test_calibration_receives_context_and_conditions(self):
        self.plan()
        self.minutes()
        self.daily()
        with patch.object(calibration, "call_ai_model", return_value="[]") as ai:
            calibration.run_ai_calibration(self.day, "10:00")
        payload = json.loads(ai.call_args.args[1])[0]
        self.assertIn("macd", payload["technical_context"]["daily"])
        self.assertEqual(payload["technical_context"]["intraday"]["volume_ratio_3m_20m"], 2)
        self.assertEqual(payload["rules"][0]["conditions"], [self.condition])

    def test_generation_passes_technical_context(self):
        from app import app
        from routes import main
        endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/watch_plans/generate")
        self.daily()
        data = {"batch_results": [{"code": "000001", "current_price": 10}]}
        with patch.object(main, "DB_PATH", self.path), \
             patch.object(main, "load_temp_result", return_value=data), \
             patch.object(main, "save_temp_result"), \
             patch.object(main, "get_watch_enabled_map", return_value={"000001": True}), \
             patch.object(main, "_watch_plan_trade_date", return_value=dt.date(2026, 8, 19)), \
             patch.object(main, "call_ai_model", return_value='[{"code":"000001","rules":[]}]') as ai, \
             patch.object(main, "save_watch_plans", return_value=1):
            asyncio.run(endpoint(target="next"))
        payload = json.loads(ai.call_args.args[1].split("\n", 1)[1])[0]
        self.assertIsNotNone(payload["technical_context"]["daily"]["macd"])
        self.assertIn("conditions", ai.call_args.args[0])

    def test_migration_is_idempotent_and_template_parses(self):
        from jinja2 import Environment, FileSystemLoader
        db.init_db()
        self.plan()
        env = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "templates")))
        template = env.get_template("batch.html")
        rendered = template.render(watch_plans=db.get_watch_plans(self.day), today_plans=db.get_watch_plans(self.day),
                                   available_cash=0, market_indices=[], monitor_health={})
        self.assertIn("影子条件", rendered)


if __name__ == "__main__":
    unittest.main()
