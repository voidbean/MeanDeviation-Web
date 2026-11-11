"""Synthetic market-data contract tests: never require a credential or network."""
import copy
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

import pandas as pd

from core import db
from services import etf_kline as ek
from services.etf_kline_feed import collect_etf, OfficialQuery
from services.kline55 import DataError
from services.mcp_data import SnapshotReader

CODE = '588170.SH'
NOW = dt.datetime(2026, 11, 11, 15, 5, tzinfo=ek.TZ)


def calendar(start, end):
    return {(start + dt.timedelta(days=i)).isoformat(): int((start + dt.timedelta(days=i)).weekday() < 5)
            for i in range((end-start).days + 1)}


def document(n=300, now=NOW):
    cal = calendar(now.date() - dt.timedelta(days=600), now.date() + dt.timedelta(days=45))
    factors = {day: 1.001 + i * .00017 for i, (day, opened) in enumerate(cal.items()) if opened and day <= now.date().isoformat()}
    doc = dict(schema_version=1, code=CODE, asset_type='ETF', collected_at=ek.iso(now),
               calendar=dict(values=cal, fetched_at=ek.iso(now), error=None),
               factors=dict(values=factors, fetched_at=ek.iso(now), error=None),
               adjustment_anchor=max(factors), periods={})
    for period in ek.PERIODS:
        ends = [at for day, opened in cal.items() if opened for at in ek.grid(dt.date.fromisoformat(day), period)
                if at <= now - dt.timedelta(seconds=90)][-n:]
        frame = pd.DataFrame([dict(ts_code=CODE, trade_date=at.strftime('%Y%m%d'),
                    trade_time=at.strftime('%Y-%m-%d %H:%M:%S'), open=.701+i*.00007,
                    close=.702+i*.00007, high=.709+i*.00007, low=.699+i*.00007, vol=123.45, amount=234.567)
                    for i, at in enumerate(ends)])
        source = 'fund_daily' if period == 'D' else 'etf_mins'
        bars = ek.parse_bars(frame, CODE, period, source, now)
        doc['periods'][period] = dict(bars=bars, errors=[], fetched_at=ek.iso(now))
    return doc


class ETFPureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = document()

    def test_etf_normalization_does_not_reuse_equity_rejection(self):
        for code in ('588170', 'sh588170', CODE):
            self.assertEqual(ek.normalize(code), CODE)
        self.assertEqual(ek.normalize('sz159915'), '159915.SZ')
        for code, period in [('600519', 'D'), ('588170.SZ', 'D'), ('159915.SH', 'D'), ('160222', 'D'), ('501001', 'D'), ('../.env', 'D'), (CODE, '5min')]:
            with self.assertRaises(ValueError): ek.normalize(code, period)

    def test_exchange_end_grid_no_lunch_or_auction(self):
        day = NOW.date()
        self.assertEqual([x.strftime('%H:%M') for x in ek.grid(day, '60min')], ['10:30','11:30','14:00','15:00'])
        small = ek.grid(day, '15min')
        self.assertEqual(len(small), 16)
        self.assertEqual(small[8].strftime('%H:%M'), '13:15')
        self.assertEqual(ek.interval_start(small[8], '15min').strftime('%H:%M'), '13:00')

    def test_daily_and_minute_units_are_not_interchangeable(self):
        daily = self.doc['periods']['D']['bars'][0]
        minute = self.doc['periods']['15min']['bars'][0]
        self.assertEqual(daily['volume_shares'], 12345)
        self.assertEqual(daily['amount_yuan'], 234567)
        self.assertEqual(minute['volume_shares'], 123.45)
        self.assertEqual(minute['amount_yuan'], 234.567)

    def test_official_realtime_documented_aliases_and_exchange_calendar(self):
        frame=pd.DataFrame([dict(code=CODE,freq='15MIN',time='2026-11-11 09:45:00',
            open=.701,close=.702,high=.709,low=.699,vol=729200.,amount=746701.)])
        bar=ek.parse_bars(frame,CODE,'15min','rt_etf_min_daily',NOW,current=True)[0]
        self.assertEqual(bar['volume_shares'],729200.)
        self.assertTrue(bar['valid_schedule'])
        sz=pd.DataFrame([dict(cal_date='20261111',is_open=1,exchange='SZSE')])
        self.assertEqual(ek.parse_calendar(sz,NOW.date(),NOW.date(),'SZSE'),{'2026-11-11':1})
        with self.assertRaises(ValueError):ek.parse_calendar(sz,NOW.date(),NOW.date(),'SSE')

    def test_ma55_and_233_boundaries_and_no_price_prerounding(self):
        for period in ek.PERIODS:
            result = ek.prepare_series(self.doc, period, NOW)
            bars = result['bars']
            self.assertTrue(result['analysis_ready'], result['missing_reasons'])
            self.assertIsNone(bars[53]['ma55'])
            self.assertIsNotNone(bars[54]['ma55'])
            self.assertIsNone(bars[231]['ma233'])
            self.assertIsNotNone(bars[232]['ma233'])
            self.assertEqual(bars[-1]['ma233'], math.fsum(b['close'] for b in bars[-233:])/233)
            self.assertNotEqual(bars[-1]['ma55'], round(bars[-1]['ma55'], 2))
            last = bars[-1]
            self.assertEqual(last['close'], last['raw_ohlc']['close'] * last['adjustment_factor']/last['anchor_factor'])

    def test_insufficient_history_is_explicit_not_zero(self):
        doc = copy.deepcopy(self.doc)
        doc['periods']['D']['bars'] = doc['periods']['D']['bars'][-54:]
        result = ek.prepare_series(doc, 'D', NOW)
        self.assertFalse(result['analysis_ready'])
        self.assertIn('insufficient_ma55_history', result['missing_reasons'])
        self.assertIsNone(result['bars'][-1]['ma55'])
        doc['periods']['D']['bars'] = self.doc['periods']['D']['bars'][-232:]
        result = ek.prepare_series(doc, 'D', NOW)
        self.assertTrue(result['analysis_ready'])
        self.assertIsNone(result['bars'][-1]['ma233'])

    def test_missing_expected_bar_resets_ma_and_fails_closed(self):
        doc = copy.deepcopy(self.doc)
        del doc['periods']['15min']['bars'][-20]
        result = ek.prepare_series(doc, '15min', NOW)
        self.assertEqual(result['gap_count'], 1)
        self.assertFalse(result['analysis_ready'])
        self.assertIsNone(result['bars'][-1]['ma55'])

    def test_calendar_weekend_holiday_is_not_a_market_gap(self):
        doc = copy.deepcopy(self.doc)
        # Reclassify a full session as a known holiday, excluding it from input too.
        holiday = doc['periods']['60min']['bars'][-9]['trade_date']
        doc['calendar']['values'][holiday] = 0
        doc['periods']['60min']['bars'] = [b for b in doc['periods']['60min']['bars'] if b['trade_date'] != holiday]
        result = ek.prepare_series(doc, '60min', NOW)
        self.assertEqual(result['gap_count'], 0)
        self.assertTrue(result['analysis_ready'])

    def test_unverified_auction_record_not_silently_dropped_into_ma(self):
        doc = copy.deepcopy(self.doc)
        extra = copy.deepcopy(doc['periods']['60min']['bars'][-4])
        extra.update(end_at=extra['end_at'].replace('10:30:', '09:30:'), valid_schedule=False)
        doc['periods']['60min']['bars'].append(extra)
        doc['periods']['60min']['bars'].sort(key=lambda b:b['end_at'])
        result = ek.prepare_series(doc, '60min', NOW)
        self.assertFalse(result['analysis_ready'])
        self.assertIn('unverified_schedule', result['missing_reasons'])
        self.assertTrue(all(b['ma55'] is None for b in result['bars']))
        self.assertEqual(result['unconfirmed_bars'][0]['closure_state'], 'unverified_schedule')

    def test_factor_missing_never_fills_from_yesterday(self):
        doc = copy.deepcopy(self.doc)
        del doc['factors']['values'][NOW.date().isoformat()]
        doc['adjustment_anchor'] = max(doc['factors']['values'])
        for period in ek.PERIODS:
            result = ek.prepare_series(doc, period, NOW)
            self.assertFalse(result['analysis_ready'])
            self.assertTrue(result['stale'])
            missing = result['unconfirmed_bars'][-1]
            self.assertIsNone(missing['close'])
            self.assertEqual(missing['closure_state'], 'factor_missing')

    def test_shared_anchor_rebuilds_all_periods(self):
        doc = copy.deepcopy(self.doc)
        for period in ek.PERIODS:
            old = ek.prepare_series(doc, period, NOW)['bars'][-1]
            doc2 = copy.deepcopy(doc)
            doc2['factors']['values'][doc2['adjustment_anchor']] *= 2
            new = ek.prepare_series(doc2, period, NOW)['bars'][-2]
            original = ek.prepare_series(doc, period, NOW)['bars'][-2]
            # Penultimate day may share anchor date for minutes; compare using raw factor formula instead.
            self.assertAlmostEqual(new['close'], new['raw_ohlc']['close'] * new['adjustment_factor']/new['anchor_factor'], places=15)
            self.assertEqual(old['close'], old['raw_ohlc']['close'])
            if period == 'D': self.assertEqual(new['close'], original['close']/2)

    def test_dependency_failure_marks_retained_values_stale(self):
        for key in ('calendar', 'factors'):
            doc = copy.deepcopy(self.doc)
            doc[key]['error'] = key + ':network_error'
            result = ek.prepare_series(doc, 'D', NOW)
            self.assertTrue(result['stale'])
            self.assertFalse(result['analysis_ready'])

    def test_realtime_needs_post_end_capture_and_stability_not_read_time(self):
        doc = copy.deepcopy(self.doc)
        bar = doc['periods']['60min']['bars'][-1]
        bar.update(historical=False, observations=1, stable_since=ek.iso(NOW), fetched_at=ek.iso(NOW))
        result = ek.prepare_series(doc, '60min', NOW + dt.timedelta(hours=5))
        self.assertEqual(result['unconfirmed_bars'][-1]['closure_state'], 'awaiting_stability')
        bar.update(observations=2, stable_since=ek.iso(NOW-dt.timedelta(seconds=31)))
        result = ek.prepare_series(doc, '60min', NOW)
        self.assertEqual(result['bars'][-1]['closure_state'], 'confirmed')
        # Capture before end cannot be promoted by merely reading after close.
        bar['fetched_at'] = ek.iso(NOW.replace(hour=14, minute=59))
        result = ek.prepare_series(doc, '60min', NOW+dt.timedelta(days=1))
        self.assertEqual(result['unconfirmed_bars'][-1]['closure_state'], 'awaiting_close_verification')

    def test_close_grace_90_seconds_and_forming_classification(self):
        doc = copy.deepcopy(self.doc)
        bar = doc['periods']['60min']['bars'][-1]
        for second, closed in [(89, False),(90, True)]:
            now = NOW.replace(hour=15,minute=0,second=0)+dt.timedelta(seconds=second)
            bar['fetched_at'] = ek.iso(now)
            result = ek.prepare_series(doc, '60min', now)
            self.assertEqual(bool(result['bars'] and result['bars'][-1]['end_at']==bar['end_at']),closed)
        now = NOW.replace(hour=14,minute=50)
        bar['fetched_at'] = ek.iso(now)
        self.assertEqual(ek.prepare_series(doc, '60min', now)['unconfirmed_bars'][-1]['closure_state'], 'forming')

    def test_revisions_reset_observation_and_stability(self):
        frame = pd.DataFrame([dict(ts_code=CODE,trade_time='2026-11-11 15:00:00',open=.7,close=.701,high=.71,low=.69,vol=10,amount=7)])
        first = ek.parse_bars(frame,CODE,'15min','rt_etf_min_daily',NOW,current=True)
        second = ek.parse_bars(frame,CODE,'15min','rt_etf_min_daily',NOW+dt.timedelta(seconds=35),first,current=True)
        self.assertEqual(second[0]['observations'],2)
        self.assertEqual(second[0]['stable_since'],first[0]['stable_since'])
        frame.loc[0,'close']=.702
        revised = ek.parse_bars(frame,CODE,'15min','rt_etf_min_daily',NOW+dt.timedelta(seconds=70),second,current=True)[0]
        self.assertEqual(revised['observations'],1)
        self.assertTrue(revised['revised'])
        self.assertEqual(revised['revision_count'],1)
        self.assertEqual(revised['stable_since'],ek.iso(NOW+dt.timedelta(seconds=70)))

    def test_malformed_payloads_rejected_not_silently_sanitized(self):
        valid = dict(ts_code=CODE,trade_time='2026-11-11 15:00:00',open=.7,close=.701,high=.71,low=.69,vol=10,amount=7)
        for change in ({'ts_code':'600519.SH'}, {'freq':'60min'}, {'high':.68}, {'close':float('nan')}, {'vol':-1}, {'amount':float('inf')}):
            with self.assertRaises(ValueError): ek.parse_bars(pd.DataFrame([{**valid,**change}]),CODE,'15min','test',NOW)
        with self.assertRaises(ValueError): ek.parse_bars(pd.DataFrame([valid,valid]),CODE,'15min','test',NOW)
        with self.assertRaisesRegex(ValueError,'possibly_truncated'):
            ek.parse_bars(pd.DataFrame([valid]*1000),CODE,'15min','test',NOW,current=True)
        with self.assertRaises(ValueError): ek.parse_factors(pd.DataFrame([{'ts_code':CODE,'trade_date':'20261111','adj_factor':0}]),CODE)
        with self.assertRaises(ValueError): ek.parse_calendar(pd.DataFrame([{'cal_date':'20261111','is_open':.5}]),NOW.date(),NOW.date())
        with self.assertRaisesRegex(ValueError,'calendar_incomplete'):
            ek.parse_calendar(pd.DataFrame([{'cal_date':'20261111','is_open':1}]),NOW.date()-dt.timedelta(days=1),NOW.date())

    def test_extreme_factor_does_not_leak_infinity_or_zero_price(self):
        doc=copy.deepcopy(self.doc)
        doc['factors']['values'][doc['periods']['D']['bars'][0]['trade_date']]=1e308
        doc['factors']['values'][doc['adjustment_anchor']]=1e-308
        result=ek.prepare_series(doc,'D',NOW)
        self.assertFalse(result['analysis_ready'])
        self.assertIn('factor_invalid',result['missing_reasons'])
        json.dumps(result,allow_nan=False)


class ETFCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.doc = document()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'test.db'
        self.save(copy.deepcopy(self.doc))

    def save(self, doc, now=NOW):
        state, lease, _ = db.claim_etf_kline_refresh(CODE,now.timestamp(),str(self.path),cooldown=0)
        self.assertEqual(state,'claimed')
        self.assertTrue(db.publish_etf_kline(CODE,lease,doc,db_path=str(self.path)))

    def read(self,period='D',**kwargs):
        reader = SnapshotReader(self.path)
        with reader.connect() as conn:
            return ek.read_kline_data(conn,CODE,period,now=NOW,**kwargs)

    def test_shared_epoch_across_periods_and_snapshot_changed(self):
        initial = self.read()
        for p in ek.PERIODS:
            result = self.read(p,snapshot_id=initial['snapshot_id'])
            self.assertEqual(result['snapshot_id'],initial['snapshot_id'])
            self.assertEqual(result['adjustment_anchor'],initial['adjustment_anchor'])
        changed = copy.deepcopy(self.doc)
        changed['collected_at'] = ek.iso(NOW+dt.timedelta(minutes=3))
        self.save(changed)
        result = self.read('15min',snapshot_id=initial['snapshot_id'])
        self.assertEqual(result['status'],'snapshot_changed')
        self.assertFalse(result['analysis_ready'])
        self.assertEqual(result['bars'],[])

    def test_pagination_ascending_within_page_backwards_without_gaps(self):
        result = self.read(limit=70)
        ids = []
        while True:
            batch=[b['end_at'] for b in result['bars']]
            self.assertEqual(batch,sorted(batch))
            ids=batch+ids
            if not result['next_cursor']: break
            result=self.read(limit=70,cursor=result['next_cursor'])
        self.assertEqual(len(ids),300)
        self.assertEqual(len(set(ids)),300)
        self.assertEqual(ids,sorted(ids))
        self.assertFalse(result['analysis_ready'])
        self.assertTrue(result['source_analysis_ready'])

    def test_range_warmup_can_reproduce_first_returned_ma233(self):
        result = self.read(limit=5,start_at='2026-11-01',end_at='2026-11-11')
        self.assertEqual(result['returned_samples'],5)
        self.assertEqual(len(result['warmup_bars']),232)
        seq=result['warmup_bars']+[result['bars'][0]]
        self.assertEqual(result['bars'][0]['ma233'],math.fsum(b['close'] for b in seq)/233)
        self.assertTrue(all(b['ma233'] is None for b in result['warmup_bars']))
        self.assertTrue(result['ma233_ready'])
        self.assertEqual(result['actual_range']['end_at'],result['as_of'])

    def test_empty_and_historical_range_not_claimed_current_ready(self):
        result = self.read(start_at='2030-01-01')
        self.assertEqual(result['status'],'empty_range')
        self.assertTrue(result['source_analysis_ready'])
        self.assertFalse(result['analysis_ready'])
        self.assertIsNone(result['as_of'])
        self.assertFalse(result['ma233_ready'])
        result=self.read(end_at='2026-10-30')
        self.assertFalse(result['analysis_ready'])
        self.assertTrue(result['stale'])
        self.assertTrue(result['source_analysis_ready'])
        self.assertNotEqual(result['as_of'],result['source_as_of'])

    def test_invalid_request_and_cursor_bindings(self):
        result=self.read(limit=50)
        for args in ({'limit':501},{'start_at':'2026-11-11T10:00:00'}, {'start_at':'2026-11-12','end_at':'2026-11-11'},
                     {'cursor':'bad'}, {'cursor':result['next_cursor'],'limit':49}):
            with self.assertRaises(ValueError): self.read(**args)

    def test_no_network_no_table_creation_no_business_write(self):
        before=self.path.read_bytes()
        with patch('services.etf_kline_feed.OfficialQuery',side_effect=AssertionError('network forbidden')):
            self.read()
            SnapshotReader(self.path).kline(CODE,'15min')
        self.assertEqual(before,self.path.read_bytes())
        with sqlite3.connect(self.path) as conn:
            names={r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
        self.assertEqual(names,{'etf_kline_cache'})
        missing=Path(self.tmp.name)/'absent.db'
        result=SnapshotReader(missing).kline('588170')
        self.assertEqual(result['missing_reasons'],['database_missing'])
        self.assertFalse(missing.exists())
        empty=Path(self.tmp.name)/'empty.db'
        with sqlite3.connect(empty):pass
        original=empty.read_bytes()
        self.assertEqual(SnapshotReader(empty).kline(CODE)['missing_reasons'],['cache_uninitialized'])
        self.assertEqual(original,empty.read_bytes())

    def test_closed_only_never_mixes_forming_into_main_bars(self):
        doc=copy.deepcopy(self.doc)
        doc['periods']['60min']['bars'][-1].update(historical=False,observations=1)
        self.save(doc)
        result=self.read('60min',closed_only=False)
        self.assertEqual(result['unconfirmed_count'],1)
        self.assertEqual(result['unconfirmed_bars'][0]['closure_state'],'awaiting_stability')
        self.assertIsNone(result['forming_bar'])
        self.assertTrue(all(b['is_closed'] for b in result['bars']))
        self.assertEqual(self.read('60min')['unconfirmed_bars'],[])

    def test_lease_global_cooldown_and_superseded_publication(self):
        now=NOW.timestamp()+100
        state, lease, _=db.claim_etf_kline_refresh(CODE,now,str(self.path))
        self.assertEqual(state,'claimed')
        self.assertEqual(db.claim_etf_kline_refresh('159915.SZ',now+1,str(self.path))[0],'busy')
        self.assertFalse(db.publish_etf_kline(CODE,'wrong',{},db_path=str(self.path)))
        self.assertTrue(db.publish_etf_kline(CODE,lease,None,'collector_failed',str(self.path)))
        self.assertEqual(db.claim_etf_kline_refresh('159915.SZ',now+2,str(self.path))[0],'cooldown')
        result=self.read()
        self.assertIn('collector_failed',result['missing_reasons'])
        self.assertTrue(result['stale'])
        self.assertEqual(len(result['bars']),300)

    def test_expired_lease_cannot_overwrite_newer_same_symbol(self):
        now=NOW.timestamp()+100
        _,old,_=db.claim_etf_kline_refresh(CODE,now,str(self.path))
        _,new,_=db.claim_etf_kline_refresh(CODE,now+601,str(self.path))
        self.assertNotEqual(old,new)
        self.assertFalse(db.publish_etf_kline(CODE,old,{},db_path=str(self.path)))
        self.assertTrue(db.publish_etf_kline(CODE,new,self.doc,db_path=str(self.path)))

    def test_global_cooldown_counts_from_completed_collection(self):
        now=NOW.timestamp()+100
        _,lease,_=db.claim_etf_kline_refresh(CODE,now,str(self.path))
        self.assertTrue(db.publish_etf_kline(CODE,lease,self.doc,db_path=str(self.path),completed_at=now+200))
        self.assertEqual(db.claim_etf_kline_refresh('159915.SZ',now+201,str(self.path))[0],'cooldown')

    def test_concurrent_collectors_get_exactly_one_global_lease(self):
        barrier=threading.Barrier(2)
        def claim(code):
            barrier.wait(timeout=5)
            return db.claim_etf_kline_refresh(code,NOW.timestamp()+100,str(self.path))[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(claim,[CODE,'159915.SZ']))
        self.assertCountEqual(statuses,['claimed','busy'])


class ETFCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'feed.db')
        self.calls=[]

    def query(self, endpoint, params):
        self.calls.append((endpoint,params))
        if endpoint=='trade_cal':
            start=dt.datetime.strptime(params['start_date'],'%Y%m%d').date()
            end=dt.datetime.strptime(params['end_date'],'%Y%m%d').date()
            return pd.DataFrame([dict(cal_date=d.replace('-',''),is_open=o,exchange='SSE') for d,o in calendar(start,end).items()])
        if endpoint=='fund_adj':
            return pd.DataFrame([dict(ts_code=CODE,trade_date=d.replace('-',''),adj_factor=1.005) for d,o in calendar(NOW.date()-dt.timedelta(days=600),NOW.date()).items() if o])
        if endpoint=='fund_daily':
            start=dt.datetime.strptime(params['start_date'],'%Y%m%d').date()
            end=dt.datetime.strptime(params['end_date'],'%Y%m%d').date()
            ends=[ek.grid(dt.date.fromisoformat(d),'D')[0] for d,o in calendar(start,end).items() if o]
        elif endpoint=='etf_mins':
            start=dt.datetime.fromisoformat(params['start_date']).date()
            end=dt.datetime.fromisoformat(params['end_date']).date()
            ends=[e for d,o in calendar(start,end).items() if o for e in ek.grid(dt.date.fromisoformat(d),params['freq'])]
        elif endpoint=='rt_etf_min_daily':
            period=params['freq'].lower()
            ends=[e for e in ek.grid(NOW.date(),period) if e <= getattr(self,'capture_time',NOW)]
        else:raise AssertionError(endpoint)
        return pd.DataFrame([dict(ts_code=CODE,trade_date=e.strftime('%Y%m%d'),trade_time=e.strftime('%Y-%m-%d %H:%M:%S'),
            open=.701,close=.702,high=.709,low=.699,vol=100,amount=70.2) for e in ends])

    def test_full_collection_atomic_three_periods_and_cooldown(self):
        result=collect_etf(CODE,self.path,NOW,query=self.query)
        self.assertEqual(result['status'],'collected',result)
        self.assertTrue(result['published'])
        self.assertGreater(result['samples']['D'],233)
        self.assertGreater(result['samples']['60min'],233)
        self.assertGreater(result['samples']['15min'],233)
        count=len(self.calls)
        self.assertEqual(collect_etf(CODE,self.path,NOW+dt.timedelta(seconds=1),query=self.query)['status'],'cooldown')
        self.assertEqual(len(self.calls),count)
        with sqlite3.connect(self.path) as conn:
            payload=json.loads(conn.execute('SELECT payload_json FROM etf_kline_cache').fetchone()[0])
            tables={r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
        self.assertEqual(tables,{'etf_kline_cache'})
        self.assertEqual(set(payload['periods']),set(ek.PERIODS))
        self.assertFalse(any('watch' in name for name in tables))

    def test_denied_minutes_publishes_explicit_partial_daily_not_fake_minutes(self):
        def query(endpoint,params):
            if endpoint in ('etf_mins','rt_etf_min_daily'):raise DataError('permission_denied')
            return self.query(endpoint,params)
        result=collect_etf(CODE,self.path,NOW,query=query)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['samples']['60min'],0)
        self.assertEqual(result['samples']['15min'],0)
        self.assertIn('etf_mins:permission_denied',result['errors'])
        with SnapshotReader(self.path).connect() as conn:
            output=ek.read_kline_data(conn,CODE,'15min',now=NOW)
        self.assertFalse(output['analysis_ready'])
        self.assertEqual(output['status'],'partial')
        self.assertIn('etf_mins:permission_denied',output['missing_reasons'])
        with SnapshotReader(self.path).connect() as conn:
            daily=ek.read_kline_data(conn,CODE,'D',now=NOW)
        self.assertTrue(daily['analysis_ready'],daily['missing_reasons'])

    def test_error_redaction_and_retained_old_payload_is_not_fresh(self):
        collect_etf(CODE,self.path,NOW,query=self.query)
        def fail(*args): raise RuntimeError('SECRET_TOKEN_MUST_NOT_LEAK')
        result=collect_etf(CODE,self.path,NOW+dt.timedelta(seconds=100),query=fail)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('SECRET',json.dumps(result))
        with SnapshotReader(self.path).connect() as conn:
            output=ek.read_kline_data(conn,CODE,'D',now=NOW+dt.timedelta(seconds=100))
        self.assertTrue(output['stale'])
        self.assertFalse(output['analysis_ready'])
        self.assertGreater(len(output['bars']),200)
        self.assertNotIn('SECRET',Path(self.path).read_bytes().decode(errors='ignore'))

    def test_intraday_two_real_captures_confirm_without_future_promotions(self):
        first=NOW.replace(hour=14,minute=10)
        self.capture_time=first
        self.assertEqual(collect_etf(CODE,self.path,first,query=self.query)['status'],'collected')
        with SnapshotReader(self.path).connect() as conn:
            data=ek.read_kline_data(conn,CODE,'60min',now=first)
        self.assertFalse(data['analysis_ready'])
        self.assertEqual(data['latest_unconfirmed_bar']['closure_state'],'awaiting_stability')
        second=first+dt.timedelta(seconds=100)
        self.capture_time=second
        self.assertEqual(collect_etf(CODE,self.path,second,query=self.query)['status'],'collected')
        with SnapshotReader(self.path).connect() as conn:
            data=ek.read_kline_data(conn,CODE,'60min',now=second)
        self.assertTrue(data['analysis_ready'],data['missing_reasons'])
        self.assertEqual(data['as_of'],'2026-11-11T14:00:00+08:00')
        self.assertEqual(data['bars'][-1]['observations'],2)

    def test_complete_post_close_history_needs_no_realtime_endpoint(self):
        def query(endpoint,params):
            if endpoint=='rt_etf_min_daily':raise AssertionError('unnecessary realtime call')
            return self.query(endpoint,params)
        result=collect_etf(CODE,self.path,NOW,query=query)
        self.assertEqual(result['status'],'collected',result)
        self.assertNotIn('rt_etf_min_daily',[endpoint for endpoint,_ in self.calls])

    def test_official_permission_denial_not_retried_with_another_frequency(self):
        query=OfficialQuery()
        with patch('services.kline55.safe_query',side_effect=DataError('permission_denied')) as call:
            for freq in ('60min','15min','60min'):
                with self.assertRaisesRegex(DataError,'permission_denied'):query('etf_mins',{'freq':freq})
        self.assertEqual(call.call_count,1)

    def test_official_endpoint_pacing(self):
        query=OfficialQuery()
        with patch('services.kline55.safe_query',return_value=pd.DataFrame()), \
             patch('services.etf_kline_feed.time.monotonic',side_effect=[100.,100.,110.,165.]), \
             patch('services.etf_kline_feed.time.sleep') as sleep:
            query('etf_mins',{'freq':'60min'})
            query('etf_mins',{'freq':'15min'})
        sleep.assert_called_once_with(55.)

    def test_cli_loop_reuses_denial_cache_not_new_query_each_round(self):
        from scripts.collect_etf_klines import main
        with patch('sys.argv',['collector','--code',CODE,'--db',self.path,'--loop']), \
             patch('scripts.collect_etf_klines.config.TS_TOKEN','test-only'), \
             patch('scripts.collect_etf_klines.OfficialQuery') as factory, \
             patch('scripts.collect_etf_klines.collect_etf',return_value={'status':'collected'}) as collect, \
             patch('scripts.collect_etf_klines.time.sleep',side_effect=[None,KeyboardInterrupt]), patch('builtins.print'):
            self.assertEqual(main(),0)
        factory.assert_called_once_with()
        self.assertEqual(collect.call_count,2)
        self.assertIs(collect.call_args_list[0].kwargs['query'],collect.call_args_list[1].kwargs['query'])

    def test_cli_one_shot_writes_isolated_db_with_injected_official_contract(self):
        from scripts.collect_etf_klines import main
        with patch('sys.argv',['collector','--code',CODE,'--db',self.path]), \
             patch('scripts.collect_etf_klines.config.TS_TOKEN','test-only'), \
             patch('scripts.collect_etf_klines.OfficialQuery',return_value=self.query), \
             patch('services.etf_kline_feed.local_now',return_value=NOW), \
             patch('services.etf_kline_feed.dt') as clock, patch('builtins.print') as printed:
            clock.datetime.now.return_value=NOW
            clock.timedelta=dt.timedelta
            clock.time=dt.time
            self.assertEqual(main(),0)
        output=json.loads(printed.call_args.args[0])
        self.assertTrue(output['published'])
        with SnapshotReader(self.path).connect() as conn:
            result=ek.read_kline_data(conn,CODE,'D',now=NOW)
        self.assertTrue(result['analysis_ready'],result['missing_reasons'])
        self.assertGreater(result['samples'],233)

    def test_failed_compare_and_swap_not_reported_as_collected(self):
        with patch('core.db.publish_etf_kline',return_value=False):
            result=collect_etf(CODE,self.path,NOW,query=self.query)
        self.assertEqual(result['status'],'superseded')
        self.assertFalse(result['published'])


if __name__=='__main__':unittest.main()
