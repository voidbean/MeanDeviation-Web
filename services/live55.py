"""Official raw candles -> aligned qfq -> closed-bar MA55 observations.

Quote snapshots are deliberately NOT a candle source. All upstream failures and
unproven calendars/factors fail closed. No trades or synthetic fills are created.
"""
import datetime as dt
import json
import math
import time

from core import db
from services.kline55 import DataError, ERRORS, TZ, normalize, safe_query
from services.signals55 import HOURS, analyze


class NotReady(Exception):
    pass


def raw_bars(frame, code, period, source):
    if frame is None or frame.empty:
        raise NotReady('接口没有返回K线')
    if len(frame) >= (6000 if period == 'D' else 8000):
        raise NotReady('接口可能截断，请分段回补')
    output, seen = [], set()
    for row in frame.to_dict('records'):
        if row.get('ts_code', row.get('code')) != code:
            raise NotReady('返回股票代码不一致')
        stamp = str(row.get('trade_date') if period == 'D' else row.get('trade_time', row.get('time')))
        dt.datetime.strptime(stamp, '%Y%m%d' if period == 'D' else '%Y-%m-%d %H:%M:%S')
        if stamp in seen:
            raise NotReady('返回K线时间重复')
        seen.add(stamp)
        bar = {'time': stamp}
        for key in ('open', 'high', 'low', 'close'):
            value = float(row[key])
            if not math.isfinite(value) or value <= 0:
                raise NotReady('K线价格无效')
            bar[key] = value
        if not bar['low'] <= min(bar['open'],bar['close']) <= max(bar['open'],bar['close']) <= bar['high']:
            raise NotReady('K线高低价关系无效')
        for key, dest, scale in [('vol', 'volume_shares', 100 if period == 'D' else 1),
                                 ('amount', 'amount_yuan', 1000 if period == 'D' else 1)]:
            value = float(row[key]) * scale
            if not math.isfinite(value) or value < 0:
                raise NotReady('K线成交量额无效')
            bar[dest] = value
        bar['source'] = source
        output.append(bar)
    return sorted(output, key=lambda b: b['time'])


def factors_map(frame, code, dates):
    if frame is None or frame.empty:
        raise NotReady('复权因子尚未就绪')
    result = {}
    for r in frame.to_dict('records'):
        day, value = str(r['trade_date']), float(r['adj_factor'])
        if r['ts_code'] != code or day in result or not math.isfinite(value) or value <= 0:
            raise NotReady('复权因子无效')
        result[day] = value
    if not set(dates).issubset(result):
        raise NotReady('复权因子覆盖不足（包含当日），禁止混用价格基准')
    return result


def adjusted(code, period, bars, factors, anchor):
    out, closes = [], []
    for raw in bars:
        day = raw['time'][:10].replace('-', '')
        ratio = factors[day] / factors[anchor]
        b = dict(raw)
        for key in ('open', 'high', 'low', 'close'):
            b[key] *= ratio
        closes.append(b['close'])
        for n in (5, 10, 20, 55, 233):
            b[f'ma{n}'] = sum(closes[-n:]) / n if len(closes) >= n else None
        out.append(b)
    return dict(code=code, period=period, adjustment='qfq', adjustment_anchor=anchor, bars=out)


def verify_schedule(daily, hours, calendar, today, cutoff):
    if calendar is None or calendar.empty or calendar['cal_date'].duplicated().any():
        raise NotReady('交易日历缺失或重复')
    days = {str(r['cal_date']): int(r['is_open']) for r in calendar.to_dict('records')}
    start = dt.datetime.strptime(daily[0]['time'], '%Y%m%d').date()
    span = [(start + dt.timedelta(days=i)).strftime('%Y%m%d') for i in range((today - start).days + 1)]
    if not set(span).issubset(days) or days[today.strftime('%Y%m%d')] != 1:
        raise NotReady('交易日历不完整或非交易日')
    expected_days = [d for d in span if days[d] == 1 and d < today.strftime('%Y%m%d')]
    if [b['time'] for b in daily] != expected_days:
        raise NotReady('日线缺口或停牌待核验，暂停确认')
    # Keep the whole retained hourly window on an exact official exchange grid.
    first_day = hours[0]['time'][:10].replace('-', '')
    expected = [f'{d[:4]}-{d[4:6]}-{d[6:]} {h}' for d in span
                if days[d] == 1 and d >= first_day for h in HOURS
                if f'{d[:4]}-{d[4:6]}-{d[6:]} {h}' <= cutoff.strftime('%Y-%m-%d %H:%M:%S')]
    if [b['time'] for b in hours] != expected:
        raise NotReady('60分钟K线缺口或时间边界不符，等待官方数据补齐')


def sync_one(subscription, now=None, query=safe_query):
    now = now or dt.datetime.now(TZ)
    now = now.astimezone(TZ)
    code, _ = normalize(subscription['code'], '60min')
    today = now.strftime('%Y%m%d')
    yesterday = (now.date() - dt.timedelta(days=1)).strftime('%Y%m%d')
    context = json.loads(subscription.get('context_json') or '{}')
    # Once per session, reconcile historical raw bars. No qfq series is stitched.
    if context.get('history_date') != today:
        start = (now.date() - dt.timedelta(days=600)).strftime('%Y%m%d')
        daily = raw_bars(query('daily', dict(ts_code=code, start_date=start, end_date=yesterday)), code, 'D', 'daily')
        hstart = (now.date() - dt.timedelta(days=120)).strftime('%Y-%m-%d') + ' 09:00:00'
        hours = raw_bars(query('stk_mins', dict(ts_code=code, freq='60min', start_date=hstart,
            end_date=(now.date() - dt.timedelta(days=1)).strftime('%Y-%m-%d') + ' 15:00:00')), code, '60min', 'stk_mins')
        if any(b['time'] > yesterday for b in daily) or any(b['time'][:10] >= now.strftime('%Y-%m-%d') for b in hours):
            raise NotReady('历史接口返回越界K线')
        db.live55_save_bars(code, 'D', daily, 'daily', (start, yesterday))
        db.live55_save_bars(code, '60min', hours, 'stk_mins', (hstart, now.strftime('%Y-%m-%d') + ' 00:00:00'))
        context = dict(history_date=today, daily_start=daily[0]['time'], hour_start=hours[0]['time'])
        db.live55_update(code, '历史回补完成，等待当日标准K线', context)
    daily = [b for b in db.live55_load_bars(code, 'D') if context['daily_start'] <= b['time'] < today]
    source = 'rt_min_daily'
    try:
        frame = query(source, dict(ts_code=code, freq='60MIN'))
    except DataError as exc:
        if str(exc) != 'permission_denied':
            raise
        source = 'rt_min'
        frame = query(source, dict(ts_code=code, freq='60MIN'))
    current = raw_bars(frame, code, '60min', source)
    if any(b['time'][:10] != now.strftime('%Y-%m-%d') for b in current):
        raise NotReady('实时接口数据日期不是今天')
    prior = {b['time']: b for b in db.live55_load_bars(code, '60min')}
    fields = ('open', 'high', 'low', 'close', 'volume_shares', 'amount_yuan')
    for bar in current:
        old = prior.get(bar['time'], {})
        unchanged = all(old.get(k) == bar[k] for k in fields)
        bar['observations'] = old.get('observations', 0) + 1 if unchanged else 1
        bar['stable_since'] = old.get('stable_since', now.timestamp()) if unchanged else now.timestamp()
    db.live55_save_bars(code, '60min', current, source)
    # Timestamp alone is not enough: wait 90 seconds after the documented grid end.
    cutoff = (now - dt.timedelta(seconds=90)).replace(tzinfo=None)
    hours = [b for b in db.live55_load_bars(code, '60min')
             if context['hour_start'] <= b['time'] <= cutoff.strftime('%Y-%m-%d %H:%M:%S')]
    if any(b['time'][:10] == now.strftime('%Y-%m-%d') and
           (b.get('observations', 0) < 2 or now.timestamp() - b.get('stable_since', now.timestamp()) < 30) for b in hours):
        raise NotReady('已过收线时间，等待第二次采集确认K线稳定')
    if len(daily) < 60 or len(hours) < 55:
        raise NotReady('日线或60分钟历史数量不足')
    calendar = query('trade_cal', dict(exchange='SSE', start_date=daily[0]['time'], end_date=today))
    verify_schedule(daily, hours, calendar, now.date(), cutoff)
    factors = factors_map(query('adj_factor', dict(ts_code=code, start_date=daily[0]['time'], end_date=today)),
                          code, [b['time'] for b in daily] + [b['time'][:10].replace('-', '') for b in hours] + [today])
    h = adjusted(code, '60min', hours, factors, today)
    d = adjusted(code, 'D', daily, factors, today)
    result = analyze(h, d, now=now, closed_before=cutoff)
    # Quality gates apply before any baseline or event is advanced.
    if result['status'] != 'evaluated':
        raise NotReady(result['reason'])
    result.update(historical_only=False, notifications_enabled=True)
    for event in result['events']:
        event['historical_only'] = False
    events = db.live55_record(code, result, now)
    db.live55_update(code, '【55线】' + result['label'].replace('历史', '') + ' · ' + str(result['as_of']), context)
    from services.monitor import publish_events
    publish_events(events)
    return result, events


def run_once(now=None):
    now = now or dt.datetime.now(TZ)
    if now.weekday() > 4 or not '09:30' <= now.strftime('%H:%M') <= '15:15':
        return
    subscription = db.live55_claim(now.timestamp(), lease_seconds=600)
    if not subscription:
        return
    try:
        sync_one(subscription, now)
    except DataError as exc:
        db.live55_update(subscription['code'], '待确认：' + ERRORS.get(str(exc), '行情请求失败'))
    except NotReady as exc:
        db.live55_update(subscription['code'], '待确认：' + str(exc))
    except Exception:
        # Never expose provider payloads, credentials or tracebacks in user status.
        db.live55_update(subscription['code'], '待确认：采集或校验失败，保留原始数据')
    finally:
        db.live55_release(time.time() + 90)


def background_loop(stop):
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            pass  # Isolated from the existing quote/notification worker.
        stop.wait(5)
