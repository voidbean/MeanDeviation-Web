"""Explicit ETF collector. Never imported/called by an MCP data read."""
import copy
import datetime as dt
import time

from core import db
from services.etf_kline import TZ, PERIODS, grid, iso, local_now, normalize, parse_bars, parse_calendar, parse_factors


class OfficialQuery:
    """Pace repeated endpoint calls, including 15/60min. No credential in URLs/argv/logs."""
    def __init__(self):
        self.last = {}
        self.denied = set()

    def __call__(self, endpoint, params):
        from services.kline55 import safe_query, DataError
        if endpoint in self.denied:
            raise DataError("permission_denied")
        delay = 65 - (time.monotonic() - self.last.get(endpoint, -1e10))
        if delay > 0:
            time.sleep(delay)
        self.last[endpoint] = time.monotonic()
        try:
            return safe_query(endpoint, params)
        except DataError as exc:
            if str(exc) == "permission_denied":
                self.denied.add(endpoint)  # no alternate-frequency retry of a denied endpoint
            raise


def collect_etf(code, db_path=None, now=None, query=None):
    code = normalize(code)
    clock = (lambda: local_now(now)) if now is not None else (lambda: dt.datetime.now(TZ))
    started = clock()
    status, lease, previous = db.claim_etf_kline_refresh(code, started.timestamp(), db_path)
    if status != "claimed":
        return {"code": code, "status": status, "network_called": False}
    query = query or OfficialQuery()
    document = copy.deepcopy(previous) if previous else dict(schema_version=1, code=code, asset_type="ETF", periods={})
    start = started.date() - dt.timedelta(days=600)
    minute_start = started.date() - dt.timedelta(days=120)
    history_end = started.date() if started.time() >= dt.time(15, 2) else started.date() - dt.timedelta(days=1)
    calendar_end = started.date() + dt.timedelta(days=45)
    exchange = "SSE" if code.endswith(".SH") else "SZSE"
    errors = []

    def request(endpoint, params, convert):
        try:
            frame = query(endpoint, params)
            fetched = clock()
            return convert(frame, fetched), iso(fetched), None
        except Exception as exc:
            # Error strings are explicitly enumerated: never persist/return arbitrary upstream exceptions.
            safe = {"permission_denied", "rate_limited", "missing_token", "network_error", "empty", "api_error",
                    "invalid_data", "possibly_truncated", "code_mismatch", "period_mismatch", "duplicate_or_out_of_range",
                    "not_today", "invalid_ohlc", "invalid_volume", "calendar_missing", "calendar_invalid",
                    "calendar_incomplete", "factor_missing", "factor_invalid"}
            error = str(exc) if str(exc) in safe else "invalid_data"
            errors.append(endpoint + ":" + error)
            return None, iso(clock()), endpoint + ":" + error

    try:
        calendar, fetched, error = request("trade_cal", dict(exchange=exchange, start_date=start.strftime("%Y%m%d"),
            end_date=calendar_end.strftime("%Y%m%d")), lambda frame, _: parse_calendar(frame, start, calendar_end, exchange))
        if calendar is not None:
            document["calendar"] = dict(values=calendar, fetched_at=fetched, error=None, exchange=exchange)
        else:
            document.setdefault("calendar", {})["error"] = error
        factors, fetched, error = request("fund_adj", dict(ts_code=code, start_date=start.strftime("%Y%m%d"),
            end_date=started.strftime("%Y%m%d")), lambda frame, _: parse_factors(frame, code))
        if factors is not None:
            document["factors"] = dict(values=factors, fetched_at=fetched, error=None)
        else:
            document.setdefault("factors", {})["error"] = error
        known = document.get("factors", {}).get("values", {})
        # Latest PROVEN factor is the shared anchor, never invent today's factor from yesterday's value.
        document["adjustment_anchor"] = max((day for day in known if day <= started.date().isoformat()), default=None)

        for period in PERIODS:
            old = document["periods"].get(period, {})
            old_bars = old.get("bars", [])
            period_errors = []
            endpoint = "fund_daily" if period == "D" else "etf_mins"
            beginning = start if period == "D" else minute_start
            params = dict(ts_code=code, start_date=beginning.strftime("%Y%m%d") if period == "D" else f"{beginning} 09:00:00",
                          end_date=history_end.strftime("%Y%m%d") if period == "D" else f"{history_end} 16:00:00")
            if period != "D":
                params["freq"] = period
            historical, fetched, error = request(endpoint, params, lambda frame, seen: parse_bars(
                frame, code, period, endpoint, seen, old_bars, beginning, history_end))
            if error:
                period_errors.append(error)
                historical = [b for b in old_bars if beginning.isoformat() <= b["trade_date"] <= history_end.isoformat()]
            current = []
            today_open = document.get("calendar", {}).get("values", {}).get(started.date().isoformat()) == 1
            history_covers_today = {iso(at) for at in grid(started.date(), period)}.issubset(
                {b["end_at"] for b in historical if b["valid_schedule"]})
            # A complete post-close historical day doesn't additionally require realtime permissions.
            if period != "D" and today_open and started.time() >= dt.time(9, 30) and not history_covers_today:
                current, current_fetched, error = request("rt_etf_min_daily", dict(ts_code=code, freq=period.upper()),
                    lambda frame, seen: parse_bars(frame, code, period, "rt_etf_min_daily", seen,
                                                   old_bars, started.date(), started.date(), current=True))
                if error:
                    period_errors.append(error)
                    current = [b for b in old_bars if b["trade_date"] == started.date().isoformat()]
                else:
                    fetched = current_fetched
            # An authoritative accumulated-day response replaces that day, not an append masking missing/revised bars.
            combined = {bar["end_at"]: bar for bar in current}
            for bar in historical:
                combined[bar["end_at"]] = bar  # published historical source takes precedence after close
            document["periods"][period] = dict(bars=sorted(combined.values(), key=lambda b: b["end_at"]),
                                               fetched_at=max((b["fetched_at"] for b in combined.values()), default=old.get("fetched_at")),
                                               errors=period_errors, last_attempt_at=iso(clock()))
        document["collected_at"] = iso(clock())
        published = db.publish_etf_kline(code, lease, document, db_path=db_path, completed_at=clock().timestamp())
        return dict(code=code, status="superseded" if not published else "partial" if errors else "collected", published=published,
                    collected_at=document["collected_at"], adjustment_anchor=document["adjustment_anchor"],
                    errors=sorted(set(errors)), samples={p: len(document["periods"][p]["bars"]) for p in PERIODS})
    except Exception:
        db.publish_etf_kline(code, lease, None, "collector_failed", db_path, completed_at=clock().timestamp())
        return dict(code=code, status="error", errors=["collector_failed"], published=False)
