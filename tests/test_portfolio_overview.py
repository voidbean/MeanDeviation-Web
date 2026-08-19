import asyncio
import json
import unittest
from unittest.mock import patch

import pandas as pd
import tushare as ts

from app import app
from routes import main


class PortfolioOverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/portfolio_overview")

    def setUp(self):
        main._invalidate_portfolio_cache()

    def test_quotes_are_batched_and_second_request_uses_cache(self):
        holdings = [
            {"code": "000001", "cost": 9.0, "max_price": 0, "name": "平安银行", "quantity": 100},
            {"code": "600519", "cost": 1400.0, "max_price": 0, "name": "贵州茅台", "quantity": 10},
        ]
        frame = pd.DataFrame([
            {"code": "000001", "name": "平安银行", "price": "10", "open": "9.8", "pre_close": "9.5"},
            {"code": "600519", "name": "贵州茅台", "price": "1500", "open": "1480", "pre_close": "1450"},
        ])
        with patch.object(main, "get_all_holdings", return_value=holdings), \
             patch.object(main, "get_prev_closes", return_value={}), \
             patch.object(ts, "get_realtime_quotes", return_value=frame) as quote:
            first = json.loads(asyncio.run(type(self).endpoint()).body)
            second = json.loads(asyncio.run(type(self).endpoint()).body)

        quote.assert_called_once_with(["000001", "600519"])
        self.assertEqual(first, second)
        self.assertEqual(first["stocks"][0]["today_pnl_pct"], 5.26)
        self.assertEqual(first["stocks"][1]["quote_source"], "realtime")


if __name__ == "__main__":
    unittest.main()
