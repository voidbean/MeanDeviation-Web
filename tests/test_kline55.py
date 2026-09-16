import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from core import db
from services import kline55 as k
from routes.kline import register

NOW = dt.datetime(2026, 9, 16, 16, tzinfo=k.TZ)


def fixture(period='D', count=280):
    if period == 'D':
        times = [x.strftime('%Y%m%d') for x in pd.bdate_range(end='2026-09-15', periods=count)]
        dates = times
    else:
        times = [f'{day:%Y-%m-%d} {hour}' for day in pd.bdate_range(end='2026-09-15', periods=80)
                 for hour in ('10:30:00', '11:30:00', '14:00:00', '15:00:00')][-count:]
        dates = [x[:10].replace('-', '') for x in times]
    rows = []
    for i, stamp in enumerate(times):
        close = 10 + i / 100
        rows.append({'ts_code': '600000.SH', 'trade_date' if period == 'D' else 'trade_time': stamp,
                     'open': close - .01, 'close': close, 'high': close + .1, 'low': close - .1,
                     'vol': 100, 'amount': 1000, 'pre_close': close - .02})
    bars = pd.DataFrame(rows[::-1])
    factors = pd.DataFrame([{'ts_code': '600000.SH', 'trade_date': day,
                             'adj_factor': 1 if day < dates[-100] else 2} for day in sorted(set(dates), reverse=True)])
    return bars, factors


class KlineDataTest(unittest.TestCase):
    def load(self, period='D', count=280, mutate=None):
        bars, factors = fixture(period, count)
        if mutate:
            bars, factors = mutate(bars, factors)
        calls = []
        def query(name, params):
            calls.append((name, params))
            return (factors if name == 'adj_factor' else bars).copy()
        with contextlib.redirect_stdout(io.StringIO()):
            result = k.fetch_snapshot('600000', period, NOW, query)
        return result, calls

    def test_daily_sdk_ma_matches_reference(self):
        result, calls = self.load()
        self.assertEqual(result['samples'], 280)
        self.assertEqual([x[0] for x in calls], ['daily', 'adj_factor'])
        self.assertEqual(result['adjustment_anchor'], '20260915')
        bars = result['bars']
        self.assertIsNone(bars[53]['ma55'])
        self.assertIsNotNone(bars[54]['ma55'])
        self.assertIsNone(bars[231]['ma233'])
        for n in (5, 10, 20, 55, 233):
            self.assertAlmostEqual(bars[-1][f'ma{n}'], sum(b['close'] for b in bars[-n:]) / n, delta=.005001)
        self.assertEqual(bars[-1]['volume_shares'], 10000)
        self.assertEqual(bars[-1]['amount_yuan'], 1000000)
        self.assertFalse(result['signals_enabled'])
        self.assertTrue(result['history_only'])

    def test_minute_uses_official_period_and_daily_factor_dates(self):
        for period in ('15min', '60min'):
            result, calls = self.load(period)
            self.assertEqual(calls[0][0], 'stk_mins')
            self.assertEqual(calls[0][1]['freq'], period)
            self.assertEqual(len(calls[1][1]['start_date']), 8)
            self.assertEqual(calls[1][1]['end_date'], '20260915')
            self.assertEqual(result['bars'][-1]['volume_shares'], 100)
            self.assertEqual(result['bars'][-1]['amount_yuan'], 1000)
            self.assertIsNotNone(result['bars'][-1]['ma233'])
            self.assertEqual(result['bars'][0]['close'], 5.0)

    def test_short_history_does_not_invent_ma233(self):
        result, _ = self.load(count=100)
        self.assertIsNone(result['bars'][-1]['ma233'])
        json.dumps(result, allow_nan=False)

    def test_invalid_prices_duplicates_and_factors_rejected(self):
        changes = [
            lambda b, f: (pd.concat([b, b.iloc[:1]]), f),
            lambda b, f: (b.assign(close=float('nan')), f),
            lambda b, f: (b.assign(high=1), f),
            lambda b, f: (b, f.iloc[1:]),
            lambda b, f: (b, f.assign(adj_factor=0)),
            lambda b, f: (b.assign(ts_code='000001.SZ'), f),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(k.DataError):
                self.load(mutate=change)

    def test_today_and_future_bars_are_not_confirmed(self):
        def change(b, f):
            b.loc[0, 'trade_date'] = '20260916'
            return b, f
        with self.assertRaises(k.DataError):
            self.load(mutate=change)

    def test_normalize(self):
        self.assertEqual(k.normalize('sh600000', 'D'), ('600000.SH', 'D'))
        self.assertEqual(k.normalize('920001', 'D'), ('920001.BJ', 'D'))
        for code, period in [('600000', '55min'), ('<script>', 'D'), ('510300.SH', 'D'), ('000001.SH', 'D')]:
            with self.assertRaises(ValueError):
                k.normalize(code, period)

    def test_provider_error_is_redacted(self):
        class Response:
            returncode = 0
            stdout = json.dumps({'code': -1, 'msg': 'secret-value 每分钟最多访问1次'})
        with patch.object(k.config, 'TS_TOKEN', 'secret-value'), patch.object(k.shutil, 'which', return_value='/usr/bin/curl'), patch.object(k.subprocess, 'run', return_value=Response()) as run:
            with self.assertRaisesRegex(k.DataError, '^rate_limited$'):
                k.safe_query('daily', {})
            self.assertNotIn('secret-value', ' '.join(run.call_args.args[0]))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'cache.db')
        self.snapshot = {'bars': [{'close': 10}], 'adjustment_anchor': '20260915'}

    def test_atomic_replace_throttle_and_cache_read(self):
        def fetch(*args):
            return self.snapshot
        first = k.refresh_snapshot('600000', 'D', self.path, fetch)
        self.assertEqual(first['status'], 'updated')
        with patch.object(k.time, 'time', return_value=0):
            read = k.get_snapshot('600000', 'D', self.path)
        self.assertEqual(read['snapshot'], self.snapshot)
        self.assertEqual(k.refresh_snapshot('600000', '60min', self.path, fetch)['status'], 'cooldown')

    def test_error_preserves_snapshot_and_error_clears_on_success(self):
        k.refresh_snapshot('600000', 'D', self.path, lambda *a: self.snapshot)
        def fail(*a):
            raise k.DataError('rate_limited')
        with patch.object(db.time, 'time', return_value=k.time.time() + 100):
            result = k.refresh_snapshot('600000', 'D', self.path, fail)
        self.assertEqual(result['snapshot'], self.snapshot)
        self.assertEqual(result['last_error'], 'rate_limited')
        with patch.object(db.time, 'time', return_value=k.time.time() + 300):
            result = k.refresh_snapshot('600000', 'D', self.path, lambda *a: {'new': True})
        self.assertEqual(result['snapshot'], {'new': True})
        self.assertIsNone(result['last_error'])

    def test_arbitrary_exception_does_not_leak(self):
        def fail(*a):
            raise RuntimeError('secret-value')
        result = k.refresh_snapshot('600000', 'D', self.path, fail)
        self.assertNotIn('secret-value', json.dumps(result))

    def test_nan_is_not_persisted(self):
        result = k.refresh_snapshot('600000', 'D', self.path, lambda *a: {'ma': float('nan')})
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['snapshot'])


class RouteTemplateTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        register(app, None)
        self.client = TestClient(app)

    def test_read_is_cache_only_and_refresh_is_explicit(self):
        with patch('routes.kline.get_snapshot', return_value={'status': 'missing'}) as get, patch('routes.kline.refresh_snapshot', return_value={'status': 'updated'}) as refresh:
            self.assertEqual(self.client.get('/api/kline55?code=600000').json()['status'], 'missing')
            refresh.assert_not_called()
            self.assertEqual(self.client.post('/api/kline55/refresh?code=600000&period=60min').json()['status'], 'updated')
            refresh.assert_called_once_with('600000', '60min')

    def test_invalid_parameters(self):
        self.assertEqual(self.client.get('/api/kline55?code=bad').status_code, 400)

    def test_template(self):
        env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
        template = env.get_template('_kline55.html')
        self.assertNotIn('kline55-panel', template.render(result=None))
        html = template.render(result={'code': '600000', 'status': 'success'})
        self.assertIn('kline55-panel', html)
        self.assertIn('暂不用于实时买卖通知', html)
        env.get_template('index.html')  # syntax check the integrated page too


if __name__ == '__main__':
    unittest.main()
