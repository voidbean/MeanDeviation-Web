"""Local point-in-time watch context and auditable shadow checks.

Daily bars are closed bars only. Intraday volume is delta shares, not lots;
missing snapshots must not masquerade as full one-minute observations.
"""
import datetime as dt
import math
import sqlite3

from core.watch_conditions import CONDITION_LABELS
from services.indicators import _compute_macd_from_closes


def _positive(value):
    return value is not None and math.isfinite(float(value)) and float(value) > 0


def load_watch_context(db_path, code, trade_date):
    conn = sqlite3.connect(db_path)
    try:
        return build_watch_context(conn, code, trade_date)
    finally:
        conn.close()


def build_watch_context(conn, code, trade_date, now=None):
    now = now or dt.datetime.now()
    # Today's partially written daily row must never enter a closed-bar MACD.
    closed_before = now.date() + dt.timedelta(days=int(now.time() >= dt.time(15, 0)))
    cutoff = min(trade_date, closed_before.isoformat())
    rows = conn.execute(
        "SELECT date,open,high,low,close,vol,amount FROM daily_records "
        "WHERE code=? AND date<? ORDER BY date DESC LIMIT 120", (code, cutoff),
    ).fetchall()[::-1]
    bars = [dict(zip(("date", "open", "high", "low", "close", "volume_shares", "amount_qianyuan"), r))
            for r in rows]
    closes = [float(r[4]) for r in rows if _positive(r[4])]
    macd = None
    if len(closes) >= 35 and len(closes) == len(rows):
        dif, dea, hist = _compute_macd_from_closes(closes)
        macd = {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1], "previous_hist": hist[-2],
                "as_of": rows[-1][0], "period": "closed_daily", "parameters": [12, 26, 9],
                "source": "local_close_ema_first_close", "samples": len(closes)}
    daily_ratio = None
    if len(rows) >= 21 and all(_positive(r[5]) for r in rows[-21:]):
        daily_ratio = float(rows[-1][5]) / (sum(float(r[5]) for r in rows[-21:-1]) / 20)
    intraday = intraday_context(conn, code, min(trade_date, now.date().isoformat()))
    return {"daily": {"bars": bars[-30:], "as_of": rows[-1][0] if rows else None,
                      "volume_ratio_1d_20d": daily_ratio, "macd": macd,
                      "note": "仅已收盘日线；成交量单位股，旧数据未回填为null；MACD仅作背景，不是买卖硬门槛"},
            "intraday": intraday}


def intraday_context(conn, code, trade_date):
    rows = conn.execute(
        "SELECT time,price,vol,amount FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time",
        (code, trade_date),
    ).fetchall()
    volume = sum(float(r[2] or 0) for r in rows)
    amount = sum(float(r[3] or 0) for r in rows)
    vwap = amount * 1000 / volume if volume > 0 and amount > 0 else None
    ratio, reason = None, "需要24个连续分钟快照（含区间起点），缺失或跨午休不作分钟量比较"
    window = rows[-24:]
    if len(window) == 24:
        times = [dt.datetime.strptime(r[0], "%H:%M") for r in window]
        contiguous = all((b - a).total_seconds() == 60 for a, b in zip(times, times[1:]))
        valid = all(r[2] is not None and math.isfinite(float(r[2])) and float(r[2]) >= 0 for r in window[1:])
        if contiguous and valid:
            baseline = sum(float(r[2]) for r in window[1:21]) / 20
            if baseline > 0:
                ratio = (sum(float(r[2]) for r in window[21:]) / 3) / baseline
                reason = "近3分钟均量/此前20分钟均量；首个快照仅作区间起点"
    return {"date": trade_date, "as_of": rows[-1][0] if rows else None,
            "vwap": vwap, "total_volume_shares": volume,
            "volume_ratio_3m_20m": ratio, "volume_note": reason,
            "recent_minutes": [dict(zip(("time", "price", "volume_shares", "amount_qianyuan"), r))
                               for r in rows[-30:]]}


def evaluate_conditions(conditions, context, price, trade_date):
    intraday, daily = context["intraday"], context["daily"]
    macd = daily["macd"]
    macd_fresh = macd and 0 <= (dt.date.fromisoformat(trade_date) - dt.date.fromisoformat(macd["as_of"])).days <= 7
    values = {"volume_ratio_3m_20m": intraday["volume_ratio_3m_20m"],
              "price_vs_vwap": price / intraday["vwap"] if intraday["vwap"] else None,
              "daily_macd_hist": macd["hist"] if macd_fresh else None}
    checks = []
    for condition in conditions:
        metric = condition["metric"]
        actual = values[metric]
        passed = actual is not None and (actual >= condition["value"] if condition["op"] == "gte" else actual <= condition["value"])
        checks.append({**condition, "actual": actual,
                       "status": "unknown" if actual is None else "met" if passed else "unmet",
                       "label": CONDITION_LABELS[metric],
                       "as_of": daily["as_of"] if metric == "daily_macd_hist" else intraday["as_of"]})
    status = "not_configured" if not checks else (
        "unmet" if any(c["status"] == "unmet" for c in checks) else
        "unknown" if any(c["status"] == "unknown" for c in checks) else "met")
    return {"mode": "shadow", "status": status, "checks": checks}


def shadow_summary(evaluation):
    labels = {"met": "满足", "unmet": "未满足", "unknown": "数据不足"}
    checks = evaluation["checks"]
    if not checks:
        return "未设置额外检查，按原提醒条件监测；不代表买卖确认。"
    detail = "；".join(f"{c['label']}：{labels[c['status']]}" for c in checks)
    return (f"旁路检查（不参与本次触发）：{detail}。"
            "结果仅供参考，不影响原提醒，也不代表买卖确认。")
