"""Historical MA55 research data, deliberately disconnected from order/alert rules.

Use official timeframe bars and Tushare's pro_bar MA implementation. Replace a
whole adjusted snapshot atomically: different qfq anchors must never be merged.
Only data before today's Shanghai session is requested in this first version.
"""
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import threading
import time
import warnings

import pandas as pd
import requests
import tushare as ts

from core import config, db

TZ = dt.timezone(dt.timedelta(hours=8))
PERIODS = {"D": "日线", "15min": "15分钟", "60min": "60分钟"}
MA_PERIODS = (5, 10, 20, 55, 233)
COOLDOWN = 90
_LOCK = threading.Lock()
ERRORS = {
    "permission_denied": "接口权限不足，保留旧缓存。",
    "rate_limited": "接口频率受限，请稍后重试，旧缓存未改动。",
    "network_error": "行情连接失败，旧缓存未改动。",
    "missing_token": "未配置Tushare Token。",
    "invalid_data": "数据口径或完整性校验未通过，旧缓存未改动。",
    "empty": "接口未返回历史K线，旧缓存未改动。",
    "api_error": "行情接口返回错误，旧缓存未改动。",
}


class DataError(Exception):
    """Only fixed, non-sensitive messages may escape the transport."""


def normalize(code, period):
    raw = str(code).strip().upper()
    if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", raw):
        raw = raw[2:] + "." + raw[:2]
    if re.fullmatch(r"\d{6}", raw):
        suffix = "SH" if raw.startswith("6") else "BJ" if raw.startswith(("4", "8", "92")) else "SZ"
        raw += "." + suffix
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", raw) or period not in PERIODS:
        raise ValueError("股票代码或周期无效")
    # This module supports A-share equities only; do not apply stock factors to ETFs/indexes.
    num, exchange = raw.split(".")
    if not ((exchange == "SH" and num.startswith("6")) or
            (exchange == "SZ" and num.startswith(("00", "30"))) or
            (exchange == "BJ" and num.startswith(("4", "8", "92")))):
        raise ValueError("55线模块目前仅支持A股个股，不支持ETF或指数")
    return raw, period


def safe_query(api_name, params):
    """HTTPS only. curl stdin keeps credentials out of argv, logs and URLs."""
    if not config.TS_TOKEN:
        raise DataError("missing_token")
    body = {"api_name": api_name, "token": config.TS_TOKEN, "params": params, "fields": ""}
    try:
        if shutil.which("curl"):
            response = subprocess.run(
                ["curl", "--silent", "--fail", "--connect-timeout", "8", "--max-time", "25",
                 "--header", "Content-Type: application/json", "--data-binary", "@-",
                 "https://api.tushare.pro"],
                input=json.dumps(body), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=30,
            )
            if response.returncode:
                raise DataError("network_error")
            payload = json.loads(response.stdout)
        else:
            response = requests.post("https://api.tushare.pro", json=body,
                                     timeout=(8, 25), allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
    except DataError:
        raise
    except Exception:
        raise DataError("network_error") from None
    if payload.get("code") != 0:
        message = str(payload.get("msg", "")).lower()
        if any(s in message for s in ("频次", "每分钟", "每小时", "每天", "rate limit")):
            raise DataError("rate_limited")
        if any(s in message for s in ("权限", "积分", "permission")):
            raise DataError("permission_denied")
        raise DataError("api_error")
    try:
        data = payload["data"]
        return pd.DataFrame(data["items"], columns=data["fields"])
    except Exception:
        raise DataError("invalid_data") from None


class BarAPI:
    """Adapter for pro_bar: validated descending bars, exact daily factor coverage."""
    def __init__(self, query=safe_query):
        self.query = query
        self.dates = set()
        self.anchor = None
        self.error = None

    def _bars(self, endpoint, params):
        try:
            frame = self.query(endpoint, {k: v for k, v in params.items() if v is not None})
            if frame.empty:
                raise DataError("empty")
            key = "trade_date" if endpoint == "daily" else "trade_time"
            fmt = "%Y%m%d" if key == "trade_date" else "%Y-%m-%d %H:%M:%S"
            stamps = [dt.datetime.strptime(str(v), fmt) for v in frame[key]]
            if len(set(stamps)) != len(stamps):
                raise DataError("invalid_data")
            start = dt.datetime.strptime(params["start_date"], fmt)
            end = dt.datetime.strptime(params["end_date"], fmt)
            if any(v < start or v > end for v in stamps):
                raise DataError("invalid_data")
            if not (frame["ts_code"] == params["ts_code"]).all():
                raise DataError("invalid_data")
            for col in ("open", "high", "low", "close", "vol", "amount"):
                frame[col] = pd.to_numeric(frame[col], errors="raise")
                if not all(math.isfinite(v) and (v > 0 if col in ("open", "high", "low", "close") else v >= 0) for v in frame[col]):
                    raise DataError("invalid_data")
            if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) |
                    (frame.low > frame[["open", "close", "high"]].min(axis=1))).any():
                raise DataError("invalid_data")
            # A full limit response may have silently truncated the requested range.
            if len(frame) >= (6000 if endpoint == "daily" else 8000):
                raise DataError("invalid_data")
            self.dates = {v.strftime("%Y%m%d") for v in stamps}
            return frame.sort_values(key, ascending=False).reset_index(drop=True)
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, DataError) else "invalid_data"
            raise DataError(self.error) from None

    def daily(self, **params):
        return self._bars("daily", params)

    def stk_mins(self, **params):
        return self._bars("stk_mins", params)

    def adj_factor(self, **params):
        try:
            # SDK forwards minute timestamps; adj_factor expects YYYYMMDD dates.
            for key in ("start_date", "end_date"):
                params[key] = params[key][:10].replace("-", "")
            frame = self.query("adj_factor", params)
            if frame.empty or frame.trade_date.duplicated().any():
                raise DataError("invalid_data")
            if not self.dates.issubset(set(frame.trade_date)):
                raise DataError("invalid_data")
            frame = frame[frame.trade_date.isin(self.dates)].sort_values("trade_date", ascending=False).reset_index(drop=True)
            if not (frame.ts_code == params["ts_code"]).all():
                raise DataError("invalid_data")
            if not all(math.isfinite(float(v)) and float(v) > 0 for v in frame.adj_factor):
                raise DataError("invalid_data")
            self.anchor = str(frame.iloc[0].trade_date)
            return frame
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, DataError) else "invalid_data"
            raise DataError(self.error) from None


def fetch_snapshot(code, period, now=None, query=safe_query):
    code, period = normalize(code, period)
    now = now or dt.datetime.now(TZ)
    end = now.date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=600 if period == "D" else 240)
    api = BarAPI(query)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            frame = ts.pro_bar(
                ts_code=code, api=api, start_date=start.strftime("%Y%m%d") if period == "D" else f"{start} 09:00:00",
                end_date=end.strftime("%Y%m%d") if period == "D" else f"{end} 16:00:00",
                freq=period, adj="qfq", ma=list(MA_PERIODS), adjfactor=True, retry_count=1,
            )
        if frame is None or frame.empty:
            raise DataError("empty")
        key = "trade_date" if period == "D" else "trade_time"
        frame = frame.sort_values(key)
        bars = []
        for row in frame.to_dict("records"):
            item = {"time": str(row[key]), **{k: float(row[k]) for k in ("open", "high", "low", "close")}}
            item["volume_shares"] = float(row["vol"]) * (100 if period == "D" else 1)
            item["amount_yuan"] = float(row["amount"]) * (1000 if period == "D" else 1)
            for n in MA_PERIODS:
                value = float(row[f"ma{n}"])
                item[f"ma{n}"] = value if math.isfinite(value) else None
            bars.append(item)
        return {"schema_version": 1, "code": code, "period": period, "label": PERIODS[period], "bars": bars,
                "samples": len(bars), "as_of": bars[-1]["time"], "adjustment": "qfq",
                "adjustment_anchor": api.anchor, "source": "tushare.pro_bar",
                "fetched_at": now.isoformat(), "requested_end": end.isoformat(),
                "history_only": True, "signals_enabled": False,
                "note": "截至昨日的官方历史K线；前复权至最近返回交易日，价格与MA沿用SDK两位小数精度。分钟线保留接口原始开盘记录，未验证与交易软件的口径一致性；不产生交易信号。"}
    except DataError:
        raise
    except Exception:
        raise DataError(api.error or "invalid_data") from None


def get_snapshot(code, period, db_path=None):
    code, period = normalize(code, period)
    record = db.load_kline55_snapshot(code, period, db_path)
    payload = record.get("snapshot")
    return {"status": "cached" if payload else "missing", "snapshot": payload,
            "last_error": record.get("last_error"), "message": ERRORS.get(record.get("last_error"), ""),
            "retry_after": max(0, int(COOLDOWN - (time.time() - record.get("attempt_at", 0))))}


def refresh_snapshot(code, period, db_path=None, fetcher=fetch_snapshot):
    code, period = normalize(code, period)
    if not _LOCK.acquire(blocking=False):
        return {**get_snapshot(code, period, db_path), "status": "busy", "message": "已有K线请求进行中，请稍后再试。"}
    try:
        if not db.claim_kline55_refresh(code, period, COOLDOWN, db_path):
            return {**get_snapshot(code, period, db_path), "status": "cooldown", "message": "为避免接口限流，连续刷新至少间隔90秒。", "retry_after": COOLDOWN}
        try:
            payload = fetcher(code, period)
            db.save_kline55_snapshot(code, period, payload, None, db_path)
            return {**get_snapshot(code, period, db_path), "status": "updated"}
        except Exception as exc:
            error = str(exc) if isinstance(exc, DataError) and str(exc) in ERRORS else "invalid_data"
            db.save_kline55_snapshot(code, period, None, error, db_path)
            return {**get_snapshot(code, period, db_path), "status": "error"}
    finally:
        _LOCK.release()
