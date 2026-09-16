import datetime as dt
import sqlite3
import unittest
from services.market_context import build_market_context, market_warning_text


class MarketContextTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.executescript('CREATE TABLE intraday_snapshots(code,date,time,price,high,amount); CREATE TABLE daily_records(code,date,close,amount);')
        self.now = dt.datetime(2026, 9, 15, 10, 30)
        self.c.execute("INSERT INTO daily_records VALUES('000001.SH','2026-09-14',100,1000)")
        self.c.execute("INSERT INTO intraday_snapshots VALUES('000001.SH','2026-09-15','10:30',101,102,200)")

    def get(self):
        return build_market_context(self.c, now=self.now)['indexes']['000001.SH']

    def test_linear_and_warning(self):
        i = self.get()
        self.assertEqual(i['estimate_method'], 'linear_time')
        self.assertAlmostEqual(i['estimated_full_day_amount_yi'], .008)
        self.assertIsNone(i['same_time_amount_ratio'])
        self.assertIn('非诱多定论', i['warning'])
        self.assertIn('勿据此追涨', market_warning_text(build_market_context(self.c, now=self.now)))

    def test_historical(self):
        for day in ['2026-09-09', '2026-09-10', '2026-09-11']:
            self.c.execute('INSERT INTO daily_records VALUES(?,?,?,?)', ('000001.SH', day, 100, 1000))
            self.c.execute('INSERT INTO intraday_snapshots VALUES(?,?,?,?,?,?)', ('000001.SH', day, '10:30', 100, 100, 300))
        i = self.get()
        self.assertEqual(i['estimate_method'], 'historical_same_time')
        self.assertEqual(i['regime'], '缩量上涨')
        self.assertAlmostEqual(i['estimated_full_day_amount_yi'], 200/.3/100000)

    def test_stale(self):
        self.now += dt.timedelta(minutes=4)
        i = self.get()
        self.assertEqual(i['status'], 'stale')
        self.assertIsNone(i['warning'])
        self.assertIsNone(i['estimated_full_day_amount_yi'])

    def test_missing_and_no_total(self):
        context = build_market_context(self.c, now=self.now)
        self.assertEqual(context['indexes']['399001.SZ']['status'], 'missing')
        self.assertIsNone(context['total_market_amount_yi'])

    def test_early_and_invalid(self):
        self.c.execute("UPDATE intraday_snapshots SET time='09:40'")
        self.now = self.now.replace(hour=9, minute=40)
        self.assertIsNone(self.get()['estimated_full_day_amount_yi'])
        self.c.execute('UPDATE intraday_snapshots SET amount=NULL')
        self.assertEqual(self.get()['status'], 'missing')

    def test_lunch_excluded(self):
        self.c.execute("UPDATE intraday_snapshots SET time='11:30'")
        self.now = self.now.replace(hour=12)
        self.assertEqual(self.get()['status'], 'ok')
        self.assertAlmostEqual(self.get()['estimated_full_day_amount_yi'], .004)

    def test_database_error_degrades_without_blocking(self):
        self.c.execute('DROP TABLE intraday_snapshots')
        result = build_market_context(self.c, now=self.now)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(market_warning_text(result), '')
