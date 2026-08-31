import sqlite3
import time
import json
from core.watch_conditions import validate_conditions, describe_conditions
from core.watch_actions import ACTIONS, infer_action

from core.config import DB_PATH, STOCK_NAME_CACHE, logger

VALUATION_HIST_LIMIT = 750  # 估值锚历史窗口（约 3 年交易日）

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
                ("ALTER TABLE daily_records ADD COLUMN vol REAL", "daily_records.vol"),
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS valuation_history (
                    date   TEXT NOT NULL,
                    code   TEXT NOT NULL,
                    pe_ttm REAL,
                    pb     REAL,
                    PRIMARY KEY (date, code)
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS financial_indicators (
                    code               TEXT NOT NULL,
                    end_date           TEXT NOT NULL,
                    ann_date           TEXT,
                    dt_netprofit_yoy   REAL,
                    netprofit_yoy      REAL,
                    q_netprofit_yoy    REAL,
                    q_sales_yoy        REAL,
                    basic_eps_yoy      REAL,
                    fetched_at         INTEGER NOT NULL,
                    PRIMARY KEY (code, end_date)
                )
            """)
            conn.commit()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_watchlist (
                    code       TEXT PRIMARY KEY,
                    enabled    INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_settings (
                    setting_key TEXT PRIMARY KEY,
                    value       REAL NOT NULL DEFAULT 0,
                    updated_at  INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_plans (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    code        TEXT NOT NULL,
                    name        TEXT NOT NULL DEFAULT '',
                    trade_date  TEXT NOT NULL,
                    bias        TEXT NOT NULL DEFAULT '',
                    summary     TEXT NOT NULL DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'draft',
                    source      TEXT NOT NULL DEFAULT 'ai',
                    raw_json    TEXT NOT NULL DEFAULT '{}',
                    created_at  INTEGER NOT NULL,
                    UNIQUE(code, trade_date)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_rules (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id              INTEGER NOT NULL,
                    rule_type            TEXT NOT NULL,
                    threshold            REAL NOT NULL,
                    confirmation_minutes INTEGER NOT NULL DEFAULT 1,
                    priority             TEXT NOT NULL DEFAULT 'observe',
                    message              TEXT NOT NULL DEFAULT '',
                    state                TEXT NOT NULL DEFAULT 'waiting',
                    consecutive_hits     INTEGER NOT NULL DEFAULT 0,
                    triggered_at         TEXT,
                    FOREIGN KEY(plan_id) REFERENCES watch_plans(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id      INTEGER NOT NULL,
                    code         TEXT NOT NULL,
                    name         TEXT NOT NULL DEFAULT '',
                    event_type   TEXT NOT NULL,
                    priority     TEXT NOT NULL,
                    price        REAL NOT NULL,
                    message      TEXT NOT NULL,
                    triggered_at TEXT NOT NULL
                )
            """)
            conn.commit()
            for stmt, label in [
                ("ALTER TABLE watch_events ADD COLUMN read_at TEXT", "watch_events.read_at"),
                ("ALTER TABLE watch_events ADD COLUMN max_gain_pct REAL", "watch_events.max_gain_pct"),
                ("ALTER TABLE watch_events ADD COLUMN max_drawdown_pct REAL", "watch_events.max_drawdown_pct"),
                ("ALTER TABLE watch_events ADD COLUMN evaluated_at TEXT", "watch_events.evaluated_at"),
                ("ALTER TABLE watch_rules ADD COLUMN paused INTEGER NOT NULL DEFAULT 0", "watch_rules.paused"),
                ("ALTER TABLE watch_rules ADD COLUMN original_threshold REAL", "watch_rules.original_threshold"),
                ("ALTER TABLE watch_rules ADD COLUMN revision_reason TEXT NOT NULL DEFAULT ''", "watch_rules.revision_reason"),
                ("ALTER TABLE watch_rules ADD COLUMN recovery_hits INTEGER NOT NULL DEFAULT 0", "watch_rules.recovery_hits"),
                ("ALTER TABLE watch_rules ADD COLUMN pause_source TEXT NOT NULL DEFAULT ''", "watch_rules.pause_source"),
                ("ALTER TABLE watch_rules ADD COLUMN state_changed_at TEXT", "watch_rules.state_changed_at"),
                ("ALTER TABLE watch_rules ADD COLUMN indicator_label TEXT NOT NULL DEFAULT ''", "watch_rules.indicator_label"),
                ("ALTER TABLE watch_rules ADD COLUMN conditions_json TEXT NOT NULL DEFAULT '[]'", "watch_rules.conditions_json"),
                ("ALTER TABLE watch_rules ADD COLUMN shadow_result_json TEXT NOT NULL DEFAULT '{}'", "watch_rules.shadow_result_json"),
                ("ALTER TABLE watch_rules ADD COLUMN action TEXT NOT NULL DEFAULT ''", "watch_rules.action"),
                ("ALTER TABLE watch_rules ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'pending'", "watch_rules.execution_status"),
                ("ALTER TABLE watch_rules ADD COLUMN target_quantity INTEGER", "watch_rules.target_quantity"),
                ("ALTER TABLE watch_rules ADD COLUMN snooze_until TEXT", "watch_rules.snooze_until"),
                ("ALTER TABLE watch_rules ADD COLUMN ignore_until_recovery INTEGER NOT NULL DEFAULT 0", "watch_rules.ignore_until_recovery"),
                ("ALTER TABLE watch_rules ADD COLUMN last_snapshot_time TEXT", "watch_rules.last_snapshot_time"),
                ("ALTER TABLE watch_rules ADD COLUMN active_event_id INTEGER", "watch_rules.active_event_id"),
                ("ALTER TABLE watch_events ADD COLUMN updated_at TEXT", "watch_events.updated_at"),
                ("ALTER TABLE watch_events ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1", "watch_events.repeat_count"),
            ]:
                try:
                    conn.execute(stmt)
                    conn.commit()
                    print(f"Migration: added {label} column.")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
            conn.execute("UPDATE watch_rules SET original_threshold=threshold WHERE original_threshold IS NULL")
            for rule_id, message in conn.execute("SELECT id,message FROM watch_rules WHERE action=''").fetchall():
                conn.execute("UPDATE watch_rules SET action=? WHERE id=?", (infer_action(message), rule_id))
            conn.execute("""CREATE TABLE IF NOT EXISTS watch_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                rule_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                trade_log_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                voided_at TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS watch_executions_rule ON watch_executions(rule_id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_shadow_checks (
                    rule_id INTEGER NOT NULL,
                    trade_date TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    code TEXT NOT NULL,
                    price REAL NOT NULL,
                    base_hit INTEGER NOT NULL,
                    legacy_confirmed INTEGER NOT NULL DEFAULT 0,
                    evaluation_json TEXT NOT NULL,
                    rule_json TEXT NOT NULL,
                    PRIMARY KEY(rule_id, trade_date, snapshot_time)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_calibration_runs (
                    trade_date  TEXT NOT NULL,
                    slot        TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    started_at  TEXT NOT NULL,
                    completed_at TEXT,
                    error       TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(trade_date, slot)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watch_plan_revisions (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id             INTEGER NOT NULL,
                    trade_date          TEXT NOT NULL,
                    slot                TEXT NOT NULL,
                    source              TEXT NOT NULL,
                    decision            TEXT NOT NULL,
                    reason              TEXT NOT NULL DEFAULT '',
                    original_rules_json TEXT NOT NULL DEFAULT '[]',
                    applied_rules_json  TEXT NOT NULL DEFAULT '[]',
                    created_at          TEXT NOT NULL,
                    UNIQUE(plan_id, slot),
                    FOREIGN KEY(plan_id) REFERENCES watch_plans(id) ON DELETE CASCADE
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
            quantity = int(row[4]) if row[4] else 0
            # 持股数量是持仓状态的唯一依据；避免“股数已清空但成本仍在”的幽灵持仓。
            cost = row[0] if quantity > 0 else 0
            max_price = (row[3] if row[3] is not None else 0.0) if quantity > 0 else 0.0
            return {
                "cost":       cost,
                "stage_high": row[1],
                "stage_low":  row[2],
                "max_price":  max_price,
                "quantity":   quantity,
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


def get_available_cash() -> float:
    """读取账户可用现金；未设置时返回 0。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT value FROM account_settings WHERE setting_key='available_cash'"
        ).fetchone()
        conn.close()
        return max(0.0, float(row[0])) if row else 0.0
    except Exception as e:
        logger.error("get_available_cash failed: %s", e)
        return 0.0


def save_available_cash(amount: float) -> None:
    """保存账户可用现金。"""
    amount = max(0.0, float(amount))
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO account_settings(setting_key,value,updated_at)
           VALUES('available_cash',?,?)
           ON CONFLICT(setting_key) DO UPDATE SET
             value=excluded.value,updated_at=excluded.updated_at""",
        (amount, int(time.time())),
    )
    conn.commit()
    conn.close()


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
    """返回所有成本价、持股数量均有效的持仓，含股票名称缓存。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""
            SELECT p.code, p.cost_price, p.max_price,
                   COALESCE(s.name, '') AS name,
                   COALESCE(p.quantity, 0) AS quantity
            FROM portfolio p
            LEFT JOIN stock_name_cache s ON s.code = p.code
            WHERE p.cost_price > 0 AND COALESCE(p.quantity, 0) > 0
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
    """从 valuation_history 或 daily_records 取最新 pe/pb。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT pe_ttm, pb FROM valuation_history WHERE code = ? "
            "ORDER BY date DESC LIMIT 1",
            (code,),
        ).fetchone()
        if not row:
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
            result["pe_ttm"] = round(float(row[0]), 1)
        if row[1] is not None:
            result["pb"] = round(float(row[1]), 1)
        return result
    except Exception as e:
        logger.error("get_latest_valuation failed for %s: %s", code, e)
        return {}


def upsert_valuation_history(records: list[tuple]) -> int:
    """批量写入估值历史。records: [(date, code, pe_ttm, pb), ...]"""
    if not records:
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany(
            """
            INSERT INTO valuation_history(date, code, pe_ttm, pb)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                pe_ttm = excluded.pe_ttm,
                pb     = excluded.pb
            """,
            records,
        )
        conn.commit()
        conn.close()
        return len(records)
    except Exception as e:
        logger.error("upsert_valuation_history failed: %s", e)
        return 0


def get_valuation_history_series(code: str, limit: int = VALUATION_HIST_LIMIT) -> dict | None:
    """读取本地估值历史序列，供估值锚计算使用。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT date, pe_ttm, pb FROM valuation_history "
            "WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return None

        current: dict[str, float] = {}
        pe_hist: list[float] = []
        pb_hist: list[float] = []
        for i, (_, pe_ttm, pb) in enumerate(rows):
            if i == 0:
                if pe_ttm is not None and pe_ttm > 0:
                    current["pe_ttm"] = round(float(pe_ttm), 1)
                if pb is not None and pb > 0:
                    current["pb"] = round(float(pb), 1)
            if pe_ttm is not None and pe_ttm > 0:
                pe_hist.append(float(pe_ttm))
            if pb is not None and pb > 0:
                pb_hist.append(float(pb))

        return {
            "current": current,
            "pe_ttm_hist": pe_hist,
            "pb_hist": pb_hist,
            "count": len(rows),
            "last_date": rows[0][0],
        }
    except Exception as e:
        logger.error("get_valuation_history_series failed for %s: %s", code, e)
        return None


def is_valuation_cache_fresh(code: str) -> bool:
    """本地估值缓存是否足够新（与最新日线日期对齐，或 3 自然日内）。"""
    try:
        from datetime import date

        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            """
            SELECT
                (SELECT MAX(date) FROM valuation_history WHERE code = ?) AS val_date,
                (SELECT MAX(date) FROM daily_records WHERE code = ? AND close > 0) AS daily_date
            """,
            (code, code),
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return False
        val_date, daily_date = row[0], row[1]
        if daily_date:
            return val_date >= daily_date
        return (date.today() - date.fromisoformat(val_date)).days <= 3
    except Exception as e:
        logger.error("is_valuation_cache_fresh failed for %s: %s", code, e)
        return False


def upsert_financial_indicators(records: list[tuple]) -> int:
    """缓存财务指标。records 与 financial_indicators 除主键外字段顺序一致。"""
    if not records:
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany(
            """
            INSERT INTO financial_indicators(
                code, end_date, ann_date, dt_netprofit_yoy, netprofit_yoy,
                q_netprofit_yoy, q_sales_yoy, basic_eps_yoy, fetched_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, end_date) DO UPDATE SET
                ann_date         = excluded.ann_date,
                dt_netprofit_yoy = excluded.dt_netprofit_yoy,
                netprofit_yoy    = excluded.netprofit_yoy,
                q_netprofit_yoy  = excluded.q_netprofit_yoy,
                q_sales_yoy      = excluded.q_sales_yoy,
                basic_eps_yoy    = excluded.basic_eps_yoy,
                fetched_at       = excluded.fetched_at
            """,
            records,
        )
        conn.commit()
        conn.close()
        return len(records)
    except Exception as e:
        logger.error("upsert_financial_indicators failed: %s", e)
        return 0


def get_financial_indicators(code: str, limit: int = 4) -> list[dict]:
    """按报告期倒序读取最近财务指标。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """
            SELECT end_date, ann_date, dt_netprofit_yoy, netprofit_yoy,
                   q_netprofit_yoy, q_sales_yoy, basic_eps_yoy, fetched_at
            FROM financial_indicators WHERE code = ?
            ORDER BY end_date DESC LIMIT ?
            """,
            (code, limit),
        ).fetchall()
        conn.close()
        keys = ("end_date", "ann_date", "dt_netprofit_yoy", "netprofit_yoy",
                "q_netprofit_yoy", "q_sales_yoy", "basic_eps_yoy", "fetched_at")
        return [dict(zip(keys, row)) for row in rows]
    except Exception as e:
        logger.error("get_financial_indicators failed for %s: %s", code, e)
        return []


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


def get_prev_closes(codes: list[str], before_date: str | None = None) -> dict[str, float]:
    """一次读取多只股票的最近收盘价，避免持仓概览逐只打开 SQLite。"""
    if not codes:
        return {}
    normalized = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    marks = ",".join("?" for _ in normalized)
    date_clause = "AND date < ?" if before_date else ""
    params = normalized + ([before_date] if before_date else [])
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            f"""SELECT d.code,d.close FROM daily_records d
                JOIN (SELECT code,MAX(date) AS max_date FROM daily_records
                      WHERE code IN ({marks}) AND close>0 {date_clause} GROUP BY code) x
                ON x.code=d.code AND x.max_date=d.date""",
            params,
        ).fetchall()
        conn.close()
        return {str(code): float(close) for code, close in rows if close is not None}
    except Exception as e:
        logger.error("get_prev_closes failed: %s", e)
        return {}


# ── 次日盯盘 ────────────────────────────────────────────────────────────────

def set_watch_enabled(code: str, enabled: bool) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO stock_watchlist(code, enabled, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
        (code, int(enabled), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_watch_enabled_map(codes: list[str] | None = None) -> dict[str, bool]:
    conn = sqlite3.connect(DB_PATH)
    if codes:
        marks = ",".join("?" for _ in codes)
        rows = conn.execute(f"SELECT code, enabled FROM stock_watchlist WHERE code IN ({marks})", codes).fetchall()
    else:
        rows = conn.execute("SELECT code, enabled FROM stock_watchlist").fetchall()
    conn.close()
    return {code: bool(enabled) for code, enabled in rows}


def save_watch_plans(plans: list[dict], trade_date: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for plan in plans:
            code = str(plan.get("code", "")).strip()
            rules = (plan.get("rules") or [])[:4]
            if not code or not rules:
                continue
            protected = conn.execute("""SELECT 1 FROM watch_rules r JOIN watch_plans p ON p.id=r.plan_id
                WHERE p.code=? AND p.trade_date=? AND (r.execution_status!='pending' OR r.snooze_until IS NOT NULL
                OR r.ignore_until_recovery=1 OR EXISTS(SELECT 1 FROM watch_executions x WHERE x.rule_id=r.id))""",
                (code, trade_date)).fetchone()
            if protected:
                logger.warning("skip watch plan with execution feedback code=%s", code)
                continue
            # Reject the entire malformed plan before replacing any existing draft.
            # Silently discarding an unsupported condition would change its meaning.
            try:
                for candidate in rules:
                    candidate["conditions"] = validate_conditions(candidate.get("conditions", []))
                    if candidate.get("priority") == "risk" and candidate["conditions"]:
                        raise ValueError("风险规则不得附加量能/MACD条件")
                    candidate["action"] = candidate.get("action") or infer_action(candidate.get("message", ""))
                    if candidate["action"] not in ACTIONS:
                        raise ValueError("无效动作")
            except (ValueError, TypeError, AttributeError):
                logger.warning("skip watch plan with unsupported conditions code=%s", code)
                continue
            try:
                current_price = float(plan.get("_current_price") or 0)
            except (TypeError, ValueError):
                current_price = 0
            breakouts, breakdowns = [], []
            for candidate in rules:
                try:
                    candidate_price = float(candidate.get("price"))
                except (TypeError, ValueError):
                    continue
                if candidate.get("type") == "breakout":
                    breakouts.append(candidate_price)
                elif candidate.get("type") == "breakdown":
                    breakdowns.append(candidate_price)
            if breakouts and breakdowns and min(breakouts) <= max(breakdowns):
                logger.warning("skip inconsistent watch plan code=%s breakout<=breakdown", code)
                continue
            now = int(time.time())
            conn.execute(
                """INSERT INTO watch_plans(code,name,trade_date,bias,summary,status,source,raw_json,created_at)
                   VALUES(?,?,?,?,?,'draft','ai',?,?)
                   ON CONFLICT(code,trade_date) DO UPDATE SET name=excluded.name,bias=excluded.bias,
                   summary=excluded.summary,status='draft',raw_json=excluded.raw_json,created_at=excluded.created_at""",
                (code, str(plan.get("name", "")), trade_date, str(plan.get("bias", "")),
                 str(plan.get("summary", "")), json.dumps(plan, ensure_ascii=False), now),
            )
            plan_id = conn.execute(
                "SELECT id FROM watch_plans WHERE code=? AND trade_date=?", (code, trade_date)
            ).fetchone()[0]
            conn.execute("DELETE FROM watch_rules WHERE plan_id=?", (plan_id,))
            inserted_rules = 0
            for rule in rules:
                kind = str(rule.get("type", ""))
                try:
                    threshold = float(rule.get("price"))
                    confirmation = max(1, min(5, int(rule.get("confirmation_minutes", 1))))
                except (TypeError, ValueError):
                    continue
                if kind not in {"breakout", "breakdown", "near", "rapid_move_5m", "volume_spike"} or threshold <= 0:
                    continue
                if kind in {"breakout", "breakdown", "near"} and current_price > 0:
                    if abs(threshold - current_price) / current_price > 0.15:
                        continue
                if kind == "rapid_move_5m" and not 0.3 <= threshold <= 10:
                    continue
                if kind == "volume_spike" and not 1.2 <= threshold <= 20:
                    continue
                priority = str(rule.get("priority", "observe"))
                if priority not in {"risk", "opportunity", "observe"}:
                    priority = "observe"
                conn.execute(
                    """INSERT INTO watch_rules(plan_id,rule_type,threshold,original_threshold,
                       confirmation_minutes,priority,message,indicator_label,conditions_json,action) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (plan_id, kind, threshold, threshold, confirmation, priority, str(rule.get("message", "")),
                     str(rule.get("indicator") or rule.get("line_name") or "")[:80],
                     json.dumps(rule["conditions"], ensure_ascii=False), rule["action"]),
                )
                inserted_rules += 1
            if inserted_rules:
                count += 1
            else:
                conn.execute("DELETE FROM watch_plans WHERE id=?", (plan_id,))
        conn.commit()
    finally:
        conn.close()
    return count


def get_watch_plans(trade_date: str | None = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    sql = "SELECT id,code,name,trade_date,bias,summary,status FROM watch_plans"
    args = []
    if trade_date:
        sql += " WHERE trade_date=?"
        args.append(trade_date)
    sql += " ORDER BY code"
    rows = conn.execute(sql, args).fetchall()
    plans = []
    for row in rows:
        rules = conn.execute(
            """SELECT id,rule_type,threshold,confirmation_minutes,priority,message,state,triggered_at,
                      paused,original_threshold,revision_reason,pause_source,state_changed_at,indicator_label,
                      conditions_json,shadow_result_json,action,execution_status,target_quantity,snooze_until,
                      (SELECT COALESCE(SUM(quantity),0) FROM watch_executions x WHERE x.rule_id=watch_rules.id AND x.voided_at IS NULL)
               FROM watch_rules WHERE plan_id=? ORDER BY id""",
            (row[0],),
        ).fetchall()
        plans.append({"id": row[0], "code": row[1], "name": row[2], "trade_date": row[3],
                      "bias": row[4], "summary": row[5], "status": row[6],
                      "rules": [{"id": r[0], "type": r[1], "price": r[2], "confirmation_minutes": r[3],
                                 "priority": r[4], "message": r[5], "state": r[6], "triggered_at": r[7],
                                 "paused": bool(r[8]), "original_threshold": r[9], "revision_reason": r[10],
                                 "pause_source": r[11], "state_changed_at": r[12],
                                 "indicator": r[13], "conditions": json.loads(r[14]),
                                 "conditions_description": describe_conditions(json.loads(r[14])),
                                 "shadow_result": json.loads(r[15]), "action": r[16], "execution_status": r[17],
                                 "target_quantity": r[18], "snooze_until": r[19], "filled_quantity": r[20]} for r in rules]})
    conn.close()
    return plans


def get_recent_watch_plan_dates(before_date: str, limit: int = 10) -> list[str]:
    """返回指定日期之前最近的计划日期，供页面展示已过期历史计划。"""
    limit = max(1, min(int(limit), 60))
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT DISTINCT trade_date FROM watch_plans
           WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?""",
        (before_date, limit),
    ).fetchall()
    conn.close()
    return [str(row[0]) for row in rows]


def activate_watch_plans(trade_date: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE watch_plans SET status='active' WHERE trade_date=? AND status='draft'", (trade_date,))
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def expire_watch_plans(before_date: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE watch_plans SET status='expired' WHERE trade_date < ? AND status IN ('draft','active')",
        (before_date,),
    )
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def update_watch_rule(rule_id: int, threshold: float, confirmation_minutes: int) -> bool:
    if threshold <= 0:
        return False
    confirmation_minutes = max(1, min(5, int(confirmation_minutes)))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """UPDATE watch_rules SET threshold=?, confirmation_minutes=?, state='waiting',
           consecutive_hits=0,recovery_hits=0,triggered_at=NULL,paused=0,pause_source='',
           state_changed_at=NULL,shadow_result_json='{}',revision_reason='用户手动修改' WHERE id=?""",
        (threshold, confirmation_minutes, rule_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def get_recent_watch_events(limit: int = 50, after_id: int = 0) -> list[dict]:
    from core.watch_execution import event_details
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT id,code,name,event_type,priority,price,message,triggered_at,read_at,
                  max_gain_pct,max_drawdown_pct,evaluated_at
           FROM watch_events WHERE id > ? ORDER BY id DESC LIMIT ?""",
        (after_id, limit),
    ).fetchall()
    result = [event_details(conn, row[0]) for row in rows]
    conn.close()
    return result


def mark_watch_events_read(up_to_id: int | None = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if up_to_id is None:
        cur = conn.execute("UPDATE watch_events SET read_at=? WHERE read_at IS NULL", (now,))
    else:
        cur = conn.execute("UPDATE watch_events SET read_at=? WHERE read_at IS NULL AND id<=?", (now, up_to_id))
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def get_watch_plan_revisions(trade_date: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT x.id,x.plan_id,p.code,p.name,x.slot,x.source,x.decision,x.reason,
                  x.applied_rules_json,x.created_at
           FROM watch_plan_revisions x JOIN watch_plans p ON p.id=x.plan_id
           WHERE x.trade_date=? ORDER BY x.created_at,x.id""",
        (trade_date,),
    ).fetchall()
    conn.close()
    keys = ("id", "plan_id", "code", "name", "slot", "source", "decision", "reason",
            "applied_rules_json", "created_at")
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        try:
            item["applied_rules"] = json.loads(item.pop("applied_rules_json"))
        except Exception:
            item["applied_rules"] = []
        result.append(item)
    return result


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
