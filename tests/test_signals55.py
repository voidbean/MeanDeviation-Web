"""Synthetic, explicit indicator fixtures exercise shape logic, not API access."""
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core import db
from services import kline55
from services.signals55 import analyze, TZ

NOW = dt.datetime(2026, 9, 16, 16, tzinfo=TZ)


def candle(stamp, close=9.9):
    return {"time": stamp, "open": close-.02, "close": close, "high": close+.05,
            "low": close-.05, "volume_shares": 1000, "ma5": 10.2,
            "ma10": 10.1, "ma20": 10, "ma55": 10, "ma233": None}


def set_price(bar, close, open_=None, low=None, high=None):
    bar.update(close=close, open=close-.02 if open_ is None else open_)
    bar['low'] = min(bar['open'], close)-.05 if low is None else low
    bar['high'] = max(bar['open'], close)+.05 if high is None else high


def snapshot(period, bars):
    return {"code": "600000.SH", "period": period, "adjustment": "qfq",
            "adjustment_anchor": "20260915", "bars": bars, "as_of": bars[-1]['time'] if bars else None}


def daily_fixture():
    dates = pd.bdate_range(end='2026-09-15', periods=100)
    bars = []
    for i, date in enumerate(dates):
        b = candle(date.strftime('%Y%m%d'), 9.9 if i < 85 else 10.4+(i-85)*.01)
        b.update(ma5=9.2+i*.01, ma10=9.1+i*.01, ma20=9+i*.01)
        bars.append(b)
    return snapshot('D', bars)


def hourly_fixture():
    bars = [candle(f'{date:%Y-%m-%d} {hour}') for date in pd.bdate_range(end='2026-09-15', periods=40)
            for hour in ('10:30:00', '11:30:00', '14:00:00', '15:00:00')]
    for i in range(141, len(bars)):
        set_price(bars[i], 10.2)
    set_price(bars[141], 10.15)  # breakout
    set_price(bars[142], 10.30)  # departure on a later candle
    set_price(bars[143], 9.99, open_=10.1, low=9.95)  # first pullback: one close below
    bars[143]['volume_shares'] = 500
    set_price(bars[144], 10.05, open_=10.0, low=9.97)  # reclaim also crosses MA55
    return snapshot('60min', bars)


def run(hourly=None, daily=None):
    return analyze(hourly if hourly is not None else hourly_fixture(), daily if daily is not None else daily_fixture(), NOW)


class DailySignalsTest(unittest.TestCase):
    def test_source_is_structured_not_inferred_from_message(self):
        result = analyze(daily_fixture(), now=NOW)
        self.assertEqual(result['strategy_source'], 'ma55')
        self.assertEqual(result['strategy_label'], '55线规则')
        event = result['events'][0]
        self.assertEqual(event['strategy_source'], 'ma55')
        self.assertEqual(event['rule_version'], result['version'])
        self.assertEqual(event['title'], '【55线】日线 · 新趋势')

    def test_new_trend_only_on_cross(self):
        daily = daily_fixture()
        result = analyze(daily, now=NOW)
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(event['type'], 'NEW_TREND')
        self.assertEqual(event['time'], daily['bars'][85]['time'])
        self.assertEqual(result['state'], 'trend')
        self.assertIsNone(result['signal'])  # old event is not a new latest-bar signal
        daily['bars'] = daily['bars'][:86]
        current = analyze(daily, now=NOW)
        self.assertEqual(current['signal'], 'NEW_TREND')
        self.assertFalse(current['notifications_enabled'])

    def test_touch_alone_and_wrong_slope_are_not_new_trend(self):
        daily = daily_fixture()
        for b in daily['bars']:
            b['ma5'], b['ma10'], b['ma20'] = 10.2, 10.1, 10
        self.assertEqual(analyze(daily, now=NOW)['events'], [])

    def test_insufficient_and_missing_ma(self):
        d = daily_fixture()
        d['bars'] = d['bars'][:20]
        self.assertEqual(analyze(d, now=NOW)['state'], 'insufficient')
        d = daily_fixture()
        d['bars'][-1]['ma10'] = None
        self.assertEqual(analyze(d, now=NOW)['state'], 'insufficient')

    def test_today_and_future_are_ignored(self):
        d = daily_fixture()
        before = analyze(d, now=NOW)
        for date in ('20260916', '20260917'):
            b = copy.deepcopy(d['bars'][-1]); b['time'] = date
            set_price(b, 1)
            d['bars'].append(b)
        self.assertEqual(analyze(d, now=NOW), before)

    def test_invalid_data_fails_closed(self):
        for field, value in [('close', float('nan')), ('ma55', float('inf')), ('low', 100), ('time', 'bad')]:
            d = daily_fixture(); d['bars'][-1][field] = value
            result = analyze(d, now=NOW)
            self.assertEqual(result['state'], 'invalid_data')
            self.assertEqual(result['events'], [])
        d = daily_fixture(); d['bars'].append(d['bars'][-1])
        self.assertEqual(analyze(d, now=NOW)['state'], 'invalid_data')

    def test_stale_is_explicit_but_preserves_historical_events(self):
        d = daily_fixture()
        old = analyze(d, now=NOW+dt.timedelta(days=20))
        self.assertGreater(old['age_calendar_days'], 7)
        self.assertTrue(any('旧历史状态' in w for w in old['warnings']))
        self.assertEqual(old['events'], analyze(d, now=NOW)['events'])


class HourlySignalsTest(unittest.TestCase):
    def test_complete_sequence_and_reclaim_cross_not_reset(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:145]
        result = run(h)
        self.assertEqual(result['signal'], 'PULLBACK_ENTRY')
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(event['strategy_source'], 'ma55')
        self.assertEqual(event['title'], '【55线】60分钟 · 回踩转强')
        self.assertEqual(event['setup_time'], h['bars'][141]['time'])
        self.assertEqual(event['evidence']['touch_time'], h['bars'][143]['time'])
        self.assertEqual(event['evidence']['volume_ratio'], .5)
        self.assertLess(event['evidence']['daily_time'], event['time'][:10].replace('-', ''))
        self.assertGreater(event['levels']['reference_pressure'], event['close'])

    def test_intermediate_states(self):
        for length, state in [(142, 'breakout'), (143, 'departed'), (144, 'pullback'), (145, 'entry')]:
            h = hourly_fixture(); h['bars'] = h['bars'][:length]
            self.assertEqual(run(h)['state'], state)

    def test_one_event_per_setup(self):
        result = run()
        self.assertEqual(len(result['events']), 1)
        self.assertIsNone(result['signal'])
        self.assertEqual(result['state'], 'entry')

    def test_no_breakout_no_departure_no_entry(self):
        h = hourly_fixture()
        for b in h['bars'][:142]:
            set_price(b, 10.15)  # no observed up-cross
        self.assertEqual(run(h)['events'], [])
        h = hourly_fixture(); set_price(h['bars'][142], 10.05)  # not 1% away
        self.assertEqual(run(h)['events'], [])

    def test_touch_without_reversal_not_entry(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:145]
        set_price(h['bars'][144], 10.01, open_=10.02)  # above line but red, no buffer
        result = run(h)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['state'], 'pullback')

    def test_same_candle_touch_and_reversal_allowed_after_departure(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:144]
        set_price(h['bars'][143], 10.35, open_=10.02, low=10.0)
        self.assertEqual(run(h)['signal'], 'PULLBACK_ENTRY')

    def test_missing_zero_and_large_volume_not_contraction(self):
        for v in (None, 0, float('nan'), 1200):
            h = hourly_fixture(); h['bars'][143]['volume_shares'] = v
            self.assertEqual(run(h)['events'], [])
        h = hourly_fixture(); h['bars'][130]['volume_shares'] = None
        self.assertEqual(run(h)['events'], [])

    def test_deep_pierce_and_two_closes_below_invalidate(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:144]; h['bars'][143]['low'] = 9.7
        self.assertEqual(run(h)['state'], 'invalidated')
        h = hourly_fixture(); h['bars'] = h['bars'][:145]
        set_price(h['bars'][144], 9.98)
        self.assertEqual(run(h)['state'], 'invalidated')
        self.assertEqual(run(h)['events'], [])

    def test_touch_expires(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:148]
        for b in h['bars'][143:]:
            set_price(b, 10.01, open_=10.02)
        self.assertEqual(run(h)['state'], 'expired')
        self.assertEqual(run(h)['events'], [])

    def test_whole_setup_expires_without_pullback(self):
        h = hourly_fixture()
        for b in h['bars'][104:]:
            set_price(b, 10.3)
        self.assertEqual(run(h)['state'], 'expired')
        self.assertEqual(run(h)['events'], [])

    def test_old_daily_breakout_not_reused(self):
        d = daily_fixture()
        for b in d['bars'][70:85]:
            set_price(b, 10.4)
        result = run(daily=d)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['state'], 'no_trend')

    def test_upper_missing_indicator_is_unknown_not_negative(self):
        d = daily_fixture()
        for b in d['bars']:
            b['ma10'] = None
        self.assertEqual(run(daily=d)['state'], 'insufficient')

    def test_bad_ma233_not_required_for_entry(self):
        # Missing a long-term reference must not fabricate pressure or block MA55.
        h = hourly_fixture()
        for b in h['bars']:
            b['ma233'] = None
        self.assertEqual(len(run(h)['events']), 1)

    def test_missing_hour_and_extra_opening_record_stop_confirmation(self):
        h = hourly_fixture(); del h['bars'][130]
        self.assertEqual(run(h)['events'], [])
        self.assertEqual(run(h)['state'], 'unverified_schedule')
        h = hourly_fixture(); h['bars'][128]['time'] = h['bars'][128]['time'][:11] + '09:30:00'
        self.assertEqual(run(h)['events'], [])
        self.assertEqual(run(h)['state'], 'unverified_schedule')

    def test_missing_entire_session_detected_against_daily(self):
        h = hourly_fixture(); del h['bars'][128:132]
        self.assertEqual(run(h)['events'], [])
        self.assertEqual(run(h)['state'], 'unverified_schedule')

    def test_no_daily_and_mismatched_anchor(self):
        self.assertEqual(analyze(hourly_fixture(), now=NOW)['state'], 'missing_daily')
        d = daily_fixture(); d['adjustment_anchor'] = '20260914'
        self.assertEqual(run(daily=d)['state'], 'unaligned')

    def test_no_future_daily_leakage(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:145]
        before = run(h)
        event_day = h['bars'][-1]['time'][:10].replace('-', '')
        d = daily_fixture()
        for b in d['bars']:
            if b['time'] >= event_day:
                set_price(b, 1)
                b.update(ma5=1, ma10=2, ma20=3)
        self.assertEqual(run(h, d), before)
        # Removing later daily bars (anchor fixed) also cannot change prior decisions.
        d['bars'] = [b for b in d['bars'] if b['time'] < event_day]
        self.assertEqual(run(h, d), before)

    def test_future_hourly_extension_preserves_events(self):
        full = hourly_fixture()
        prefix = copy.deepcopy(full); prefix['bars'] = prefix['bars'][:145]
        old = run(prefix)['events']
        for b in full['bars'][145:]:
            set_price(b, 1)
        self.assertEqual(run(full)['events'][:len(old)], old)

    def test_daily_trend_failure_invalidates_existing_shape(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:149]
        d = daily_fixture()
        day_before = h['bars'][147]['time'][:10].replace('-', '')
        for b in d['bars']:
            if b['time'] == day_before:
                set_price(b, 9)
        self.assertEqual(run(h, d)['state'], 'invalidated')

    def test_future_high_not_used_as_pressure(self):
        h = hourly_fixture(); h['bars'] = h['bars'][:145]
        event = run(h)['events'][0]
        h2 = hourly_fixture(); h2['bars'][150]['high'] = 1000
        self.assertEqual(run(h2)['events'][0]['levels'], event['levels'])

    def test_15min_not_silently_reusing_hourly_rules(self):
        h = hourly_fixture(); h['period'] = '15min'
        self.assertEqual(run(h)['state'], 'unsupported')


class SignalCacheIntegrationTest(unittest.TestCase):
    def test_read_uses_both_cached_series_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.db')
            db.claim_kline55_refresh('600000.SH', 'D', 0, path)
            db.save_kline55_snapshot('600000.SH', 'D', daily_fixture(), None, path)
            db.claim_kline55_refresh('600000.SH', '60min', 0, path)
            db.save_kline55_snapshot('600000.SH', '60min', hourly_fixture(), None, path)
            with patch.object(kline55, 'safe_query', side_effect=AssertionError('network forbidden')), \
                    patch.object(kline55, 'analyze', side_effect=lambda s, d: analyze(s, d, NOW)):
                result = kline55.get_snapshot('600000', '60min', path)
            self.assertEqual(len(result['analysis']['events']), 1)
            json.dumps(result, allow_nan=False)
            db.save_kline55_snapshot('600000.SH', '60min', None, 'rate_limited', path)
            result = kline55.get_snapshot('600000', '60min', path)
            self.assertTrue(any('刷新失败' in w for w in result['analysis']['warnings']))


if __name__ == '__main__':
    unittest.main()
