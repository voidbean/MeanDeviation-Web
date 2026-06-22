"""
services/indicators.py — 技术指标计算 + 分时快照 + 揉搓线分析
不依赖 Tushare Pro API，仅读取本地 DB。
"""
import math
import sqlite3
import time

import tushare as ts

from core.config import DB_PATH, logger, COMMON_STOCKS

_INDEX_RT_CODES = [
    ("sh000001", "000001.SH"),
    ("399001",   "399001.SZ"),
    ("399006",   "399006.SZ"),
]


# ── 分时快照 ─────────────────────────────────────────────────────────────────

def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _is_trading_time() -> bool:
    import datetime
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (datetime.time(9, 30) <= t <= datetime.time(11, 30)) or \
           (datetime.time(13, 0) <= t <= datetime.time(15, 0))


def _save_intraday_snapshot(code: str, today: str, now_hhmm: str,
                             price: float, open_: float, high: float, low: float,
                             cum_vol: float, cum_amount_qianyuan: float) -> None:
    if price == 0 or cum_vol == 0:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            prev_row = conn.execute(
                "SELECT COALESCE(SUM(vol), 0), COALESCE(SUM(amount), 0) "
                "FROM intraday_snapshots WHERE code = ? AND date = ?",
                (code, today),
            ).fetchone()
            prev_cum_vol    = float(prev_row[0])
            prev_cum_amount = float(prev_row[1])
        finally:
            conn.close()
    except Exception as e:
        logger.error("intraday_fetch: 读取历史快照失败 code=%s %s", code, e)
        prev_cum_vol = prev_cum_amount = 0.0

    delta_vol    = max(cum_vol            - prev_cum_vol,    0.0)
    delta_amount = max(cum_amount_qianyuan - prev_cum_amount, 0.0)

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO intraday_snapshots "
                "(code, date, time, price, open, high, low, vol, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code, today, now_hhmm, price, open_, high, low, delta_vol, delta_amount),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("intraday_fetch: %s %s price=%.3f delta_vol=%.0f", code, now_hhmm, price, delta_vol)
    except Exception as e:
        logger.error("intraday_fetch: 写入失败 code=%s %s", code, e)


def _fetch_and_save_intraday_snapshots() -> None:
    if not COMMON_STOCKS:
        return
    import datetime
    today    = _today_str()
    now_hhmm = datetime.datetime.now().strftime("%H:%M")

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            import datetime as _dt
            cutoff = (_dt.date.today() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
            conn.execute("DELETE FROM intraday_snapshots WHERE date < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("intraday_fetch: 清理旧数据失败 %s", e)

    for item in COMMON_STOCKS:
        code = item["code"]
        try:
            df = ts.get_realtime_quotes(code)
        except Exception as e:
            logger.warning("intraday_fetch: get_realtime_quotes 失败 code=%s %s", code, e)
            continue
        if df is None or df.empty:
            continue
        try:
            price      = float(df.loc[0, "price"])
            high       = float(df.loc[0, "high"])
            low        = float(df.loc[0, "low"])
            open_      = float(df.loc[0, "open"])
            cum_vol    = float(df.loc[0, "volume"])
            cum_amount = float(df.loc[0, "amount"]) / 1000.0
        except Exception as e:
            logger.warning("intraday_fetch: 解析行情失败 code=%s %s", code, e)
            continue
        _save_intraday_snapshot(code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)

    for rt_code, store_code in _INDEX_RT_CODES:
        try:
            df = ts.get_realtime_quotes(rt_code)
        except Exception as e:
            logger.warning("intraday_fetch: 指数失败 code=%s %s", rt_code, e)
            continue
        if df is None or df.empty:
            continue
        try:
            price      = float(df.loc[0, "price"])
            high       = float(df.loc[0, "high"])
            low        = float(df.loc[0, "low"])
            open_      = float(df.loc[0, "open"])
            cum_vol    = float(df.loc[0, "volume"])
            cum_amount = float(df.loc[0, "amount"]) / 1000.0
        except Exception as e:
            logger.warning("intraday_fetch: 解析指数失败 code=%s %s", rt_code, e)
            continue
        _save_intraday_snapshot(store_code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)


def _intraday_bg_loop() -> None:
    logger.info("intraday_bg_loop: 后台线程已启动")
    while True:
        if _is_trading_time():
            logger.info("intraday_bg_loop: 开始抓取分时快照")
            _fetch_and_save_intraday_snapshots()
        time.sleep(60)


def _get_intraday_points(code: str) -> list:
    today = _today_str()
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT time, price, vol, amount FROM intraday_snapshots "
                "WHERE code = ? AND date = ? ORDER BY time ASC",
                (code, today),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error("_get_intraday_points: 读取失败 %s", e)
        return []

    cum_vol = cum_amount = 0.0
    points = []
    for t, price, vol, amount in rows:
        cum_vol    += vol    or 0.0
        cum_amount += amount or 0.0
        avg = round(cum_amount * 1000 / cum_vol, 4) if cum_vol > 0 else None
        points.append({"time": t, "price": round(price, 4), "avg": avg, "vol": vol or 0.0})
    return points


def _build_intraday_candles(points: list, window_minutes: int = 30) -> list:
    if not points:
        return []

    def _to_minutes(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    candles = []
    bucket_start  = None
    bucket_points = []

    for p in points:
        t_min = _to_minutes(p["time"])
        if bucket_start is None:
            offset       = t_min - 570
            bucket_idx   = offset // window_minutes
            bucket_start = 570 + bucket_idx * window_minutes
        elif t_min >= bucket_start + window_minutes:
            if bucket_points:
                prices = [x["price"] for x in bucket_points]
                candles.append({
                    "window_start": f"{bucket_start // 60:02d}:{bucket_start % 60:02d}",
                    "open":  bucket_points[0]["price"],
                    "close": bucket_points[-1]["price"],
                    "high":  max(prices),
                    "low":   min(prices),
                    "vol":   sum(x["vol"] for x in bucket_points),
                    "n":     len(bucket_points),
                })
            offset        = t_min - 570
            bucket_idx    = offset // window_minutes
            bucket_start  = 570 + bucket_idx * window_minutes
            bucket_points = []
        bucket_points.append(p)

    if bucket_points:
        prices = [x["price"] for x in bucket_points]
        candles.append({
            "window_start": f"{bucket_start // 60:02d}:{bucket_start % 60:02d}",
            "open":  bucket_points[0]["price"],
            "close": bucket_points[-1]["price"],
            "high":  max(prices),
            "low":   min(prices),
            "vol":   sum(x["vol"] for x in bucket_points),
            "n":     len(bucket_points),
        })
    return candles


# ── 揉搓线分析 ───────────────────────────────────────────────────────────────

def get_intraday_volatility_stats(code: str, n_days: int = 7) -> dict | None:
    """
    从历史分时快照计算该股近N个交易日的日内波动特征，用于指导条件单参数设置。
    返回：
      - avg_upper_shadow_pct: 近N日 (日内最高 - 收盘) / 收盘 均值，对应"冲高后典型回落幅度"
      - avg_lower_shadow_pct: 近N日 (收盘 - 日内最低) / 收盘 均值，对应"低点后典型反弹幅度"
      - avg_daily_range_pct:  近N日 (最高 - 最低) / 收盘 均值，对应"日内总振幅"
      - days_used: 实际用到的天数
    """
    bare_code = code.split(".")[0] if "." in code else code
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            import datetime as _dt
            cutoff = (_dt.date.today() - _dt.timedelta(days=n_days + 3)).strftime("%Y-%m-%d")
            rows = conn.execute(
                """
                SELECT date,
                       MAX(price) AS day_high,
                       MIN(price) AS day_low,
                       MAX(CASE WHEN time = (SELECT MAX(time) FROM intraday_snapshots s2
                                             WHERE s2.code = s1.code AND s2.date = s1.date)
                                THEN price END) AS day_close
                FROM intraday_snapshots s1
                WHERE code = ? AND date >= ?
                GROUP BY date
                ORDER BY date DESC
                LIMIT ?
                """,
                (bare_code, cutoff, n_days),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error("get_intraday_volatility_stats: DB error code=%s %s", code, e)
        return None

    if not rows:
        return None

    upper_shadows, lower_shadows, ranges = [], [], []
    for _date, day_high, day_low, day_close in rows:
        if not day_close or day_close <= 0:
            continue
        upper_shadows.append((day_high - day_close) / day_close * 100)
        lower_shadows.append((day_close - day_low) / day_close * 100)
        ranges.append((day_high - day_low) / day_close * 100)

    if not upper_shadows:
        return None

    return {
        "avg_upper_shadow_pct": round(sum(upper_shadows) / len(upper_shadows), 2),
        "avg_lower_shadow_pct": round(sum(lower_shadows) / len(lower_shadows), 2),
        "avg_daily_range_pct":  round(sum(ranges) / len(ranges), 2),
        "days_used": len(upper_shadows),
        "source": "intraday",
    }


def get_daily_volatility_stats(code: str, n_days: int = 20) -> dict | None:
    """
    从日K记录计算波动特征，作为分时数据缺失时的 fallback。
    上影线用 (high - close) / close，下影线用 (close - low) / close。
    """
    bare_code = code.split(".")[0] if "." in code else code
    ts_code   = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, bare_code] if ts_code else [bare_code]):
            if not q:
                continue
            cur = conn.execute(
                "SELECT high, low, close FROM daily_records "
                "WHERE code = ? AND high > 0 AND low > 0 AND close > 0 "
                "ORDER BY date DESC LIMIT ?",
                (q, n_days),
            )
            rows = cur.fetchall()
            if rows:
                break
        conn.close()
    except Exception as e:
        logger.error("get_daily_volatility_stats: DB error code=%s %s", code, e)
        return None

    if not rows:
        return None

    upper_shadows, lower_shadows, ranges = [], [], []
    for high, low, close in rows:
        upper_shadows.append((high - close) / close * 100)
        lower_shadows.append((close - low)  / close * 100)
        ranges.append((high - low) / close * 100)

    return {
        "avg_upper_shadow_pct": round(sum(upper_shadows) / len(upper_shadows), 2),
        "avg_lower_shadow_pct": round(sum(lower_shadows) / len(lower_shadows), 2),
        "avg_daily_range_pct":  round(sum(ranges) / len(ranges), 2),
        "days_used": len(upper_shadows),
        "source": "daily_kline",
    }


def get_volatility_stats(code: str) -> dict | None:
    """优先使用分时快照，没有则 fallback 到日K影线法。"""
    stats = get_intraday_volatility_stats(code, n_days=7)
    if stats:
        return stats
    return get_daily_volatility_stats(code, n_days=20)


def analyze_rousu_lines(records: list, n: int = 10, label: str = "日K") -> list:
    INTERPRETATIONS = {
        ("下跌趋势", "黑K", "下影接上影"): "中继下跌",
        ("下跌趋势", "红K", "下影接上影"): "支撑位震荡选方向",
        ("下跌趋势", "红K", "上影接下影"): "支撑位资金抢反弹",
        ("下跌趋势", "黑K", "上影接下影"): "短期止跌",
        ("上涨趋势", "黑K", "下影接上影"): "开始有分歧",
        ("上涨趋势", "红K", "下影接上影"): "分歧但强势继续看新高",
        ("上涨趋势", "红K", "上影接下影"): "承接力度大，但只承接不追高",
        ("上涨趋势", "黑K", "上影接下影"): "承接低，可能出现短期顶",
    }
    results = []
    for i, rec in enumerate(records[:n]):
        try:
            o = float(rec["open"]); c = float(rec["close"])
            h = float(rec["high"]); lo = float(rec["low"])
        except (KeyError, TypeError, ValueError):
            continue
        candle_range = h - lo
        if candle_range < 1e-6:
            continue
        body_ratio         = abs(c - o) / candle_range
        upper_shadow_ratio = (h - max(o, c)) / candle_range
        lower_shadow_ratio = (min(o, c) - lo) / candle_range
        if not (body_ratio < 0.4 and upper_shadow_ratio > 0.2 and lower_shadow_ratio > 0.2):
            continue
        prior_slice = records[i + 1: i + 6]
        if len(prior_slice) < 5:
            continue
        prior_closes = []
        for r in prior_slice:
            try:
                prior_closes.append(float(r["close"]))
            except (KeyError, TypeError, ValueError):
                pass
        if len(prior_closes) < 3:
            continue
        prior_avg     = sum(prior_closes) / len(prior_closes)
        trend         = "上涨趋势" if c > prior_avg else "下跌趋势"
        color         = "红K" if c >= o else "黑K"
        open_position = (o - lo) / candle_range
        shadow_order  = "下影接上影" if open_position > 0.5 else "上影接下影"
        interpretation = INTERPRETATIONS.get((trend, color, shadow_order), "未知形态")
        date_key = rec.get("date") or rec.get("window_start") or f"index-{i}"
        results.append({
            "date":           date_key,
            "label":          label,
            "trend":          trend,
            "color":          color,
            "shadow_order":   shadow_order,
            "interpretation": interpretation,
            "body_ratio":     round(body_ratio, 3),
            "open_position":  round(open_position, 3),
        })
    return results


def analyze_rousu_lines_intraday(code: str, date: str = None) -> list:
    if not date:
        date = _today_str()
    bare_code = code.split(".")[0]
    rows = []
    for query_code in [bare_code, code]:
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                rows = conn.execute(
                    "SELECT time, price, vol, amount FROM intraday_snapshots "
                    "WHERE code = ? AND date = ? ORDER BY time ASC",
                    (query_code, date),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.error("analyze_rousu_lines_intraday: DB error code=%s %s", query_code, e)
        if rows:
            break

    if not rows:
        return []

    points  = [{"time": r[0], "price": r[1], "vol": r[2] or 0.0, "amount": r[3] or 0.0} for r in rows]
    candles = _build_intraday_candles(points, window_minutes=30)
    if not candles:
        return []
    return analyze_rousu_lines(list(reversed(candles)), n=len(candles), label="30分钟K")


def _get_daily_records_for_rousu(code: str, n: int = 15) -> list:
    short_code = code.split(".")[0]
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT date, COALESCE(open, close) AS open, close, high, low "
            "FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT ?",
            (short_code, n),
        ).fetchall()
        conn.close()
        return [{"date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4]} for r in rows]
    except Exception:
        return []


# ── 技术指标计算 ─────────────────────────────────────────────────────────────

def _calc_macd(code: str) -> dict | None:
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            rows = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date ASC LIMIT 90", (q,)
            ).fetchall()
            if rows:
                break
        conn.close()
    except Exception:
        return None

    if not rows or len(rows) < 35:
        return None

    dates  = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows]
    n      = len(closes)

    def _ema(data, period):
        k = 2 / (period + 1)
        out = [data[0]] * len(data)
        for i in range(1, len(data)):
            out[i] = data[i] * k + out[i - 1] * (1 - k)
        return out

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif   = [ema12[i] - ema26[i] for i in range(n)]
    dea   = _ema(dif, 9)
    hist  = [dif[i] - dea[i] for i in range(n)]

    latest = {"date": dates[-1], "dif": round(dif[-1], 4), "dea": round(dea[-1], 4), "hist": round(hist[-1], 4)}

    WIN   = min(40, n)
    seg_c = closes[-WIN:]
    seg_h = hist[-WIN:]
    seg_d = dates[-WIN:]

    def find_peaks(arr, mode="high"):
        pts = []
        for i in range(2, len(arr) - 2):
            if mode == "high":
                if arr[i] > arr[i-1] and arr[i] > arr[i-2] and arr[i] > arr[i+1] and arr[i] > arr[i+2]:
                    pts.append(i)
            else:
                if arr[i] < arr[i-1] and arr[i] < arr[i-2] and arr[i] < arr[i+1] and arr[i] < arr[i+2]:
                    pts.append(i)
        return pts

    divergence = div_detail = None
    peak_idx = find_peaks(seg_c, "high")
    if len(peak_idx) >= 2:
        i1, i2 = peak_idx[-2], peak_idx[-1]
        if seg_c[i2] > seg_c[i1] and seg_h[i2] < seg_h[i1]:
            divergence = "top"
            div_detail = (f"价格高点 {seg_d[i1]}({seg_c[i1]:.2f}) → {seg_d[i2]}({seg_c[i2]:.2f}) 创新高，"
                          f"但 MACD 柱 {seg_h[i1]:.4f} → {seg_h[i2]:.4f} 未同步新高，上涨动能衰竭，警惕回调。")

    if divergence is None:
        trough_idx = find_peaks(seg_c, "low")
        if len(trough_idx) >= 2:
            i1, i2 = trough_idx[-2], trough_idx[-1]
            if seg_c[i2] < seg_c[i1] and seg_h[i2] > seg_h[i1]:
                divergence = "bottom"
                div_detail = (f"价格低点 {seg_d[i1]}({seg_c[i1]:.2f}) → {seg_d[i2]}({seg_c[i2]:.2f}) 创新低，"
                              f"但 MACD 柱 {seg_h[i1]:.4f} → {seg_h[i2]:.4f} 未同步新低，下跌动能衰竭，关注反弹机会。")

    cross = None
    for i in range(n - 1, max(n - 10, 0), -1):
        if hist[i] > 0 and hist[i - 1] <= 0:
            cross = {"type": "golden", "date": dates[i], "label": "金叉（DIF上穿DEA）"}
            break
        if hist[i] < 0 and hist[i - 1] >= 0:
            cross = {"type": "dead", "date": dates[i], "label": "死叉（DIF下穿DEA）"}
            break

    return {
        "latest": latest, "divergence": divergence, "div_detail": div_detail or "",
        "cross": cross, "above_zero": dif[-1] > 0,
        "series": {
            "dates": dates[-20:],
            "dif":   [round(v, 4) for v in dif[-20:]],
            "dea":   [round(v, 4) for v in dea[-20:]],
            "hist":  [round(v, 4) for v in hist[-20:]],
        },
    }


def _get_benchmark_index(short_code: str) -> str:
    if short_code.startswith("60"):
        return "000001.SH"
    elif short_code.startswith("00"):
        return "399001.SZ"
    elif short_code.startswith("30"):
        return "399006.SZ"
    elif short_code.startswith("68"):
        return "000688.SH"
    return "000001.SH"


def _calc_yidong(code: str, current_price: float) -> dict | None:
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            rows = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date DESC LIMIT 31", (q,)
            ).fetchall()
            if rows:
                break
        if not rows or len(rows) < 31:
            conn.close()
            return None
        base_date, base_close = rows[-1][0], float(rows[-1][1])
        if base_close <= 0:
            conn.close()
            return None

        index_code = _get_benchmark_index(short_code)
        idx_rows = conn.execute(
            "SELECT date, close FROM daily_records WHERE code=? ORDER BY date DESC LIMIT 31", (index_code,)
        ).fetchall()
        conn.close()
        index_base = index_current = None
        if idx_rows and len(idx_rows) >= 31:
            index_current = float(idx_rows[0][1])
            index_base    = float(idx_rows[-1][1])
    except Exception:
        return None

    r_stock = round((current_price - base_close) / base_close * 100, 4)
    if index_base and index_base > 0 and index_current and index_current > 0:
        r_index     = round((index_current - index_base) / index_base * 100, 4)
        deviation   = round(r_stock - r_index, 4)
        yidong_line = round(base_close * (1 + (2.0 + r_index / 100)), 4)
        fallback    = False
    else:
        r_index = None
        deviation   = r_stock
        yidong_line = round(base_close * 3.0, 4)
        index_base = index_current = None
        fallback    = True

    pct_to_line = round((current_price - yidong_line) / yidong_line * 100, 2)
    alert     = (current_price >= yidong_line * 0.9) if fallback else (deviation >= 180.0)
    triggered = (current_price >= yidong_line)       if fallback else (deviation >= 200.0)

    limit_rate           = 0.20 if short_code.startswith("68") else 0.10
    next_day_gap_pct     = round((yidong_line - current_price) / current_price * 100, 2)
    next_day_limit_price = round(current_price * (1 + limit_rate), 2)
    boards_needed = (math.ceil(math.log(yidong_line / current_price) / math.log(1 + limit_rate))
                     if yidong_line > current_price and current_price > 0 else 0)

    return {
        "base_date": base_date, "base_close": round(base_close, 4),
        "yidong_line": yidong_line, "pct_to_line": pct_to_line,
        "r_stock": r_stock, "r_index": r_index, "deviation": round(deviation, 2),
        "index_code": index_code,
        "index_base":    round(index_base, 2) if index_base else None,
        "index_current": round(index_current, 2) if index_current else None,
        "alert": alert, "triggered": triggered, "fallback": fallback,
        "next_day_gap_pct": next_day_gap_pct,
        "next_day_limit_price": next_day_limit_price,
        "next_day_boards_needed": boards_needed,
        "limit_rate": limit_rate,
    }


def _calc_boll(code: str) -> dict | None:
    ts_code    = code if "." in code else None
    short_code = code.split(".")[0] if "." in code else code
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q_code in ([ts_code, short_code] if ts_code else [short_code]):
            if q_code is None:
                continue
            rows = conn.execute(
                "SELECT date, close FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT 60", (q_code,)
            ).fetchall()
            if rows:
                break
        conn.close()
    except Exception:
        return None

    if not rows or len(rows) < 5:
        return None

    rows   = list(reversed(rows))
    closes = [r[1] for r in rows if r[1] is not None]
    n      = len(closes)

    def sma(data, period):
        return round(sum(data[-period:]) / period, 4) if len(data) >= period else None

    def boll_calc(data, period=20, k=2.0):
        if len(data) < period:
            return None, None, None
        w   = data[-period:]
        mid = sum(w) / period
        std = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
        return round(mid + k * std, 4), round(mid, 4), round(mid - k * std, 4)

    upper, mid, lower = boll_calc(closes)
    ma5   = sma(closes, 5)
    ma10  = sma(closes, 10)
    ma20  = sma(closes, 20)
    latest = closes[-1]

    if upper and lower and mid:
        if latest >= upper:
            position, advice_class = "上轨附近（超买，注意压力）", "danger"
        elif latest <= lower:
            position, advice_class = "下轨附近（超卖，关注支撑）", "success"
        elif latest > mid:
            position, advice_class = "中轨↑上轨（强势区间）", "warning"
        else:
            position, advice_class = "下轨↑中轨（弱势区间）", "secondary"
    else:
        position, advice_class = "数据不足", "secondary"

    ma_series_dates = []
    ma_series_ma5   = []
    ma_series_ma10  = []
    ma_series_ma20  = []
    for i in range(max(0, n - 20), n):
        ma_series_dates.append(rows[i][0])
        offset = i + 1
        ma_series_ma5.append(round(sum(closes[max(0, offset - 5):offset]) / min(5, offset), 4))
        ma_series_ma10.append(round(sum(closes[max(0, offset - 10):offset]) / min(10, offset), 4))
        ma_series_ma20.append(round(sum(closes[max(0, offset - 20):offset]) / min(20, offset), 4))

    return {
        "upper": upper, "mid": mid, "lower": lower,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "position": position, "advice_class": advice_class, "data_points": n,
        "recent_closes": [{"date": rows[i][0], "close": rows[i][1]} for i in range(max(0, n - 20), n)],
        "ma_series": {"dates": ma_series_dates, "ma5": ma_series_ma5,
                      "ma10": ma_series_ma10, "ma20": ma_series_ma20},
    }
