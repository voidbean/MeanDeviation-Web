import sqlite3
import time
import json

from core.config import DB_PATH, STOCK_NAME_CACHE, logger

# 三大指数代码与名称
INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
]


def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_name_cache (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    updated_at INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_records (
                    date TEXT,
                    code TEXT,
                    name TEXT,
                    close REAL,
                    high REAL,
                    low REAL,
                    avg_price REAL,
                    PRIMARY KEY (date, code)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio (
                    code TEXT PRIMARY KEY,
                    cost_price REAL DEFAULT 0,
                    stage_high REAL DEFAULT 0,
                    stage_low REAL DEFAULT 0,
                    updated_at INTEGER
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS query_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    code       TEXT,
                    name       TEXT,
                    queried_at TEXT
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS temp_results (
                    result_id  TEXT PRIMARY KEY,
                    payload    TEXT,
                    created_at INTEGER
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intraday_snapshots (
                    code    TEXT,
                    date    TEXT,
                    time    TEXT,
                    price   REAL,
                    open    REAL,
                    high    REAL,
                    low     REAL,
                    vol     REAL,
                    amount  REAL,
                    PRIMARY KEY (code, date, time)
                )
            """)
            conn.commit()

            for stmt, label in [
                ("ALTER TABLE portfolio ADD COLUMN max_price REAL DEFAULT 0",    "max_price"),
                ("ALTER TABLE portfolio ADD COLUMN quantity INTEGER DEFAULT 0",  "quantity"),
                ("ALTER TABLE daily_records ADD COLUMN amount REAL DEFAULT 0",   "amount"),
                ("ALTER TABLE daily_records ADD COLUMN open REAL DEFAULT 0",     "open"),
            ]:
                try:
                    conn.execute(stmt)
                    conn.commit()
                    print(f"Migration: added {label} column.")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise

            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    code          TEXT    NOT NULL,
                    name          TEXT    NOT NULL DEFAULT '',
                    trade_time    TEXT    NOT NULL,
                    direction     TEXT    NOT NULL,
                    price         REAL    NOT NULL,
                    volume        INTEGER NOT NULL,
                    thought       TEXT    DEFAULT '',
                    emotion       TEXT    DEFAULT '冷静',
                    review_result TEXT    DEFAULT NULL,
                    reviewed_at   TEXT    DEFAULT NULL,
                    created_at    TEXT    DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_conversations (
                    session_id  TEXT PRIMARY KEY,
                    stock_code  TEXT NOT NULL,
                    messages    TEXT NOT NULL DEFAULT '[]',
                    created_at  INTEGER NOT NULL,
                    updated_at  INTEGER NOT NULL
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_tags (
                    code TEXT PRIMARY KEY,
                    tag  TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to init db: {e}")


def get_cached_name(code: str) -> str:
    name = STOCK_NAME_CACHE.get(code)
    if name:
        return name
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute("SELECT name FROM stock_name_cache WHERE code = ?", (code,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row and row[0]:
            name = str(row[0])
            STOCK_NAME_CACHE[code] = name
            return name
    except Exception as e:
        print(f"Failed to read cache for {code}: {e}")
    return ""


def set_cached_name(code: str, name: str) -> None:
    if not code or not name:
        return
    STOCK_NAME_CACHE[code] = name
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            ts_now = int(time.time())
            conn.execute("""
                INSERT INTO stock_name_cache(code, name, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
            """, (code, name, ts_now))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to write cache for {code}: {e}")


def save_daily_record(code: str, name: str, data: dict):
    try:
        conn = sqlite3.connect(DB_PATH)
        today = time.strftime("%Y-%m-%d")
        conn.execute("""
            INSERT INTO daily_records(date, code, name, close, high, low, avg_price, open)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                close = excluded.close,
                high = excluded.high,
                low = excluded.low,
                avg_price = excluded.avg_price,
                name = excluded.name,
                open = excluded.open
        """, (today, code, name, data['price'], data['high'], data['low'], data['avg_price'], data.get('open', 0)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save daily record for {code}: {e}")


def get_portfolio(code: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT cost_price, stage_high, stage_low, max_price, COALESCE(quantity, 0) FROM portfolio WHERE code = ?",
            (code,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "cost":       row[0],
                "stage_high": row[1],
                "stage_low":  row[2],
                "max_price":  row[3] if row[3] is not None else 0.0,
                "quantity":   int(row[4]) if row[4] else 0,
            }
    except Exception as e:
        logger.error(f"Failed to get portfolio for {code}: {e}")
    return {"cost": 0, "stage_high": 0, "stage_low": 0, "max_price": 0.0, "quantity": 0}


def save_portfolio(code: str, cost: float, high: float, low: float, max_price: float = 0.0, quantity: int = 0):
    try:
        conn = sqlite3.connect(DB_PATH)
        ts_now = int(time.time())
        conn.execute("""
            INSERT INTO portfolio(code, cost_price, stage_high, stage_low, max_price, quantity, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                cost_price = excluded.cost_price,
                stage_high = excluded.stage_high,
                stage_low  = excluded.stage_low,
                max_price  = excluded.max_price,
                quantity   = excluded.quantity,
                updated_at = excluded.updated_at
        """, (code, cost, high, low, max_price, quantity, ts_now))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save portfolio for {code}: {e}")


def save_query_history(code: str, name: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO query_history(code, name, queried_at) VALUES(?, ?, ?)",
            (code, name, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute(
            "DELETE FROM query_history WHERE id NOT IN "
            "(SELECT id FROM query_history ORDER BY id DESC LIMIT 50)"
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save query history for {code}: {e}")


def get_query_history() -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT code, name, queried_at FROM query_history ORDER BY id DESC LIMIT 50"
        )
        rows = cur.fetchall()
        conn.close()
        return [{"code": r[0], "name": r[1], "queried_at": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Failed to get query history: {e}")
    return []


def save_temp_result(result_id: str, payload: dict) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO temp_results(result_id, payload, created_at) VALUES(?, ?, ?)",
            (result_id, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.execute("DELETE FROM temp_results WHERE created_at < ?", (now - 1800,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("save_temp_result failed: %s", e)


def load_temp_result(result_id: str, keep: bool = False) -> dict:
    if not result_id:
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT payload FROM temp_results WHERE result_id = ?", (result_id,)
        )
        row = cur.fetchone()
        if row:
            if not keep:
                conn.execute("DELETE FROM temp_results WHERE result_id = ?", (result_id,))
                conn.commit()
            conn.close()
            return json.loads(row[0])
        conn.close()
    except Exception as e:
        logger.error("load_temp_result failed: %s", e)
    return {}


def get_n_day_stats(code: str):
    result = {
        "n20_high": 0, "n20_low": 0,
        "n40_high": 0, "n40_low": 0,
        "n60_high": 0, "n60_low": 0,
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        for days, high_key, low_key in [
            (20, "n20_high", "n20_low"),
            (40, "n40_high", "n40_low"),
            (60, "n60_high", "n60_low"),
        ]:
            cur = conn.execute(
                "SELECT MAX(high), MIN(low) FROM daily_records WHERE code = ? AND date >= date('now', ?)",
                (code, f'-{days} days'),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                result[high_key] = row[0]
                result[low_key]  = row[1]
        conn.close()
    except Exception as e:
        logger.error(f"Failed to get stats for {code}: {e}")
    return result


def calc_atr(code: str, period: int = 14) -> float | None:
    needed = period + 1
    ts_code = code if "." in code else None
    short_code = code.split(".")[0] if "." in code else code
    rows = None
    try:
        conn = sqlite3.connect(DB_PATH)
        for q_code in ([ts_code, short_code] if ts_code else [short_code]):
            if q_code is None:
                continue
            cur = conn.execute(
                "SELECT high, low, close FROM daily_records "
                "WHERE code = ? AND high > 0 AND low > 0 AND close > 0 "
                "ORDER BY date DESC LIMIT ?",
                (q_code, needed),
            )
            rows = cur.fetchall()
            if rows:
                break
        conn.close()
    except Exception as e:
        logger.error("calc_atr failed for %s: %s", code, e)
        return None

    if not rows or len(rows) < 2:
        return None

    rows = list(reversed(rows))
    tr_list = []
    for i in range(1, len(rows)):
        high_i     = rows[i][0]
        low_i      = rows[i][1]
        close_prev = rows[i - 1][2]
        tr = max(high_i - low_i, abs(high_i - close_prev), abs(low_i - close_prev))
        tr_list.append(tr)

    if not tr_list:
        return None

    use_n = min(period, len(tr_list))
    return round(sum(tr_list[-use_n:]) / use_n, 4)


def get_index_market_data(days: int = 20) -> dict:
    result = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for ts_code, idx_name in INDEX_CODES:
            cur = conn.execute("""
                SELECT date, close, high, low, COALESCE(amount, 0), COALESCE(open, close)
                FROM daily_records
                WHERE code = ?
                ORDER BY date DESC
                LIMIT ?
            """, (ts_code, days))
            rows = cur.fetchall()
            records = []
            for r in rows:
                amount_yi = round(r[4] / 100000, 2) if r[4] else 0
                records.append({
                    "date":      r[0],
                    "close":     r[1],
                    "high":      r[2],
                    "low":       r[3],
                    "amount_yi": amount_yi,
                    "open":      r[5],
                })
            result[ts_code] = {"name": idx_name, "records": records}
        conn.close()
    except Exception as e:
        logger.error(f"Failed to get index market data: {e}")
    return result


def get_index_trend_chart_data(days: int = 20) -> dict | None:
    index_data = get_index_market_data(days=days)

    def extract(ts_code: str) -> list:
        recs = index_data.get(ts_code, {}).get("records", [])
        return list(reversed(recs))

    sh_recs = extract("000001.SH")
    sz_recs = extract("399001.SZ")
    cy_recs = extract("399006.SZ")

    if not sh_recs:
        return None

    def to_pct(records: list) -> list:
        if not records:
            return []
        base = records[0]["close"]
        if not base:
            return [None] * len(records)
        return [round((r["close"] - base) / base * 100, 2) if r["close"] else None for r in records]

    def pad(lst: list, length: int):
        return lst[:length] + [None] * max(0, length - len(lst))

    dates      = [r["date"]      for r in sh_recs]
    sh_close   = [r["close"]     for r in sh_recs]
    sh_amounts = [r["amount_yi"] for r in sh_recs]
    sz_close   = [r["close"]     for r in sz_recs] if sz_recs else []
    cy_close   = [r["close"]     for r in cy_recs] if cy_recs else []
    n = len(dates)

    return {
        "dates":      dates,
        "sh_pct":     to_pct(sh_recs),
        "sz_pct":     to_pct(sz_recs) if sz_recs else [None] * n,
        "cy_pct":     to_pct(cy_recs) if cy_recs else [None] * n,
        "sh_close":   pad(sh_close,   n),
        "sz_close":   pad(sz_close,   n),
        "cy_close":   pad(cy_close,   n),
        "sh_amounts": pad(sh_amounts, n),
    }


def get_klines_around_date(code: str, center_date: str, n: int = 10) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute(
            "SELECT date FROM daily_records WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
            (code, center_date, n)
        ).fetchall()
        after = conn.execute(
            "SELECT date FROM daily_records WHERE code=? AND date>? ORDER BY date ASC LIMIT ?",
            (code, center_date, n)
        ).fetchall()
        if not before:
            return []
        start_date = before[-1]["date"]
        end_date   = after[-1]["date"] if after else before[0]["date"]
        rows = conn.execute(
            "SELECT date, COALESCE(open, close) AS open, high, low, close, amount "
            "FROM daily_records WHERE code=? AND date BETWEEN ? AND ? ORDER BY date ASC",
            (code, start_date, end_date)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_ai_conversation(session_id: str, stock_code: str, messages: list) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        now = int(time.time())
        conn.execute("""
            INSERT INTO ai_conversations(session_id, stock_code, messages, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET messages=excluded.messages, updated_at=excluded.updated_at
        """, (session_id, stock_code, json.dumps(messages, ensure_ascii=False), now, now))
        conn.execute("DELETE FROM ai_conversations WHERE updated_at < ?", (now - 7200,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("save_ai_conversation failed: %s", e)


def get_stock_tag(code: str) -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute("SELECT tag FROM stock_tags WHERE code = ?", (code,))
            row = cur.fetchone()
        finally:
            conn.close()
        if row:
            return row[0] or ""
    except Exception as e:
        logger.error(f"Failed to get tag for {code}: {e}")
    return ""


def set_stock_tag(code: str, tag: str) -> None:
    if not code:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("""
                INSERT INTO stock_tags(code, tag) VALUES(?, ?)
                ON CONFLICT(code) DO UPDATE SET tag = excluded.tag
            """, (code, tag.strip()))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to set tag for {code}: {e}")


def get_all_stock_tags(codes: list) -> dict:
    if not codes:
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            placeholders = ",".join("?" * len(codes))
            cur = conn.execute(
                f"SELECT code, tag FROM stock_tags WHERE code IN ({placeholders})", codes
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception as e:
        logger.error(f"Failed to get all tags: {e}")
    return {}


def get_distinct_tags() -> list:
    """返回数据库中所有已使用的 tag，去重排序。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT DISTINCT tag FROM stock_tags WHERE tag != '' ORDER BY tag"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.error(f"Failed to get distinct tags: {e}")
    return []


def get_all_holdings() -> list:
    """返回所有已设置成本价的持仓，含股票名称缓存。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""
            SELECT p.code, p.cost_price, p.max_price,
                   COALESCE(s.name, '') AS name,
                   COALESCE(p.quantity, 0) AS quantity
            FROM portfolio p
            LEFT JOIN stock_name_cache s ON s.code = p.code
            WHERE p.cost_price > 0
            ORDER BY p.updated_at DESC
        """)
        rows = cur.fetchall()
        conn.close()
        return [
            {"code": row[0], "cost": row[1], "max_price": row[2] or 0.0, "name": row[3], "quantity": int(row[4])}
            for row in rows
        ]
    except Exception as e:
        logger.error("get_all_holdings failed: %s", e)
        return []


def get_latest_valuation(code: str) -> dict:
    """从 daily_records 取最新 pe/pb（由 fetch_history.py 的 stk_factor_pro 写入）。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT pe, pb FROM daily_records WHERE code = ? "
            "AND (pe IS NOT NULL OR pb IS NOT NULL) ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        result = {}
        if row[0] is not None:
            result["pe"] = round(float(row[0]), 1)
        if row[1] is not None:
            result["pb"] = round(float(row[1]), 1)
        return result
    except Exception as e:
        logger.error("get_latest_valuation failed for %s: %s", code, e)
        return {}


def get_prev_close(code: str) -> float | None:
    """从 daily_records 取最近一个交易日的收盘价（即昨收）。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT close FROM daily_records WHERE code = ? AND close > 0 ORDER BY date DESC LIMIT 1",
            (code,),
        )
        row = cur.fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception as e:
        logger.error("get_prev_close failed for %s: %s", code, e)
        return None


def load_ai_conversation(session_id: str) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT messages FROM ai_conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.error("load_ai_conversation failed: %s", e)
    return []
