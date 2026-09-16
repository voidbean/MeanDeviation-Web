"""Deterministic historical MA55 research signals, without I/O or notifications.

All decisions use a prefix of the supplied history. The hourly gate uses only
*previous-date* daily closes, never the final daily candle of its own session.
No swing pivot, future return, current quote or LLM decision enters this engine.
"""
from bisect import bisect_left
from dataclasses import asdict, dataclass
import datetime as dt
import math

TZ = dt.timezone(dt.timedelta(hours=8))
VERSION = "ma55-history-v1"
STRATEGY_SOURCE = "ma55"
STRATEGY_LABEL = "55线规则"
HOURS = ("10:30:00", "11:30:00", "14:00:00", "15:00:00")
STATES = {
    "insufficient": "数据不足", "invalid_data": "数据校验未通过",
    "unsupported": "此周期暂不计算信号", "no_trend": "日线趋势未成立",
    "trend": "日线趋势成立", "new_trend": "新趋势形成",
    "waiting_breakout": "等待60分钟突破", "breakout": "突破后等待离线",
    "departed": "已离开均线，等待回踩", "pullback": "回踩观察，尚未转强",
    "entry": "历史回踩转强确认", "invalidated": "本轮形态失效",
    "expired": "本轮形态过期", "missing_daily": "需要日线缓存",
    "unaligned": "多周期复权基准不一致", "unverified_schedule": "分钟边界或连续性未通过",
}


@dataclass(frozen=True)
class Rules:
    slope_bars: int = 3
    daily_breakout_bars: int = 20
    setup_bars: int = 40
    departure: float = .01
    touch_above: float = .005
    max_pierce: float = .02
    reclaim_buffer: float = .002
    reclaim_bars: int = 3
    contraction: float = .8
    volume_bars: int = 20
    failed_closes: int = 2


DEFAULT_RULES = Rules()


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _time(bar, period):
    fmt = "%Y%m%d" if period == "D" else "%Y-%m-%d %H:%M:%S"
    return dt.datetime.strptime(bar["time"], fmt)


def _prepare(snapshot, period, today, closed_before=None):
    if not isinstance(snapshot, dict) or snapshot.get("period") != period:
        raise ValueError("周期或缓存结构无效")
    if snapshot.get("adjustment") != "qfq" or not snapshot.get("adjustment_anchor"):
        raise ValueError("缺少明确前复权口径")
    bars = snapshot.get("bars")
    if not isinstance(bars, list):
        raise ValueError("缺少K线数组")
    out, prev = [], None
    for raw in bars:
        stamp = _time(raw, period)
        if prev is not None and stamp <= prev:
            raise ValueError("K线时间重复或未按升序排列")
        prev = stamp
        if stamp.date() >= today and not (period == "60min" and closed_before is not None and stamp <= closed_before):
            continue  # Today's or future bars never count as historical confirmation.
        b = dict(raw)
        for field in ("open", "high", "low", "close"):
            if not positive(b.get(field)):
                raise ValueError("K线价格缺失或无效")
        if b["high"] < max(b["open"], b["close"], b["low"]) or b["low"] > min(b["open"], b["close"], b["high"]):
            raise ValueError("K线高低价关系错误")
        for n in (5, 10, 20, 55, 233):
            value = b.get(f"ma{n}")
            if value is not None and not positive(value):
                raise ValueError("均线值无效")
            # MA values alone cannot prove sufficient history.
            if len(out) < n - 1:
                b[f"ma{n}"] = None
        b["_stamp"] = stamp
        out.append(b)
    return out


def _cross(bars, i):
    return (i > 0 and positive(bars[i].get("ma55")) and positive(bars[i-1].get("ma55"))
            and bars[i]["close"] > bars[i]["ma55"] and bars[i-1]["close"] <= bars[i-1]["ma55"])


def _trend(bars, i, rules):
    if i < max(54, rules.slope_bars):
        return None, {}, "日线样本不足，需MA55及均线斜率历史"
    b, old = bars[i], bars[i-rules.slope_bars]
    if not all(positive(x.get(f"ma{n}")) for x in (b, old) for n in (5, 10, 20)) or not positive(b.get("ma55")):
        return None, {}, "日线均线缺失"
    slopes = {f"ma{n}": round((b[f"ma{n}"] / old[f"ma{n}"] - 1) * 100, 6) for n in (5, 10, 20)}
    if not all(math.isfinite(v) for v in slopes.values()):
        raise ValueError("均线变化率溢出")
    checks = {
        "bullish_order": b["ma5"] > b["ma10"] > b["ma20"],
        "rising": all(b[f"ma{n}"] > old[f"ma{n}"] for n in (5, 10, 20)),
        "above_ma20": b["close"] > b["ma20"],
        "above_ma55": b["close"] > b["ma55"],
    }
    values = {"daily_time": b["time"], "close": b["close"], "ma55": b["ma55"],
              "slope_pct": slopes, "checks": checks}
    labels = {"bullish_order": "MA5>MA10>MA20", "rising": "三条均线斜率向上",
              "above_ma20": "收盘高于MA20", "above_ma55": "收盘高于MA55"}
    reason = ("日线排列、斜率和价格位置均满足" if all(checks.values()) else
              "日线未通过：" + "、".join(labels[k] for k, passed in checks.items() if not passed))
    return all(checks.values()), values, reason


def _event(code, kind, period, bar, setup_time, evidence, levels):
    label = "新趋势" if kind == "NEW_TREND" else "回踩转强"
    period_label = "日线" if period == "D" else "60分钟"
    return {"id": f"{VERSION}:{code}:{period}:{kind}:{bar['time']}:{setup_time}",
            "strategy_source": STRATEGY_SOURCE, "strategy_label": STRATEGY_LABEL,
            "rule_version": VERSION, "title": f"【55线】{period_label} · {label}",
            "type": kind, "period": period, "time": bar["time"],
            "label": label,
            "setup_time": setup_time, "close": bar["close"],
            "evidence": evidence, "levels": levels, "historical_only": True}


def _base(period, rules):
    return {"version": VERSION, "period": period, "rules": asdict(rules),
            "strategy_source": STRATEGY_SOURCE, "strategy_label": STRATEGY_LABEL,
            "historical_only": True, "notifications_enabled": False,
            "status": "insufficient", "state": "insufficient", "label": STATES["insufficient"],
            "reason": "暂无可分析的历史K线", "as_of": None, "signal": None,
            "events": [], "event_count": 0, "evidence": {}, "levels": {}, "warnings": [
                "规则阈值未经收益回测；形态匹配不代表胜率或买卖指令。",
                "基于当前前复权历史重放，不等同于含成交成本的逐时点收益回测。",
                "未用交易日历/停牌表证明整段历史无缺失；本版仅供历史研究。",
            ]}


def _finish(result, state, reason, evidence=None, levels=None):
    result.update(state=state, label=STATES[state], reason=reason,
                  evidence=evidence or {}, levels=levels or {})
    if result["as_of"]:
        result["signal"] = next((e["type"] for e in reversed(result["events"]) if e["time"] == result["as_of"]), None)
    result["event_count"] = len(result["events"])
    result["events"] = result["events"][-100:]
    return result


def _daily_states(bars, rules, code):
    contexts, events, last_break = [], [], None
    for i, b in enumerate(bars):
        trend, evidence, reason = _trend(bars, i, rules)
        if trend and _cross(bars, i):
            last_break = i
            events.append(_event(code, "NEW_TREND", "D", b, b["time"], evidence,
                                 {"ma55": b["ma55"], "invalidation": "后续日线收盘失守MA55或趋势条件不再满足时，本轮背景失效。"}))
        if trend is not True:
            last_break = None  # A broken trend cannot reuse an old breakout.
        recent = last_break is not None and i - last_break <= rules.daily_breakout_bars
        contexts.append({"trend": trend, "evidence": evidence, "reason": reason,
                         "recent_breakout": recent,
                         "breakout_time": bars[last_break]["time"] if last_break is not None else None})
    return contexts, events


def _continuous(previous, bar, daily_dates):
    a, b = previous["_stamp"], bar["_stamp"]
    ta, tb = a.strftime("%H:%M:%S"), b.strftime("%H:%M:%S")
    if ta not in HOURS or tb not in HOURS:
        return False
    if a.date() == b.date():
        return HOURS.index(tb) == HOURS.index(ta) + 1
    # Daily bars in between prove that an entire hourly session is missing.
    return (ta == HOURS[-1] and tb == HOURS[0] and not any(a.date() < d < b.date() for d in daily_dates))


def _volume_ratio(bars, i, rules):
    if i < rules.volume_bars:
        return None
    samples = [b.get("volume_shares") for b in bars[i-rules.volume_bars:i]]
    current = bars[i].get("volume_shares")
    if not positive(current) or not all(positive(v) for v in samples):
        return None
    baseline = sum(samples) / len(samples)
    if not positive(baseline):
        return None
    ratio = current / baseline
    return ratio if math.isfinite(ratio) else None


def _levels(bars, i, setup, continuous_count):
    b = bars[i]
    # Only same-timeframe prices: do not compare independently anchored series.
    candidates = []
    if continuous_count >= 233 and positive(b.get("ma233")) and b["ma233"] > b["close"]:
        candidates.append((b["ma233"], "60分钟MA233"))
    if i:
        high = max(x["high"] for x in bars[max(0, i-20):i])
        if high > b["close"]:
            candidates.append((high, "此前20根60分钟K线最高价（非未来确认拐点）"))
    target = min(candidates, default=(None, None))
    return {"ma55": b.get("ma55"), "pullback_low": setup.get("low"),
            "reference_pressure": target[0], "pressure_source": target[1],
            "invalidation": "跌穿容许深度、连续两根收盘低于MA55、日线背景失效或数据中断时，本轮形态失效；不是自动止损单。"}


def analyze(snapshot, daily_snapshot=None, now=None, rules=DEFAULT_RULES, *, closed_before=None):
    """Pure prefix replay. Only D NEW_TREND and D→60min PULLBACK_ENTRY in v1."""
    period = snapshot.get("period") if isinstance(snapshot, dict) else None
    result = _base(period, rules)
    if period == "15min":
        return _finish(result, "unsupported", "第一版只计算日线新趋势与日线→60分钟回踩，15分钟仍仅展示图表。")
    if period not in ("D", "60min"):
        return _finish(result, "insufficient", "请先获取此周期历史K线缓存。")
    now = now or dt.datetime.now(TZ)
    today = now.astimezone(TZ).date() if now.tzinfo else now.date()
    try:
        if closed_before is not None:
            local_now = now.astimezone(TZ).replace(tzinfo=None) if now.tzinfo else now
            if closed_before.tzinfo:
                closed_before = closed_before.astimezone(TZ).replace(tzinfo=None)
            closed_before = min(closed_before, local_now)
        bars = _prepare(snapshot, period, today, closed_before)
        if not bars:
            return result
        result["as_of"] = bars[-1]["time"]
        result["age_calendar_days"] = (today - bars[-1]["_stamp"].date()).days
        if result["age_calendar_days"] > 7:
            result["warnings"].append("末根K线距今超过7个自然日，仅代表旧历史状态；节假日/停牌需另行核对。")
        code = snapshot.get("code", "")
        if period == "D":
            contexts, result["events"] = _daily_states(bars, rules, code)
            latest = contexts[-1]
            state = "trend" if latest["trend"] else "no_trend" if latest["trend"] is False else "insufficient"
            if result["events"] and result["events"][-1]["time"] == bars[-1]["time"]:
                state = "new_trend"
            result["status"] = "evaluated" if latest["trend"] is not None else "insufficient"
            return _finish(result, state, latest["reason"], latest["evidence"],
                           {"ma55": bars[-1].get("ma55"), "invalidation": "日线收盘失守MA55或均线趋势条件不再满足。"})
        if not daily_snapshot:
            return _finish(result, "missing_daily", "请先切换到日线并获取缓存；60分钟形态需要当时的日线趋势背景。")
        if daily_snapshot.get("code") != code or daily_snapshot.get("adjustment_anchor") != snapshot.get("adjustment_anchor"):
            return _finish(result, "unaligned", "日线与60分钟的股票或前复权基准不同，请更新到一致基准后再计算。")
        daily = _prepare(daily_snapshot, "D", today)
        if not daily:
            return _finish(result, "missing_daily", "没有可用的已收盘日线。")
        contexts, _ = _daily_states(daily, rules, code)
        daily_dates = [b["_stamp"].date() for b in daily]
        result["warnings"].append("60分钟须为10:30/11:30/14:00/15:00结束的K线；额外09:30记录不擅自删除或重算MA，存在时停止确认。")
        result["warnings"].append("缩量按回踩K线/此前20根60分钟均量判断，尚未校正日内量能季节性；恐慌放量假跌破暂不纳入。")
        setup = None
        state, reason, evidence, levels = "insufficient", "至少需要55根连续标准60分钟K线", {}, {}
        count = 0
        for i, b in enumerate(bars):
            evidence, levels = {}, {}
            stamp = b["_stamp"]
            standard = stamp.strftime("%H:%M:%S") in HOURS
            linked = i > 0 and _continuous(bars[i-1], b, daily_dates)
            count = count + 1 if standard and linked else 1 if standard else 0
            if not linked:
                setup = None
            if count < 55 or not positive(b.get("ma55")):
                state, reason = "unverified_schedule", "连续标准60分钟样本不足55根，可能存在缺K线或额外开盘记录。"
                setup = None
                continue
            # Strictly before this session: never use the same-date daily close.
            di = bisect_left(daily_dates, stamp.date()) - 1
            if di < 0 or (stamp.date() - daily_dates[di]).days > 7:
                state, reason = "missing_daily", "当前60分钟K线之前缺少足够新的已收盘日线背景。"
                setup = None
                continue
            context = contexts[di]
            evidence = {**context["evidence"], "daily_breakout_time": context["breakout_time"],
                        "hourly_close": b["close"], "hourly_ma55": b["ma55"], "continuous_bars": count}
            if context["trend"] is None:
                state, reason = "insufficient", context["reason"]
                setup = None
                continue
            if context["trend"] is not True or not context["recent_breakout"]:
                state = "invalidated" if setup else "no_trend"
                reason = "日线趋势未成立，或最近20根已收盘日线内无仍有效的新趋势突破。"
                setup = None
                continue
            # A reclaim may itself cross MA55: don't erase an active pullback.
            if _cross(bars, i) and (setup is None or setup["consumed"]):
                setup = {"start": i, "depart": None, "touch": None, "below": 0,
                         "low": None, "volume_ratio": None, "consumed": False}
                state, reason = "breakout", "60分钟上穿MA55，等待后续收盘至少离开均线1%。"
                continue  # Breakout, departure and touch cannot be the same bar.
            if setup is None:
                if state not in ("expired", "invalidated"):
                    state, reason = "waiting_breakout", "日线背景满足，等待新的60分钟上穿MA55。"
                continue
            if i - setup["start"] > rules.setup_bars and not setup["consumed"]:
                setup = None
                state, reason = "expired", "突破后超过40根60分钟K线仍未完成确认，等待新突破。"
                continue
            evidence.update(breakout_time=bars[setup["start"]]["time"])
            setup["below"] = setup["below"] + 1 if b["close"] < b["ma55"] else 0
            if b["low"] < b["ma55"] * (1 - rules.max_pierce) or setup["below"] >= rules.failed_closes:
                setup = None
                state, reason = "invalidated", "低点跌穿MA55容许深度2%，或连续两根收盘失守MA55。"
                continue
            if setup["consumed"]:
                state, reason = "entry", "本轮历史形态已确认，不重复产生回踩信号；仍需观察后续失效条件。"
                levels = _levels(bars, i, setup, count)
                continue
            if setup["depart"] is None:
                if b["close"] >= b["ma55"] * (1 + rules.departure):
                    setup["depart"] = i
                    state, reason = "departed", "已离开MA55至少1%，等待之后的回踩。"
                continue
            if setup["touch"] is None:
                near = (b["ma55"] * (1-rules.max_pierce) <= b["low"] <= b["ma55"] * (1+rules.touch_above)
                        and b["high"] >= b["ma55"] * (1-rules.touch_above))
                if near:
                    setup["touch"] = i
                    setup["low"] = b["low"]
                    setup["volume_ratio"] = _volume_ratio(bars, i, rules)
                    state, reason = "pullback", "进入MA55回踩区，检查缩量及收回转强。"
            if setup["touch"] is not None:
                setup["low"] = min(setup["low"], b["low"])
                evidence.update(touch_time=bars[setup["touch"]]["time"], volume_ratio=setup["volume_ratio"])
                levels = _levels(bars, i, setup, count)
                if i - setup["touch"] > rules.reclaim_bars:
                    setup = None
                    state, reason = "expired", "首次回踩后超过3根K线仍未确认，等待新突破。"
                    continue
                ratio = setup["volume_ratio"]
                reversal = (b["close"] > b["ma55"] * (1+rules.reclaim_buffer)
                            and b["close"] > b["open"] and b["close"] > bars[i-1]["close"])
                if ratio is not None and ratio <= rules.contraction and reversal:
                    setup["consumed"] = True
                    state, reason = "entry", "缩量回踩后收盘收复MA55上方0.2%，且收阳并高于前一根收盘；历史形态确认。"
                    result["events"].append(_event(code, "PULLBACK_ENTRY", "60min", b,
                                                   bars[setup["start"]]["time"], dict(evidence), dict(levels)))
                elif ratio is None:
                    reason = "回踩量能数据不足，不能确认；不能把缺失或零成交量当作缩量。"
                elif ratio > rules.contraction:
                    reason = "首次回踩未达到缩量要求，本版不将放量假跌破归入确认信号。"
                else:
                    reason = "缩量回踩已出现，等待收回MA55并收阳、高于前一根收盘。"
        result["status"] = "evaluated" if state not in ("unverified_schedule", "missing_daily", "insufficient") else "insufficient"
        return _finish(result, state, reason, evidence, levels)
    except (KeyError, TypeError, ValueError, OverflowError):
        # Do not expose input strings; malformed cache must not invent a signal.
        result["events"] = []
        result["status"] = "invalid_data"
        return _finish(result, "invalid_data", "历史K线、均线或时间字段校验失败；请重新获取数据。")
