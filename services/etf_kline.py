"""Pure ETF candle validation/MA/reading. No network, migrations, monitoring or writes."""
import base64
import datetime as dt
import hashlib
import json
import math
import re

TZ = dt.timezone(dt.timedelta(hours=8))
PERIODS = ("D", "60min", "15min")
FIELDS = ("open", "high", "low", "close", "volume_shares", "amount_yuan")


def normalize(code, period="D"):
    raw = str(code).strip().upper()
    if re.fullmatch(r"(?:SH|SZ)\d{6}", raw):
        raw = raw[2:] + "." + raw[:2]
    if re.fullmatch(r"\d{6}", raw):
        raw += ".SH" if raw.startswith("5") else ".SZ"
    if not re.fullmatch(r"(?:(?:51|52|56|58)\d{4}\.SH|15\d{4}\.SZ)", raw) or period not in PERIODS:
        raise ValueError("此K线工具仅支持沪深ETF及D/60min/15min周期")
    return raw


def local_now(now=None):
    now = now or dt.datetime.now(TZ)
    if now.tzinfo is None:
        raise ValueError("时间必须带时区")
    return now.astimezone(TZ)


def iso(value):
    return value.isoformat(timespec="seconds")


def stamp(value):
    return dt.datetime.fromisoformat(value).astimezone(TZ)


def grid(day, period):
    """Exchange interval END labels. Unknown extra auction records are never silently removed."""
    if period == "D":
        return [dt.datetime.combine(day, dt.time(15), TZ)]
    step = 60 if period == "60min" else 15
    return [dt.datetime.combine(day, start, TZ) + dt.timedelta(minutes=i)
            for start in (dt.time(9, 30), dt.time(13)) for i in range(step, 121, step)]


def interval_start(end, period):
    if period == "D":
        return end.replace(hour=9, minute=30)
    return end - dt.timedelta(minutes=60 if period == "60min" else 15)


def parse_bars(frame, code, period, source, fetched_at, old=(), start=None, end=None, current=False):
    if frame is None or frame.empty:
        raise ValueError("empty")
    if len(frame) >= (5000 if period == "D" else 1000 if current else 8000):
        raise ValueError("possibly_truncated")
    previous = {b["end_at"]: b for b in old}
    output, seen = [], set()
    for row in frame.to_dict("records"):
        if str(row.get("ts_code", row.get("code"))) != code:
            raise ValueError("code_mismatch")
        if period == "D":
            day = dt.datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
            at = grid(day, period)[0]
        else:
            at = dt.datetime.strptime(str(row.get("trade_time", row.get("time"))), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
            day = at.date()
            if row.get("freq") is not None and str(row["freq"]).lower() != period.lower():
                raise ValueError("period_mismatch")
        if at in seen or (start and day < start) or (end and day > end):
            raise ValueError("duplicate_or_out_of_range")
        if current and day != fetched_at.date():
            raise ValueError("not_today")
        seen.add(at)
        bar = {"start_at": iso(interval_start(at, period)), "end_at": iso(at), "trade_date": day.isoformat(),
               "source": source, "fetched_at": iso(fetched_at), "historical": not current,
               "valid_schedule": at in grid(day, period)}
        for key in ("open", "high", "low", "close"):
            value = float(row[key])
            if not math.isfinite(value) or value <= 0:
                raise ValueError("invalid_ohlc")
            bar[key] = value
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            raise ValueError("invalid_ohlc")
        for key, dest, scale in (("vol", "volume_shares", 100 if period == "D" else 1),
                                 ("amount", "amount_yuan", 1000 if period == "D" else 1)):
            value = float(row[key]) * scale
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid_volume")
            bar[dest] = value
        prior = previous.get(bar["end_at"], {})
        unchanged = all(prior.get(k) == bar[k] for k in FIELDS)
        bar["observations"] = prior.get("observations", 0) + 1 if unchanged else 1
        bar["stable_since"] = prior["stable_since"] if unchanged else iso(fetched_at)
        bar["revised"] = bool(prior) and not unchanged
        bar["revision_count"] = prior.get("revision_count", 0) + int(bar["revised"])
        output.append(bar)
    return sorted(output, key=lambda b: b["end_at"])


def parse_calendar(frame, start, end, exchange="SSE"):
    values = {}
    if frame is None or frame.empty:
        raise ValueError("calendar_missing")
    for row in frame.to_dict("records"):
        day = dt.datetime.strptime(str(row["cal_date"]), "%Y%m%d").date().isoformat()
        if day in values or str(row["is_open"]) not in ("0", "1") or row.get("exchange", exchange) != exchange:
            raise ValueError("calendar_invalid")
        values[day] = int(row["is_open"])
    days = [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if not set(days).issubset(values):
        raise ValueError("calendar_incomplete")
    return {day: values[day] for day in days}


def parse_factors(frame, code):
    values = {}
    if frame is None or frame.empty:
        raise ValueError("factor_missing")
    if len(frame) >= 2000:
        raise ValueError("possibly_truncated")
    for row in frame.to_dict("records"):
        day = dt.datetime.strptime(str(row["trade_date"]), "%Y%m%d").date().isoformat()
        value = float(row["adj_factor"])
        if row["ts_code"] != code or day in values or not math.isfinite(value) or value <= 0:
            raise ValueError("factor_invalid")
        values[day] = value
    return values


def prepare_series(document, period, now):
    section = document.get("periods", {}).get(period, {})
    raw = section.get("bars", [])
    calendar = document.get("calendar", {}).get("values", {})
    factors = document.get("factors", {}).get("values", {})
    anchor = document.get("adjustment_anchor")
    cutoff = now - dt.timedelta(seconds=90)
    expected = [at for day, opened in sorted(calendar.items()) if opened
                for at in grid(dt.date.fromisoformat(day), period) if at <= cutoff]
    expected_latest = expected[-1] if expected else None
    indices = {iso(at): i for i, at in enumerate(expected)}
    problems = list(section.get("errors", []))
    for key in ("calendar", "factors"):
        if document.get(key, {}).get("error"):
            problems.append(document[key]["error"])
    if now.date().isoformat() not in calendar:
        problems.append("calendar_missing_current_date")
    extras = [b["end_at"] for b in raw if not b["valid_schedule"] or calendar.get(b["trade_date"]) != 1]
    if extras:
        problems.append("unverified_schedule")
    bars, unconfirmed, closes = [], [], []
    previous_index = None
    for raw_bar in raw:
        bar = dict(raw_bar)
        raw_ohlc = {key: bar[key] for key in ("open", "high", "low", "close")}
        bar.update(raw_ohlc=raw_ohlc, adjustment_factor=factors.get(bar["trade_date"]),
                   anchor_factor=factors.get(anchor), ma55=None, ma233=None)
        ratio = (factors[bar["trade_date"]] / factors[anchor]
                 if anchor in factors and bar["trade_date"] in factors else None)
        factor_problem = "factor_missing" if ratio is None else None
        if ratio is not None and (not math.isfinite(ratio) or ratio <= 0
                or any(not math.isfinite(value * ratio) or value * ratio <= 0 for value in raw_ohlc.values())):
            ratio, factor_problem = None, "factor_invalid"
        for key in raw_ohlc:
            bar[key] = raw_ohlc[key] * ratio if ratio is not None else None
        at, seen_at = stamp(bar["end_at"]), stamp(bar["fetched_at"])
        closed_at = min(now, seen_at) - dt.timedelta(seconds=90)
        stable = (bar["historical"] or (bar["observations"] >= 2
                   and (seen_at - stamp(bar["stable_since"])).total_seconds() >= 30))
        if not bar["valid_schedule"] or calendar.get(bar["trade_date"]) != 1:
            reason = "unverified_schedule"
        elif factor_problem:
            reason = factor_problem
        elif at > closed_at:
            reason = "forming" if at > now else "awaiting_close_verification"
        elif not stable:
            reason = "awaiting_stability"
        else:
            reason = None
        bar.update(is_closed=reason is None, closure_state=reason or "confirmed")
        index = indices.get(bar["end_at"])
        if reason:
            unconfirmed.append(bar)
            closes, previous_index = [], None
            continue
        if previous_index is None or index is None or index != previous_index + 1:
            closes = []
        closes.append(bar["close"])
        # Extra records make the source's interval meaning unverified. Never silently drop them to invent an MA.
        if not extras:
            for n in (55, 233):
                if len(closes) >= n:
                    try:
                        bar[f"ma{n}"] = math.fsum(closes[-n:]) / n
                    except OverflowError:
                        # Positive finite inputs can overflow their sum, though their mean is finite.
                        bar[f"ma{n}"] = math.fsum(value / n for value in closes[-n:])
        previous_index = index
        bars.append(bar)
    first = stamp(raw[0]["end_at"]) if raw else None
    present = {b["end_at"] for b in bars}
    gaps = [iso(at) for at in expected if first and at >= first and iso(at) not in present]
    if gaps:
        problems.append("gap_or_unconfirmed_or_suspension")
    problems.extend(b["closure_state"] for b in unconfirmed if b["closure_state"] in ("factor_missing", "factor_invalid"))
    if not bars:
        problems.append("no_confirmed_bars")
    elif bars[-1]["ma55"] is None:
        problems.append("insufficient_ma55_history")
    age_unknown = not calendar or now.date().isoformat() not in calendar
    stale = (age_unknown or not bars or expected_latest is None or stamp(bars[-1]["end_at"]) < expected_latest
             or bool(section.get("errors")) or any(document.get(key, {}).get("error") for key in ("calendar", "factors")))
    latest = bars[-1] if bars else None
    return dict(bars=bars, unconfirmed_bars=unconfirmed, missing_reasons=sorted(set(problems)),
                gap_count=len(gaps), gap_examples=gaps[-10:], unverified_examples=extras[-10:],
                expected_as_of=iso(expected_latest) if expected_latest else None,
                as_of=latest["end_at"] if latest else None, stale=stale,
                analysis_ready=bool(latest and latest["ma55"] is not None and not stale and not problems))


def bound(value, end=False):
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.datetime.combine(dt.date.fromisoformat(value), dt.time.max if end else dt.time.min, TZ)
    return local_now(dt.datetime.fromisoformat(value))


def read_kline_data(conn, code, period="D", start_at=None, end_at=None, limit=300,
                    cursor=None, snapshot_id=None, closed_only=True, now=None):
    code = normalize(code, period)
    now = local_now(now)
    if not 1 <= limit <= 500:
        raise ValueError("limit范围为1–500")
    start, end = bound(start_at), bound(end_at, True)
    if start and end and start > end:
        raise ValueError("start_at不能晚于end_at")
    base = dict(schema_version=1, code=code, asset_type="ETF", period=period, timezone="Asia/Shanghai",
                generated_at=iso(now), cache_only=True, adjustment="qfq", bars=[], warmup_bars=[],
                unconfirmed_bars=[], forming_bar=None, next_cursor=None, analysis_ready=False, stale=True,
                requested_range={"start_at": start_at, "end_at": end_at}, actual_range=None,
                as_of=None, source_as_of=None, source_analysis_ready=False, source_stale=True,
                units={"price": "CNY", "volume_shares": "shares", "amount_yuan": "CNY"})
    if conn is None:
        return {**base, "status": "missing", "missing_reasons": ["database_missing"]}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='etf_kline_cache'").fetchone():
        return {**base, "status": "missing", "missing_reasons": ["cache_uninitialized"]}
    record = conn.execute("SELECT payload_json,last_error,attempt_at FROM etf_kline_cache WHERE code=?", (code,)).fetchone()
    if not record or not record[0]:
        return {**base, "status": "missing", "missing_reasons": [record[1] if record and record[1] else "cache_empty"]}
    digest = hashlib.sha256(record[0].encode()).hexdigest()
    document = json.loads(record[0])
    request = dict(code=code, period=period, start_at=start_at, end_at=end_at, limit=limit, closed_only=closed_only)
    offset = None
    if cursor:
        if len(cursor) > 2048:
            raise ValueError("分页游标无效")
        try:
            page = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (page["request"] != request or type(page["offset"]) is not int or page["offset"] < 0
                    or (snapshot_id is not None and snapshot_id != page["snapshot_id"])):
                raise ValueError()
            snapshot_id, offset = page["snapshot_id"], page["offset"]
        except Exception:
            raise ValueError("分页游标无效或请求参数已改变") from None
    if snapshot_id and snapshot_id != digest:
        return {**base, "snapshot_id": digest, "status": "snapshot_changed", "missing_reasons": ["snapshot_changed"]}
    result = prepare_series(document, period, now)
    section = document.get("periods", {}).get(period, {})
    all_bars = result.pop("bars")
    selected = [b for b in all_bars if (not start or stamp(b["end_at"]) >= start) and (not end or stamp(b["end_at"]) <= end)]
    # Default page starts with the newest N bars; pagination walks backwards without gaps/duplicates.
    stop = len(selected) if offset is None else offset
    if stop > len(selected):
        raise ValueError("分页游标超出范围")
    page_start = max(0, stop - limit)
    page_bars = selected[page_start:stop]
    warmup = []
    if page_bars:
        index = next(i for i, bar in enumerate(all_bars) if bar["end_at"] == page_bars[0]["end_at"])
        warmup = all_bars[max(0, index - 232):index]
        # MA on warm-up rows isn't necessarily reproducible from this response; expose their OHLCV only.
        warmup = [{**bar, "ma55": None, "ma233": None} for bar in warmup]
    unconfirmed = result.pop("unconfirmed_bars")
    scoped = [b for b in unconfirmed if (not start or stamp(b["end_at"]) >= start) and (not end or stamp(b["end_at"]) <= end)]
    if record[1]:
        result["missing_reasons"].append(record[1])
        result.update(stale=True, analysis_ready=False)
    result["source_as_of"] = result["as_of"]
    result["source_analysis_ready"] = result["analysis_ready"]
    result["source_stale"] = result["stale"]
    result["as_of"] = page_bars[-1]["end_at"] if page_bars else None
    if result["as_of"] != result["source_as_of"] or not page_bars:
        result.update(stale=True, analysis_ready=False)
        result["missing_reasons"].append("historical_or_empty_range")
    if page_start:
        base["next_cursor"] = base64.urlsafe_b64encode(json.dumps(
            dict(request=request, snapshot_id=digest, offset=page_start), separators=(",", ":")).encode()).decode()
    status = "ready" if result["analysis_ready"] else "partial"
    if not page_bars and all_bars:
        status = "empty_range"
    elif not all_bars and not unconfirmed and not section.get("errors"):
        status = "missing"
    return {**base, **result, "status": status,
            "snapshot_id": digest, "adjustment_anchor": document.get("adjustment_anchor"),
            "factor_source": "fund_adj", "factors_fetched_at": document.get("factors", {}).get("fetched_at"),
            "calendar_source": "trade_cal", "calendar_fetched_at": document.get("calendar", {}).get("fetched_at"),
            "calendar_exchange": document.get("calendar", {}).get("exchange"),
            "source": sorted({bar["source"] for bar in all_bars + unconfirmed}),
            "fetched_at": section.get("fetched_at"), "collected_at": document.get("collected_at"),
            "last_attempt_at": iso(dt.datetime.fromtimestamp(record[2], TZ)),
            "bars": page_bars, "warmup_bars": warmup, "samples": len(all_bars),
            "actual_range": {"start_at": page_bars[0]["end_at"], "end_at": page_bars[-1]["end_at"]} if page_bars else None,
            "returned_samples": len(page_bars), "page_ma55_ready": bool(page_bars and page_bars[-1]["ma55"] is not None),
            "ma233_ready": bool(page_bars and page_bars[-1]["ma233"] is not None),
            "source_ma233_ready": bool(all_bars and all_bars[-1]["ma233"] is not None),
            "unconfirmed_bars": scoped[-100:] if not closed_only else [],
            "unconfirmed_count": len(scoped), "latest_unconfirmed_bar": scoped[-1] if scoped else None,
            "forming_bar": next((b for b in reversed(scoped) if b["trade_date"] == now.date().isoformat()
                                  and b["closure_state"] == "forming"), None),
            "timestamp_semantics": "exchange_end_grid_v1", "ma_method": "SMA of confirmed contiguous qfq close; no pre-rounding",
            "note": "仅数据供给，不产生买卖或下单指令；forming/unconfirmed不参与均线或确认。"}
