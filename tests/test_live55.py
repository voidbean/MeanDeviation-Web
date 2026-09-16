import copy
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core import db
from services import live55 as live
from services.signals55 import TZ, analyze
from test_signals55 import hourly_fixture, daily_fixture
from routes.kline import register
import fetch_history as history

NOW = dt.datetime(2026, 9, 16, 11, 33, tzinfo=TZ)


def event(time='2026-09-16 11:30:00', key='test-shape'):
    return dict(id=key, time=time, close=10.1, title='【55线】60分钟 · 回踩转强', evidence={}, levels={})


def analysis(time='2026-09-16 11:30:00', key='test-shape'):
    return dict(as_of=time, events=[event(time, key)])


class LiveStoreMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.db')
        self.patch = patch.object(db, 'DB_PATH', self.path)
        self.patch.start()
        db.init_db()
        db.live55_subscription('600000.SH', True)
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE ma55_subscriptions SET enabled_at='2026-09-16 09:00:00'")

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def baseline(self):
        self.assertEqual(db.live55_record('600000.SH', analysis('2026-09-16 10:30:00'), NOW), [])


class LiveDatabaseTests(LiveStoreMixin, unittest.TestCase):
    def test_baseline_then_single_push_and_reload(self):
        self.baseline()
        events = db.live55_record('600000.SH', analysis(), NOW)
        self.assertEqual(len(events), 1)
        self.assertIn('【55线】', events[0]['message'])
        self.assertEqual(db.live55_record('600000.SH', analysis(), NOW), [])
        loaded = db.get_recent_watch_events()
        self.assertEqual(loaded[0]['id'], events[0]['id'])
        self.assertEqual(loaded[0]['execution_status'], 'observe')
        self.assertIsNone(loaded[0]['action'])
        # Even if watermark is restored to an older point, persistent key prevents duplicates.
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE ma55_subscriptions SET watermark='2026-09-16 10:30:00'")
        self.assertEqual(db.live55_record('600000.SH', analysis(), NOW), [])

    def test_observation_rejects_execution_feedback(self):
        from core.watch_execution import submit_feedback
        self.baseline()
        item = db.live55_record('600000.SH', analysis(), NOW)[0]
        with self.assertRaisesRegex(ValueError, '不关联交易计划'):
            submit_feedback(self.path, item['id'], {'action':'disable'})

    def test_stale_and_disabled_do_not_emit(self):
        self.baseline()
        self.assertEqual(db.live55_record('600000.SH', analysis(), NOW + dt.timedelta(hours=1)), [])
        db.live55_subscription('600000.SH', False)
        self.assertEqual(db.live55_record('600000.SH', analysis('2026-09-16 14:00:00'), NOW), [])

    def test_reenable_and_redundant_enable(self):
        self.baseline()
        db.live55_subscription('600000.SH', True)
        self.assertIsNotNone(db.live55_subscription('600000.SH')[0]['watermark'])
        db.live55_subscription('600000.SH', False)
        db.live55_subscription('600000.SH', True)
        self.assertIsNone(db.live55_subscription('600000.SH')[0]['watermark'])
        self.assertEqual(db.live55_record('600000.SH', analysis(), NOW), [])

    def test_lease_blocks_second_worker(self):
        self.assertIsNotNone(db.live55_claim(1000))
        self.assertIsNone(db.live55_claim(1001))
        db.live55_release(1010)
        self.assertIsNotNone(db.live55_claim(1011))

    def test_raw_upsert_and_period_isolation(self):
        db.live55_save_bars('600000.SH', 'D', [{'time':'20260915', 'close':10}], 'daily')
        db.live55_save_bars('600000.SH', 'D', [{'time':'20260915', 'close':11}], 'daily')
        self.assertEqual(db.live55_load_bars('600000.SH', 'D'), [{'time':'20260915','close':11}])
        self.assertEqual(db.live55_load_bars('600000.SH', '60min'), [])

    def test_cursor_does_not_skip_backlog(self):
        with sqlite3.connect(self.path) as c:
            for i in range(5):
                c.execute("INSERT INTO watch_events(rule_id,code,event_type,priority,price,message,triggered_at) VALUES(0,'600000.SH','ma55_signal','normal',10,'55','2026-09-16 11:33:00')")
        rows = db.get_recent_watch_events(limit=2, after_id=1)
        self.assertEqual([r['id'] for r in rows], [3,2])

    def test_routes_read_has_no_upstream_calls(self):
        app = FastAPI(); register(app, None)
        with TestClient(app) as client, patch.object(live, 'safe_query', side_effect=AssertionError):
            self.assertEqual(client.get('/api/kline55/monitor?code=600000').json()['enabled'], 1)
            self.assertEqual(client.post('/api/kline55/monitor?code=600000&enabled=false').json()['enabled'], 0)
            self.assertEqual(client.post('/api/kline55/monitor?code=510300&enabled=true').status_code, 400)

    def test_worker_safe_error_no_notification(self):
        with patch.object(live, 'sync_one', side_effect=live.DataError('permission_denied')), patch('services.monitor.publish_events') as publish:
            live.run_once(NOW)
        self.assertIn('权限不足', db.live55_subscription('600000.SH')[0]['status'])
        publish.assert_not_called()


class LiveValidationTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame([dict(ts_code='600000.SH', time='2026-09-16 10:30:00',open=10,high=11,low=9,close=10.5,vol=100,amount=1000)])

    def test_raw_units_and_validation(self):
        frame = self.frame()
        b = live.raw_bars(frame, '600000.SH', '60min', 'rt_min')[0]
        self.assertEqual(b['volume_shares'], 100)
        for invalid in (pd.concat([frame, frame]), frame.assign(high=1), frame.assign(vol=-1),frame.assign(close=float('nan')),frame.assign(ts_code='000001.SZ')):
            with self.assertRaises(live.NotReady):
                live.raw_bars(invalid, '600000.SH', '60min', 'rt_min')

    def test_missing_current_factor_blocks(self):
        frame = pd.DataFrame([dict(ts_code='600000.SH',trade_date='20260915',adj_factor=2)])
        with self.assertRaises(live.NotReady):
            live.factors_map(frame, '600000.SH', ['20260915','20260916'])
        frame.loc[1] = ['600000.SH','20260916',1]
        self.assertEqual(live.factors_map(frame,'600000.SH',['20260916'])['20260916'],1)

    def test_rebase_all_prices_and_ma_not_volume(self):
        bars = [dict(time='20260915',open=10,high=11,low=9,close=10,volume_shares=100)] * 60
        result = live.adjusted('600000.SH','D',bars,{'20260915':1,'20260916':2},'20260916')
        self.assertEqual(result['bars'][-1]['close'],5)
        self.assertEqual(result['bars'][-1]['ma55'],5)
        self.assertEqual(result['bars'][-1]['volume_shares'],100)
        self.assertEqual(bars[-1]['close'],10)

    def test_schedule_rejects_missing_bars_extra_auction_and_calendar(self):
        daily=[{'time':'20260914'},{'time':'20260915'}]
        hours=[{'time':f'2026-09-{day} {hour}'} for day in ('14','15','16') for hour in live.HOURS]
        hours=hours[:10]
        calendar=pd.DataFrame([dict(cal_date=f'202609{d}',is_open=1) for d in ('14','15','16')])
        cutoff=dt.datetime(2026,9,16,11,31)
        live.verify_schedule(daily,hours,calendar,NOW.date(),cutoff)
        for bad in (hours[1:],hours[:4]+hours[5:], [{'time':'2026-09-14 09:30:00'}]+hours):
            with self.assertRaises(live.NotReady):
                live.verify_schedule(daily,bad,calendar,NOW.date(),cutoff)
        with self.assertRaises(live.NotReady):
            live.verify_schedule(daily,hours,calendar.iloc[1:],NOW.date(),cutoff)

    def test_current_closed_only_explicit_mode(self):
        h=hourly_fixture();d=daily_fixture()
        stamp=dt.datetime.strptime(h['bars'][144]['time'],'%Y-%m-%d %H:%M:%S')
        now=stamp.replace(tzinfo=TZ)+dt.timedelta(minutes=3)
        h['bars']=h['bars'][:145]
        self.assertIsNone(analyze(h,d,now=now)['signal'])
        result=analyze(h,d,now=now,closed_before=stamp)
        self.assertEqual(result['signal'],'PULLBACK_ENTRY')
        self.assertIsNone(analyze(h,d,now=now,closed_before=stamp-dt.timedelta(seconds=1))['signal'])


class HistoryAdjustmentTests(unittest.TestCase):
    def test_stock_and_etf_correct_factor_direction_and_atomic_failure(self):
        for code in ('600000','510300'):
            conn=sqlite3.connect(':memory:');history.ensure_tables(conn)
            ts_code=code+'.SH'
            frame=pd.DataFrame([dict(ts_code=ts_code,trade_date=day,open=10,high=11,low=9,close=10,vol=100,amount=100) for day in ('20260915','20260916')])
            factor=pd.DataFrame([dict(ts_code=ts_code,trade_date='20260915',adj_factor=2),dict(ts_code=ts_code,trade_date='20260916',adj_factor=1)])
            class API:
                def daily(self, **kw):
                    assert 'adj' not in kw
                    return frame
                fund_daily=daily
                def adj_factor(self,**kw): return factor
                fund_adj=adj_factor
            self.assertEqual(history.fetch_one(API(),conn,code),2)
            self.assertEqual(conn.execute('SELECT close FROM daily_records ORDER BY date').fetchall(),[(20.,),(10.,)])
            before=conn.execute('SELECT * FROM daily_records').fetchall()
            factor=factor.iloc[1:]
            with self.assertRaises(ValueError): history.fetch_one(API(),conn,code)
            self.assertEqual(before,conn.execute('SELECT * FROM daily_records').fetchall())
            conn.close()

    def test_incomplete_legacy_coverage_does_not_mix(self):
        conn=sqlite3.connect(':memory:');history.ensure_tables(conn)
        history.upsert_daily_record(conn,'2020-01-01','600000','',10,11,9,10)
        class API:
            def daily(self,**kw):return pd.DataFrame([dict(trade_date='20260915')])
        with self.assertRaises(ValueError):history.fetch_one(API(),conn,'600000')
        self.assertEqual(conn.execute('SELECT close FROM daily_records').fetchone()[0],10)
        conn.close()


class LivePipelineTests(LiveStoreMixin, unittest.TestCase):
    # The synthetic provider exercises orchestration; shape logic has separate replay tests.
    def provider(self, rt_count=2, missing_factor=False, missing_hour=False, deny_cumulative=False):
        code='600000.SH'
        def rows(stamps,key):
            return pd.DataFrame([dict(ts_code=code, **{key:s}, open=10,high=11,low=9,close=10,
                                     vol=100,amount=1000) for s in stamps])
        dates=[d.strftime('%Y%m%d') for d in pd.bdate_range(end='2026-09-15',periods=100)]
        daily=rows(dates,'trade_date')
        hours=rows([f'{d:%Y-%m-%d} {h}' for d in pd.bdate_range(end='2026-09-15',periods=20) for h in live.HOURS],'trade_time')
        if missing_hour: hours=hours.drop(5)
        current=rows([f'2026-09-16 {h}' for h in live.HOURS[:rt_count]],'time')
        factors=pd.DataFrame([dict(ts_code=code,trade_date=d,adj_factor=1) for d in dates + ([] if missing_factor else ['20260916'])])
        calendar=pd.DataFrame([dict(cal_date=d.strftime('%Y%m%d'),is_open=int(d.weekday()<5)) for d in pd.date_range(start=dates[0],end='20260916')])
        def query(name,params):
            if deny_cumulative and name=='rt_min_daily': raise live.DataError('permission_denied')
            return {'daily':daily,'stk_mins':hours,'rt_min_daily':current,'rt_min':current,
                    'trade_cal':calendar,'adj_factor':factors}[name].copy()
        return query

    def sub(self):return db.live55_subscription('600000.SH')[0]

    def test_full_pipeline_waits_for_stability_then_baselines_then_publishes(self):
        with self.assertRaises(live.NotReady):live.sync_one(self.sub(),NOW,self.provider())
        with patch('services.monitor.publish_events') as publish:
            result,events=live.sync_one(self.sub(),NOW+dt.timedelta(minutes=2),self.provider())
            self.assertFalse(result['historical_only'])
            self.assertEqual(events,[])
        later=NOW.replace(hour=14,minute=3)
        with self.assertRaises(live.NotReady):live.sync_one(self.sub(),later,self.provider(rt_count=3))
        fake=analysis('2026-09-16 14:00:00','shape-2');fake.update(status='evaluated',label='回踩转强')
        with patch.object(live,'analyze',return_value=fake),patch('services.monitor.publish_events') as publish:
            _,events=live.sync_one(self.sub(),later+dt.timedelta(minutes=2),self.provider(rt_count=3))
            self.assertEqual(len(events),1)
            publish.assert_called_once_with(events)
            _,events=live.sync_one(self.sub(),later+dt.timedelta(minutes=3),self.provider(rt_count=3))
            self.assertEqual(events,[])

    def test_no_factor_or_gap_never_calls_signal_engine(self):
        for args in ({'missing_factor':True},{'missing_hour':True}):
            with self.assertRaises(live.NotReady):live.sync_one(self.sub(),NOW,self.provider(**args))
            with patch.object(live,'analyze') as engine, self.assertRaises(live.NotReady):
                live.sync_one(self.sub(),NOW+dt.timedelta(minutes=2),self.provider(**args))
            engine.assert_not_called()
            self.assertEqual(db.get_recent_watch_events(),[])
            # Force another complete history attempt for the next scenario.
            db.live55_update('600000.SH','test',{})

    def test_permission_fallback_keeps_official_source(self):
        with self.assertRaises(live.NotReady):
            live.sync_one(self.sub(),NOW,self.provider(deny_cumulative=True))
        bars=db.live55_load_bars('600000.SH','60min')
        self.assertEqual(bars[-1]['source'],'rt_min')
