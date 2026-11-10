"""Cache-only MCP reads. No upstream requests, AI calls, migrations or business writes."""
import datetime as dt
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values

from core import config
from core.strategy import calculate_strategy
from services.portfolio import build_portfolio_overview

TZ = dt.timezone(dt.timedelta(hours=8))


def normalize_code(code: str) -> tuple[str, str]:
    raw = code.strip().upper()
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", raw):
        raw = raw[2:] + "." + raw[:2]
    if re.fullmatch(r"\d{6}", raw):
        market = "SH" if raw.startswith(("5", "6")) else "BJ" if raw.startswith(("4", "8", "92")) else "SZ"
        raw += "." + market
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        raise ValueError("股票代码须为六位数字、sh600519 或 600519.SH 等格式")
    return raw[:6], raw


class SnapshotReader:
    def __init__(self, db_path=None, env_path=None):
        self.db_path = Path(db_path or config.DB_PATH).resolve()
        self.env_path = Path(env_path or config._ROOT / ".env")

    @contextmanager
    def connect(self):
        # mode=ro refuses a missing DB instead of silently creating an empty one.
        conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")  # one consistent SQLite snapshot per tool call
            yield conn
        finally:
            conn.close()

    @staticmethod
    def envelope(data):
        return {"generated_at": dt.datetime.now(TZ).isoformat(), "timezone": "Asia/Shanghai",
                "cache_only": True, "data": data}

    def _stock(self, conn, code):
        short, canonical = normalize_code(code)
        aliases = (short, canonical, canonical[-2:].lower() + short)
        minute = conn.execute("""SELECT * FROM intraday_snapshots WHERE code IN (?,?,?) AND price>0
            ORDER BY date DESC,time DESC LIMIT 1""", aliases).fetchone()
        daily = conn.execute("""SELECT * FROM daily_records WHERE code IN (?,?,?) AND close>0
            ORDER BY date DESC LIMIT 1""", aliases).fetchone()
        holding = conn.execute("SELECT * FROM portfolio WHERE code IN (?,?,?) ORDER BY updated_at DESC LIMIT 1",
                               aliases).fetchone()
        name = conn.execute("SELECT name FROM stock_name_cache WHERE code IN (?,?,?) LIMIT 1", aliases).fetchone()
        quantity = int(holding["quantity"] or 0) if holding else 0
        cost = float(holding["cost_price"] or 0) if holding and quantity > 0 else 0
        max_price = float(holding["max_price"] or 0) if holding and quantity > 0 else 0
        stage_high = float(holding["stage_high"] or 0) if holding else 0
        stage_low = float(holding["stage_low"] or 0) if holding else 0
        result = {"code": canonical, "name": name[0] if name else (daily["name"] if daily else ""),
                  "cost_price": cost, "quantity": quantity, "stage_high": stage_high, "stage_low": stage_low,
                  "max_price": max_price, "status": "missing", "source": None, "observed_at": None,
                  "stale": True, "current_price": None, "avg_price": None, "strategy": None}
        if minute and (not daily or minute["date"] >= daily["date"]):
            price = float(minute["price"])
            stamp = dt.datetime.fromisoformat(f"{minute['date']}T{minute['time']}").replace(tzinfo=TZ)
            age = (dt.datetime.now(TZ) - stamp).total_seconds()
            # amount in minute snapshots is 千元; vol is shares, not lots.
            totals = conn.execute("""SELECT SUM(amount),SUM(vol) FROM intraday_snapshots
                WHERE code=? AND date=? AND time<=?""", (minute["code"], minute["date"], minute["time"])).fetchone()
            avg = totals[0] * 1000 / totals[1] if totals[0] and totals[1] else None
            high = float(minute["high"] or price)
            result.update(source="intraday_snapshots", observed_at=stamp.isoformat(),
                          stale=age > 180 or age < -60, timestamp_precision="minute")
        elif daily:
            price, high, avg = float(daily["close"]), float(daily["high"] or daily["close"]), daily["avg_price"]
            result.update(source="daily_records", observed_at=daily["date"], timestamp_precision="day",
                          note="仅有日期精度；可能为盘中保存值，不能认定为实时行情或最终收盘价。")
        else:
            return result
        # Use the same pure signal calculator as calculate_8848, without its DB/upstream side effects.
        tracked_high = max(max_price, high) if cost > 0 else high
        strategy = calculate_strategy(price, cost, tracked_high, stage_high, stage_low,
                                      stage_high > stage_low > 0)
        result.update(status="cached", current_price=price, avg_price=round(avg, 4) if avg else None,
                      strategy_high_used=tracked_high,
                      upper_line=round(avg / 0.98848, 4) if avg else None,
                      lower_line=round(avg * 0.98848, 4) if avg else None, strategy=strategy)
        return result

    def stock(self, code):
        normalize_code(code)
        with self.connect() as conn:
            return self.envelope(self._stock(conn, code))

    def watchlist(self, offset=0, limit=30):
        if not 0 <= offset <= 10000 or not 1 <= limit <= 50:
            raise ValueError("offset 范围 0–10000，limit 范围 1–50")
        # Separate MCP process sees web edits to the root .env without a restart.
        raw = dotenv_values(self.env_path).get("COMMON_STOCK_CODES", "") or ""
        codes = list(dict.fromkeys(x.strip() for x in raw.replace("，", ",").split(",") if x.strip()))
        with self.connect() as conn:
            stocks = []
            for code in codes[offset:offset + limit]:
                try:
                    normalize_code(code)
                except ValueError:
                    stocks.append({"code": code, "status": "invalid_code", "stale": True})
                else:
                    stocks.append(self._stock(conn, code))
        return self.envelope({"stocks": stocks, "total": len(codes),
                              "next_offset": offset + limit if offset + limit < len(codes) else None})

    def portfolio(self):
        with self.connect() as conn:
            holdings = []
            quotes, prev = {}, {}
            for row in conn.execute("SELECT * FROM portfolio WHERE cost_price>0 AND quantity>0 ORDER BY updated_at DESC"):
                stock = self._stock(conn, row["code"])
                short, _ = normalize_code(row["code"])
                holdings.append({"code": short, "cost": row["cost_price"], "quantity": row["quantity"],
                                 "name": stock["name"]})
                quotes[short] = {"price": stock["current_price"], "source": stock["source"],
                                 "observed_at": stock["observed_at"], "stale": stock["stale"]}
                aliases = (short, stock["code"], stock["code"][-2:].lower() + short)
                # Today's P&L is meaningful only when the quote is from today.
                date = (stock["observed_at"] or "")[:10]
                if date == dt.datetime.now(TZ).date().isoformat():
                    previous = conn.execute("""SELECT close FROM daily_records WHERE code IN (?,?,?)
                        AND date<? AND close>0 ORDER BY date DESC LIMIT 1""", (*aliases, date)).fetchone()
                    if previous:
                        prev[short] = previous[0]
            cash_row = conn.execute("SELECT value FROM account_settings WHERE setting_key='available_cash'").fetchone()
            cash = max(0, float(cash_row[0])) if cash_row else 0
            result = build_portfolio_overview(holdings, cash, quotes, prev)
            for item in result["stocks"]:
                quote = quotes[item["code"]]
                item.update(observed_at=quote["observed_at"], stale=quote["stale"])
            result["valuation_complete"] = all(q["price"] is not None for q in quotes.values())
            result["stale"] = any(q["stale"] for q in quotes.values())
            if not result["valuation_complete"]:
                # Existing UI sums known marks; don't label that partial sum as account equity in MCP.
                result["account_equity"] = None
                result["total_market_value"] = None
                for key in ("total_pnl_pct", "today_avg_pct", "total_pnl_abs", "today_pnl_abs"):
                    result[key] = None
            result["note"] = "盈亏百分比沿用网页的持仓等权平均，并非账户加权收益率；不提供可卖股数。"
        return self.envelope(result)

    def events(self, after_id=0, limit=30):
        if not 0 <= after_id <= 2**63 - 1 or not 1 <= limit <= 100:
            raise ValueError("after_id 须为非负整数，limit 范围 1–100")
        with self.connect() as conn:
            # Always ASC, including after_id=0: pagination must not skip older events.
            rows = conn.execute("""SELECT id,code,name,event_type,priority,price,message,triggered_at,
                read_at,max_gain_pct,max_drawdown_pct,evaluated_at FROM watch_events
                WHERE id>? ORDER BY id ASC LIMIT ?""", (after_id, limit + 1)).fetchall()
            batch = [dict(row) for row in rows[:limit]]
            return self.envelope({"events": batch, "next_after_id": batch[-1]["id"] if batch else after_id,
                                  "has_more": len(rows) > limit, "marks_read": False})

    def status(self):
        with self.connect() as conn:
            minute = conn.execute("SELECT date,time FROM intraday_snapshots ORDER BY date DESC,time DESC LIMIT 1").fetchone()
            daily = conn.execute("SELECT MAX(date) FROM daily_records").fetchone()[0]
            event_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM watch_events").fetchone()[0]
        return self.envelope({"database_readable": True, "latest_daily_date": daily,
                              "latest_intraday_at": f"{minute[0]}T{minute[1]}+08:00" if minute else None,
                              "latest_event_id": event_id, "background_worker_health": "unknown",
                              "note": "MCP 不启动或探测主服务后台线程；缓存时间不等同于线程健康。"})
