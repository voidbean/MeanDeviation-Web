import datetime as dt
import unittest
from unittest.mock import patch

from routes import main


class _Frame:
    def __init__(self, records):
        self.records = records

    def to_dict(self, orient):
        assert orient == "records"
        return self.records


class _Pro:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error

    def trade_cal(self, **kwargs):
        if self.error:
            raise self.error
        return _Frame(self.records)


class TradeCalendarTest(unittest.TestCase):
    def test_watch_plan_trade_date_supports_today_and_next(self):
        today = dt.date(2026, 8, 21)
        with patch.object(main, "_next_trade_date", return_value=dt.date(2026, 8, 24)):
            self.assertEqual(main._watch_plan_trade_date("today", today), today)
            self.assertEqual(main._watch_plan_trade_date("next", today), dt.date(2026, 8, 24))
        with self.assertRaisesRegex(ValueError, "计划日期参数无效"):
            main._watch_plan_trade_date("2026-08-22", today)

    def setUp(self):
        main._TRADE_DATE_CACHE.clear()

    def test_uses_next_open_day_and_skips_holiday(self):
        pro = _Pro([
            {"cal_date": "20261001", "is_open": 0},
            {"cal_date": "20261008", "is_open": 1},
            {"cal_date": "20261009", "is_open": 1},
        ])
        with patch.object(main._cfg, "pro", pro):
            self.assertEqual(main._next_trade_date(dt.date(2026, 9, 30)), dt.date(2026, 10, 8))

    def test_falls_back_to_weekday_when_api_fails(self):
        with patch.object(main._cfg, "pro", _Pro(error=RuntimeError("offline"))):
            self.assertEqual(main._next_trade_date(dt.date(2026, 8, 21)), dt.date(2026, 8, 24))


if __name__ == "__main__":
    unittest.main()
