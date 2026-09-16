"""Auditable index price/turnover context. Never sum overlapping indexes."""
import datetime as dt
import math
import sqlite3
import statistics

INDEXES = (("000001.SH", "上证指数"), ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"))
MARKET_GUIDANCE = """market_context为指数量价背景。成交额单位亿元，各指数口径不可相加为两市总量。
预计全天成交额不是实际成交额；linear_time为低置信度外推，不能当作已确认放量。
同时间成交额比>=1.2或<=0.8仅是可审计的启发式分界，未经收益回测，不代表买卖信号。
缩量上涨不等于诱多，放量上涨不保证持续；结合位置、冲高回撤和个股走势。
疑似诱多只能表述为冲高回落风险，引用数据和时间，列出确认/失效条件，不预测顶部或保证小赚。
已持仓仅提示核对可卖数量后保护浮盈/评估减仓；未持仓提示勿追，不建议买入参与诱多。
指数缺失或stale时不得臆测大盘，不阻塞止损等风险退出提醒。
"""


def _positive(x):
    return x is not None and math.isfinite(float(x)) and float(x) > 0


def _minutes(t):
    h, m = map(int, t.split(":"))
    v = h * 60 + m
    return max(0, min(v, 690) - 570) + max(0, min(v, 900) - 780)


def load_market_context(path, trade_date=None):
    with sqlite3.connect(path) as conn:
        return build_market_context(conn, trade_date)


def build_market_context(conn, trade_date=None, now=None):
    # Supplemental context must never suppress a stock risk-exit notification.
    try:
        return _build_market_context(conn, trade_date, now)
    except (sqlite3.DatabaseError, ValueError, TypeError, OverflowError):
        return {"indexes": {}, "status": "unavailable", "total_market_amount_yi": None,
                "note": "大盘数据读取或格式异常，不作量价判断"}


def _build_market_context(conn, trade_date=None, now=None):
    now = now or dt.datetime.now()
    day = min(trade_date or now.date().isoformat(), now.date().isoformat())
    result = {"date": day, "indexes": {}, "total_market_amount_yi": None,
              "note": "各指数独立口径，非两市总额；历史同时间优先，样本不足使用低置信度线性外推。"}
    for code, name in INDEXES:
        item = {"name": name, "status": "missing"}
        result["indexes"][code] = item
        rows = conn.execute("SELECT time,price,high,amount FROM intraday_snapshots WHERE code=? AND date=? AND time<=? ORDER BY time",
                            (code, day, now.strftime("%H:%M") if day == now.date().isoformat() else "15:00")).fetchall()
        if not rows:
            continue
        t, price = rows[-1][0], rows[-1][1]
        valid = _positive(price) and all(r[3] is not None and math.isfinite(float(r[3])) and float(r[3]) >= 0 for r in rows)
        if not valid:
            continue
        amount = sum(float(r[3]) for r in rows)
        previous = conn.execute("SELECT date,close FROM daily_records WHERE code=? AND date<? ORDER BY date DESC LIMIT 1", (code, day)).fetchone()
        prev = previous[1] if previous and (dt.date.fromisoformat(day)-dt.date.fromisoformat(previous[0])).days <= 7 else None
        change = (price / prev - 1) * 100 if _positive(prev) else None
        high = max(float(r[2]) for r in rows if _positive(r[2])) if any(_positive(r[2]) for r in rows) else price
        drawdown = (price / high - 1) * 100
        stale = day != now.date().isoformat() or _minutes(now.strftime("%H:%M")) - _minutes(t) > 3
        fractions, amounts, dates = [], [], []
        history = conn.execute("SELECT date,amount FROM daily_records WHERE code=? AND date<? ORDER BY date DESC LIMIT 20", (code, day)).fetchall()
        for hist_day, full in history:
            if not _positive(full):
                continue
            at = conn.execute("SELECT time,amount FROM intraday_snapshots WHERE code=? AND date=? AND time<=? ORDER BY time", (code, hist_day, t)).fetchall()
            if not at or at[-1][0] != t or any(r[1] is None or not math.isfinite(float(r[1])) or float(r[1]) < 0 for r in at):
                continue
            subtotal = sum(float(r[1]) for r in at)
            if 0 < subtotal <= full:
                fractions.append(subtotal / full)
                amounts.append(subtotal)
                dates.append(hist_day)
        elapsed = _minutes(t)
        estimate, method = None, "unavailable"
        if not stale and elapsed >= 30 and amount > 0:
            if elapsed == 240:
                estimate, method = amount, "close_snapshot"
            elif len(fractions) >= 3:
                estimate, method = amount / statistics.median(fractions), "historical_same_time"
            else:
                estimate, method = amount * 240 / elapsed, "linear_time"
        ratio = amount / statistics.median(amounts) if len(amounts) >= 3 and not stale else None
        regime = "unknown"
        if change is not None and ratio is not None:
            regime = ("放量" if ratio >= 1.2 else "缩量" if ratio <= 0.8 else "平量") + ("上涨" if change > 0 else "下跌" if change < 0 else "平盘")
        warning = None
        if not stale and change is not None and change > 0 and drawdown <= -0.5:
            warning = f"{name}仍上涨{change:.2f}%，但较日内高点回落{-drawdown:.2f}%；冲高回落风险（非诱多定论）。"
        item.update(status="stale" if stale else "ok", as_of=t, change_pct=change,
                    drawdown_from_high_pct=drawdown, amount_yi=amount / 100000,
                    same_time_amount_ratio=ratio, sample_dates=dates, regime=regime,
                    estimated_full_day_amount_yi=estimate / 100000 if estimate is not None else None,
                    estimate_method=method, estimate_confidence="low" if method == "linear_time" else "reference_only",
                    warning=warning)
    return result


def market_warning_text(context):
    warnings = [i["warning"] for i in context["indexes"].values() if i.get("warning")]
    return (" 大盘风险背景：" + "；".join(warnings) + " 不预测回落时点；已有持仓请核对可卖数量并评估保护浮盈，未持仓勿据此追涨。") if warnings else ""
