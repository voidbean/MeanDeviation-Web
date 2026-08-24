import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import core.db as db
import services.indicators as indicators


class IntradayBatchFetchTest(unittest.TestCase):
    def test_common_stocks_and_indexes_use_two_batch_requests(self):
        stocks = pd.DataFrame([
            {"code": "000001", "price": "10", "open": "9.8", "high": "10.1", "low": "9.7",
             "volume": "1000", "amount": "10000"},
            {"code": "600519", "price": "1500", "open": "1490", "high": "1510", "low": "1480",
             "volume": "2000", "amount": "3000000"},
        ])
        indexes = pd.DataFrame([
            {"code": "000001", "price": "3900", "open": "3890", "high": "3910", "low": "3880",
             "volume": "3000", "amount": "4000000"},
            {"code": "399001", "price": "14000", "open": "13900", "high": "14100", "low": "13800",
             "volume": "4000", "amount": "5000000"},
            {"code": "399006", "price": "3400", "open": "3390", "high": "3410", "low": "3380",
             "volume": "5000", "amount": "6000000"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "intraday.db")
            with patch.object(db, "DB_PATH", path), patch.object(indicators, "DB_PATH", path):
                db.init_db()
                with patch.object(indicators._cfg, "COMMON_STOCKS", [{"code": "000001"}, {"code": "600519"}]), \
                     patch.object(indicators.ts, "get_realtime_quotes", side_effect=[stocks, indexes]) as quotes, \
                     patch.object(indicators, "_save_intraday_snapshot") as save:
                    indicators._fetch_and_save_intraday_snapshots()

        self.assertEqual(quotes.call_count, 2)
        self.assertEqual(quotes.call_args_list[0].args[0], ["000001", "600519"])
        self.assertEqual(save.call_count, 5)


if __name__ == "__main__":
    unittest.main()
