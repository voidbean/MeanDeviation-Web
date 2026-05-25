from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import threading
import tushare as ts
import os
import sqlite3
import time
import json
import logging
from pathlib import Path
from dotenv import load_dotenv


# 基本日志配置，输出到 app.log，方便排查 tushare 等问题
logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), "app.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时开启后台分时快照抓取线程。"""
    t = threading.Thread(target=_intraday_bg_loop, daemon=True, name="intraday-fetcher")
    t.start()
    logger.info("lifespan: 后台分时快照线程已启动")
    yield
    # daemon 线程随主进程退出，无需额外清理


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# ── AI 分析配置 ──────────────────────────────────────────────
AI_PROVIDER      = os.getenv("AI_PROVIDER", "claude").lower()
CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL     = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
CLAUDE_BASE_URL  = os.getenv("CLAUDE_BASE_URL", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_BASE_URL  = os.getenv("OPENAI_BASE_URL", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

SKILLS_DIR = Path(__file__).parent / "skills"

# Tushare Token (Optional for basic realtime quotes, but recommended for stability)
TS_TOKEN = os.getenv("TUSHARE_TOKEN", "")
pro = None
if TS_TOKEN:
    ts.set_token(TS_TOKEN)
    pro = ts.pro_api()

# 简单的进程内股票名称缓存 + SQLite 持久化缓存
STOCK_NAME_CACHE = {}
DB_PATH = os.path.join(os.path.dirname(__file__), "stock_cache.db")


def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            # Existing cache table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_name_cache (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    updated_at INTEGER
                )
                """
            )
            # New table for daily records
            conn.execute(
                """
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
                """
            )
            # New table for portfolio settings
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio (
                    code TEXT PRIMARY KEY,
                    cost_price REAL DEFAULT 0,
                    stage_high REAL DEFAULT 0,
                    stage_low REAL DEFAULT 0,
                    updated_at INTEGER
                )
                """
            )
            conn.commit()

            # 查询历史记录表
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    code       TEXT,
                    name       TEXT,
                    queried_at TEXT
                )
                """
            )
            conn.commit()

            # PRG 临时结果表：POST 完把渲染上下文存这里，redirect 到 GET 后读取并删除
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS temp_results (
                    result_id  TEXT PRIMARY KEY,
                    payload    TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.commit()

            # 分时快照表：后台每30分钟用 rt_min 抓取自选股实时快照，AI 分析时读取
            conn.execute(
                """
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
                """
            )
            conn.commit()

            # 迁移：为 portfolio 表新增 max_price 字段（记录持仓以来历史最高价）
            # SQLite 不支持 ALTER TABLE ADD COLUMN IF NOT EXISTS，用 try/except 实现幂等
            try:
                conn.execute(
                    "ALTER TABLE portfolio ADD COLUMN max_price REAL DEFAULT 0"
                )
                conn.commit()
                print("Migration: added max_price column to portfolio table.")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    pass  # 字段已存在，忽略
                else:
                    raise

            # 迁移：为 daily_records 表新增 amount 字段（成交额，千元），用于大盘风向标量能判断
            try:
                conn.execute(
                    "ALTER TABLE daily_records ADD COLUMN amount REAL DEFAULT 0"
                )
                conn.commit()
                print("Migration: added amount column to daily_records table.")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    pass  # 字段已存在，忽略
                else:
                    raise

            # 迁移：为 daily_records 表新增 open 字段（开盘价），用于 K 线图
            try:
                conn.execute(
                    "ALTER TABLE daily_records ADD COLUMN open REAL DEFAULT 0"
                )
                conn.commit()
                print("Migration: added open column to daily_records table.")
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    pass  # 字段已存在，忽略
                else:
                    raise

        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to init db: {e}")


def get_cached_name(code: str) -> str:
    # 先查进程内内存缓存
    name = STOCK_NAME_CACHE.get(code)
    if name:
        return name

    # 再查 SQLite 持久化缓存
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT name FROM stock_name_cache WHERE code = ?", (code,)
            )
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
            conn.execute(
                """
                INSERT INTO stock_name_cache(code, name, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (code, name, ts_now),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to write cache for {code}: {e}")


init_db()


def load_common_stocks():
    """
    从环境变量 COMMON_STOCK_CODES 读取常用股票代码，格式例如：
    COMMON_STOCK_CODES=600519,000001,300750
    """
    raw = os.getenv("COMMON_STOCK_CODES", "") or ""
    # 兼容中英文逗号
    raw = raw.replace("，", ",")
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return [{"code": code} for code in codes]


COMMON_STOCKS = load_common_stocks()


def _update_env_key(path: str, key: str, value: str) -> None:
    """在 .env 文件中更新或新增指定 key 的值（幂等）。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []

        prefix = f"{key}="
        new_line = f"{key}={value}\n"
        found = False
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = new_line
                found = True
                break
        if not found:
            lines.append(new_line)

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.error(f"Failed to update .env key {key}: {e}")


def build_common_stocks_with_name():
    """
    为常用股票补充名称信息，用于页面展示。
    如获取失败，则名称留空，仅展示代码。
    """
    entries = []
    for item in COMMON_STOCKS:
        code = item.get("code")
        if not code:
            continue

        # 先从持久化/内存缓存中取名称
        name = get_cached_name(code)

        # 缓存中没有时再打一次实时接口，并写回缓存
        if not name:
            try:
                df = ts.get_realtime_quotes(code)
                if df is not None and not df.empty:
                    name = str(df.loc[0, "name"])
                    set_cached_name(code, name)
            except Exception:
                # 名称获取失败时忽略错误
                pass

        entries.append({"code": code, "name": name})
    return entries
def save_daily_record(code: str, name: str, data: dict):
    """
    Save daily record to DB.
    data includes: price (close), high, low, avg_price (vwap)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        today = time.strftime("%Y-%m-%d")
        conn.execute(
            """
            INSERT INTO daily_records(date, code, name, close, high, low, avg_price, open)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                close = excluded.close,
                high = excluded.high,
                low = excluded.low,
                avg_price = excluded.avg_price,
                name = excluded.name,
                open = excluded.open
            """,
            (today, code, name, data['price'], data['high'], data['low'], data['avg_price'], data.get('open', 0))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save daily record for {code}: {e}")

def get_portfolio(code: str):
    """
    Get portfolio settings for a stock.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT cost_price, stage_high, stage_low, max_price FROM portfolio WHERE code = ?",
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
            }
    except Exception as e:
        logger.error(f"Failed to get portfolio for {code}: {e}")
    return {"cost": 0, "stage_high": 0, "stage_low": 0, "max_price": 0.0}

def save_portfolio(code: str, cost: float, high: float, low: float, max_price: float = 0.0):
    """
    Save portfolio settings.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        ts_now = int(time.time())
        conn.execute(
            """
            INSERT INTO portfolio(code, cost_price, stage_high, stage_low, max_price, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                cost_price = excluded.cost_price,
                stage_high = excluded.stage_high,
                stage_low  = excluded.stage_low,
                max_price  = excluded.max_price,
                updated_at = excluded.updated_at
            """,
            (code, cost, high, low, max_price, ts_now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save portfolio for {code}: {e}")

def save_query_history(code: str, name: str) -> None:
    """记录一次查询到 query_history 表，只保留最近 50 条。"""
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
    """返回最近 50 条查询历史，供模板渲染。"""
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
    """将模板上下文（不含 request）JSON 序列化后写入 temp_results。
    同时清理 30 分钟前的旧记录，防止数据库无限增长。
    """
    import uuid as _uuid
    try:
        conn = sqlite3.connect(DB_PATH)
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO temp_results(result_id, payload, created_at) VALUES(?, ?, ?)",
            (result_id, json.dumps(payload, ensure_ascii=False), now),
        )
        # 清理 30 分钟前的旧记录
        conn.execute("DELETE FROM temp_results WHERE created_at < ?", (now - 1800,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("save_temp_result failed: %s", e)


def load_temp_result(result_id: str) -> dict:
    """读取并立即删除 temp_results 中对应记录（一次性消费）。
    找不到时返回空 dict。
    """
    if not result_id:
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT payload FROM temp_results WHERE result_id = ?", (result_id,)
        )
        row = cur.fetchone()
        if row:
            conn.execute("DELETE FROM temp_results WHERE result_id = ?", (result_id,))
            conn.commit()
            conn.close()
            return json.loads(row[0])
        conn.close()
    except Exception as e:
        logger.error("load_temp_result failed: %s", e)
    return {}


def get_n_day_stats(code: str):
    """
    同时返回 20 日和 60 日的高低点，供页面展示建议值。
    数据来源：daily_records 表（由 fetch_history.py 定时补充）。
    """
    result = {
        "n20_high": 0, "n20_low": 0,
        "n60_high": 0, "n60_low": 0,
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        for days, high_key, low_key in [
            (20, "n20_high", "n20_low"),
            (60, "n60_high", "n60_low"),
        ]:
            cur = conn.execute(
                """
                SELECT MAX(high), MIN(low) FROM daily_records
                WHERE code = ? AND date >= date('now', ?)
                """,
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

# 三大指数代码与名称（大盘风向标）
INDEX_CODES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
]


def get_index_market_data(days: int = 20) -> dict:
    """
    从 daily_records 读取三大指数最近 days 日数据，供 AI prompt 使用。
    返回格式：{ts_code: {"name": str, "records": [{"date", "close", "high", "low", "amount_yi"}]}}
    amount_yi：成交额（亿元），由 daily_records.amount（千元）换算。
    数据由 fetch_history.py 定时写入；若尚未运行则返回空列表。
    """
    result = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for ts_code, idx_name in INDEX_CODES:
            cur = conn.execute(
                """
                SELECT date, close, high, low, COALESCE(amount, 0)
                FROM daily_records
                WHERE code = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (ts_code, days),
            )
            rows = cur.fetchall()
            records = []
            for r in rows:
                amount_yi = round(r[4] / 100000, 2) if r[4] else 0  # 千元 → 亿元（1亿=10万千元）
                records.append({
                    "date":       r[0],
                    "close":      r[1],
                    "high":       r[2],
                    "low":        r[3],
                    "amount_yi":  amount_yi,
                })
            result[ts_code] = {"name": idx_name, "records": records}
        conn.close()
    except Exception as e:
        logger.error(f"Failed to get index market data: {e}")
    return result


def get_index_trend_chart_data(days: int = 20) -> dict | None:
    """
    返回三大指数（上证/深成/创业板）近 days 日走势数据，供前端 ECharts 多折线图渲染。
    收盘价归一化为相对首日的涨跌幅（%），方便三指数强弱对比。
    同时附带上证成交额，供 tooltip 展示量能参考。

    返回格式：
    {
        "dates":      ["2025-04-01", ...],       # X轴，升序，以上证日期为准
        "sh_pct":     [0.0, 1.2, -0.5, ...],    # 上证涨跌幅（%，相对首日）
        "sz_pct":     [...],                     # 深成涨跌幅
        "cy_pct":     [...],                     # 创业板涨跌幅
        "sh_close":   [3200.1, ...],             # 上证收盘价（tooltip用）
        "sz_close":   [...],                     # 深成收盘价
        "cy_close":   [...],                     # 创业板收盘价
        "sh_amounts": [3456.78, ...],            # 上证成交额（亿，tooltip用）
    }
    若无数据则返回 None。
    """
    index_data = get_index_market_data(days=days)

    def extract(ts_code: str) -> list:
        """取升序 records，不足则返回空列表。"""
        recs = index_data.get(ts_code, {}).get("records", [])
        return list(reversed(recs))  # get_index_market_data 返回降序

    sh_recs = extract("000001.SH")
    sz_recs = extract("399001.SZ")
    cy_recs = extract("399006.SZ")

    if not sh_recs:
        return None

    def to_pct(records: list) -> list[float | None]:
        """将收盘价序列转为相对首日的涨跌幅（%）。"""
        if not records:
            return []
        base = records[0]["close"]
        if not base:
            return [None] * len(records)
        return [round((r["close"] - base) / base * 100, 2) if r["close"] else None
                for r in records]

    dates      = [r["date"]      for r in sh_recs]
    sh_close   = [r["close"]     for r in sh_recs]
    sh_amounts = [r["amount_yi"] for r in sh_recs]
    sz_close   = [r["close"]     for r in sz_recs] if sz_recs else []
    cy_close   = [r["close"]     for r in cy_recs] if cy_recs else []

    # 对齐长度（以上证日期为准，其他指数可能数据天数略有差异）
    n = len(dates)
    def pad(lst: list, length: int):
        return lst[:length] + [None] * max(0, length - len(lst))

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


def get_stock_volume_chart_data(history_results: list) -> dict | None:
    """
    将 calculate_8848_history() 返回的 records（降序）转换为 ECharts 柱状图格式。
    若无数据则返回 None。
    """
    if not history_results:
        return None

    # calculate_8848_history 返回降序，反转为升序供图表使用
    records = list(reversed(history_results))

    dates   = [r["date"]       for r in records]
    amounts = [r.get("amount_yi", 0) for r in records]
    closes  = [r.get("close", None)  for r in records]
    colors: list[str] = []
    labels: list[str] = []

    for i, amt in enumerate(amounts):
        if i == 0 or amounts[i - 1] == 0:
            colors.append("#9e9e9e")
            labels.append("—")
        else:
            prev = amounts[i - 1]
            if amt > prev:
                colors.append("#ef5350")
                labels.append("放量")
            else:
                colors.append("#9e9e9e")
                labels.append("缩量")

    opens       = [r.get("open",       None) for r in records]
    highs       = [r.get("high",       None) for r in records]
    lows        = [r.get("low",        None) for r in records]
    upper_lines = [r.get("upper_line", None) for r in records]
    lower_lines = [r.get("lower_line", None) for r in records]
    avg_prices  = [r.get("avg_price",  None) for r in records]

    return {
        "dates":       dates,
        "amounts":     amounts,
        "colors":      colors,
        "labels":      labels,
        "closes":      closes,
        "opens":       opens,
        "highs":       highs,
        "lows":        lows,
        "upper_lines": upper_lines,
        "lower_lines": lower_lines,
        "avg_prices":  avg_prices,
    }


def calculate_strategy(now, cost, st_high, stage_high, stage_low, stage_params_set: bool = False):
    """
    Implement the strategy logic from stock.html
    """
    signal = "观望"
    advice_class = "secondary"

    # 斐波那契三条线：只有 stage_params_set=True 时才有意义
    # stage_params_set 已保证 stage_high > stage_low > 0，diff > 0
    if stage_params_set:
        diff = stage_high - stage_low
        f382 = stage_high - diff * 0.382
        f618 = stage_high - diff * 0.618
        f786 = stage_high - diff * 0.786
    else:
        diff = f382 = f618 = f786 = 0.0  # 未设置时全部为 0，前端据此显示提示

    is_break_low = False

    if cost > 0:
        # === 持仓模式 ===
        max_profit_rate = (st_high - cost) / cost if cost > 0 else 0

        if now < cost * 0.93:
            signal = "止损离场"
            advice_class = "danger"
        elif max_profit_rate >= 0.20:
            profit_limit = st_high - (st_high - cost) * 0.3
            if now <= profit_limit:
                signal = "动态止盈"
                advice_class = "warning"
            else:
                signal = "奔跑中"
                advice_class = "info"
        elif max_profit_rate >= 0.10:
            profit_limit = max(st_high - (st_high - cost) * 0.5, cost * 1.03)
            if now <= profit_limit:
                signal = "落袋/保本"
                advice_class = "warning"
            else:
                signal = "持有中"
                advice_class = "info"
        else:
            signal = "持有中"
            advice_class = "info"

    else:
        # === 观望模式 ===
        if not stage_params_set:
            # 阶段参数未有效设置，斐波那契信号全部跳过
            # 仅保留"突破跟进"（只需 stage_high > 0，不依赖 diff）
            if stage_high > 0 and now > stage_high:
                signal = "突破跟进"
                advice_class = "danger"
            else:
                signal = "观望"
                advice_class = "secondary"
        else:
            # stage_params_set=True：stage_high > stage_low > 0，diff > 0，斐波那契全部有效
            if now < stage_low:
                is_break_low = True
                signal = "破位严禁"
                advice_class = "danger"
            elif now <= f786:
                signal = "黄金坑"
                advice_class = "warning"
            elif now <= f618:
                signal = "强支撑"
                advice_class = "primary"
            elif now <= f382:
                signal = "常规买点"
                advice_class = "info"
            elif now > stage_high:
                signal = "突破跟进"
                advice_class = "danger"
            else:
                signal = "观望"
                advice_class = "secondary"

    return {
        "signal": signal,
        "advice_class": advice_class,
        "f382": round(f382, 4),
        "f618": round(f618, 4),
        "f786": round(f786, 4),
        "is_break_low": is_break_low,
    }

def load_skills() -> str:
    """读取 skills/ 目录下所有 .md 文件，拼接为字符串"""
    if not SKILLS_DIR.exists():
        return ""
    parts = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        parts.append(f"## {f.name}\n\n" + f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def build_ai_prompt(result: dict, history: list, mode: str = "intraday", user_hint: str = "", index_data: dict = None) -> str:
    """将股票数据 + 持仓参数 + 历史数据 + 大盘指数数据组装成分析 prompt
    mode: 'intraday' = 盘中（今天怎么操作）| 'next_day' = 盘后（明天怎么操作）
    index_data: get_index_market_data() 的返回值，None 时不展示大盘段落
    """
    history_text = "\n".join(
        f"  {r['date']}: 收{r['close']} 高{r['high']} 低{r['low']} 均价{r['avg_price']}"
        for r in history
    )
    holding = result['cost_price'] > 0
    fib_text = (
        f"斐波那契 38.2%: {result['f382']}  61.8%: {result['f618']}  78.6%: {result['f786']}"
        if result.get('stage_params_set')
        else "（未设置阶段高低点，斐波那契不可用）"
    )

    if mode == "intraday":
        mode_context = "【分析时机】盘中分析，当前行情仍在进行中。"
        op3_focus = (
            "当前持仓，重点关注：今天是否需要减仓或止盈？当前价位是否已到卖点？还是应该继续持有等待？"
            if holding else
            "当前未持仓，重点关注：今天是否有买入机会？当前价位是否是合适的介入点？还是应该继续观望？"
        )
        op3_label = "今日操作建议"
        extra_instruction = (
            "请特别给出今日具体的操作价位建议（如：可在 XX 附近买入 / 涨到 XX 可减仓），"
            "结合今日已有的高低点和当前价格判断当下时机，不要只给方向性建议。"
        )
    else:
        mode_context = "【分析时机】收盘后复盘，今日行情已结束，分析明日操作计划。"
        op3_focus = (
            "当前持仓，重点关注：明天是否需要操作？持仓逻辑是否仍然成立？止盈/止损位在哪里？"
            if holding else
            "当前未持仓，重点关注：明天是否有买入机会？需要关注哪些信号来确认入场时机？"
        )
        op3_label = "明日操作计划"
        extra_instruction = (
            "请给出明日具体的操作预案（如：若明日高开则 XX，若低开则 XX），"
            "结合今日收盘价和历史数据给出明日的关键价位参考，帮助提前做好应对准备。"
        )

    # 构建大盘风向标文本
    if index_data:
        index_sections = []
        for ts_code, data in index_data.items():
            name = data.get("name", ts_code)
            records = data.get("records", [])
            if records:
                lines = "\n".join(
                    f"  {r['date']}: 收{r['close']} 高{r['high']} 低{r['low']} 成交额{r['amount_yi']}亿"
                    for r in records
                )
                index_sections.append(f"{name}（{ts_code}）：\n{lines}")
            else:
                index_sections.append(f"{name}（{ts_code}）：暂无数据（请先运行 fetch_history.py）")
        index_text = "\n\n".join(index_sections)
    else:
        index_text = "暂无数据（请先运行 fetch_history.py 拉取指数数据）"

    return f"""{mode_context}

【当前股票信息】
股票代码：{result['code']}
股票名称：{result['name']}
当日价格：{result['current_price']}（今日高:{result['high']} 低:{result['low']}）
VWAP均价：{result['avg_price']}
静态8848上轨：{result['upper_line']}
静态8848下轨：{result['lower_line']}
持仓状态：{"持仓中，成本价 " + str(result['cost_price']) if holding else "未持仓"}
阶段高点：{result['stage_high'] if result['stage_high'] > 0 else "未设置"}
阶段低点：{result['stage_low'] if result['stage_low'] > 0 else "未设置"}
{fib_text}
20日高点：{result['n20_high']}  20日低点：{result['n20_low']}
60日高点：{result['n60_high']}  60日低点：{result['n60_low']}
静态规则信号参考：{result['signal']}

【近期历史数据（最近60日，按日期倒序）】
{history_text if history_text else "暂无历史数据"}

【大盘风向标（近20日，按日期倒序）】
{index_text}

【分析要求】
请按以下结构输出，每个部分控制在3-5句话以内，简洁直接：

0. **对静态信号的批判性评估**：输入数据中的“静态8848上下轨”和“斐波那契”是基于固定参数计算的，它们可能不适用于所有股票和市场情况。请你首先结合当前股票的量价关系、波动性等其他因素，判断这些静态信号在当前场景下的**可靠性**。如果认为信号有误或参考价值不大，请明确指出你的不同观点。

1. **大盘阶段判断**（参考 Skill 01）：根据三大指数的量能趋势和价格走势，当前大盘处于哪个阶段（3-1/3-2/3-3/3-4/3-5）？对个股操作有何影响？

2. **股票类型判断**（参考 Skill 11）：这只股票属于哪种类型（A/B/C/D/E类及子类），判断依据是什么？

3. **量价状态**（参考 Skill 05）：从历史数据看，近期量能趋势如何？是放量还是缩量？结合均价走势推断资金动向。

4. **{op3_label}**（参考 Skill 03 + 对应类型操作规则）：在完成上述评估后，再结合静态规则信号参考（{result['signal']}），{op3_focus}
{extra_instruction}

5. **风险提示**（参考 Skill 07）：当前主要风险点是什么？有哪些需要特别注意的信号？

注意：分析基于当前有限数据，仅供参考，不构成投资建议。
{"" if not user_hint else chr(10) + "【用户补充说明】" + chr(10) + user_hint.strip()}"""


# ── Tool Use 工具层 ──────────────────────────────────────────────────────────

# 工具安全上限：防止 LLM 无限循环调用工具
MAX_TOOL_ROUNDS = 5

# 统一工具描述（语义层），由此生成各 provider 的格式
TOOL_DEFINITIONS = [
    {
        "name": "get_intraday_lines",
        "description": (
            "获取个股今日分时数据，包含白线（每分钟收盘价）和黄线（分时均价，即累计成交额/累计成交量）。"
            "用于判断日内价格趋势、均价支撑/压力位、做T时机。每5分钟一个采样点。"
            "仅在交易时段（09:30-15:00）有数据，非交易时段返回空。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH 或 000001.SZ"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_index_intraday",
        "description": (
            "获取上证指数、深证成指、创业板指三大指数今日分时数据，"
            "包含白线（当前价）、黄线（分时均价/资金成本重心）和各时段量能节奏（增量成交量及相对均量倍数）。"
            "用于判断大盘盘中趋势：价格持续高于黄线为多头主导，低于黄线为空头主导；"
            "放量上涨/缩量下跌为强势特征，放量下跌/缩量反弹需警惕。"
            "配合 Skill 01 大盘阶段判断，辅助决策个股日内操作节奏。"
            "每3分钟更新一次，仅交易时段有数据。"
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_moneyflow",
        "description": (
            "获取个股最近5个交易日的主力资金流向数据，包含主力净流入、超大单、大单、中单、小单的净流入金额和占比。"
            "用于判断主力资金动向、散户情绪，配合量价分析（Skill 05）和市场参与者分析（Skill 08）。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_top_list",
        "description": (
            "获取个股最近10个交易日内上榜龙虎榜的记录，包含上榜原因、买入/卖出金额、机构/游资席位信息。"
            "用于判断市场参与者行为（Skill 08），识别是否有机构/游资介入。"
            "若近期未上榜则返回空列表。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_daily_basic",
        "description": (
            "获取个股最新一日的基本面指标，包含市盈率(PE)、市净率(PB)、换手率、总市值、流通市值、量比等。"
            "用于股票类型判断（Skill 11）和估值参考，辅助判断是大盘股/中小盘/题材股。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_technical_indicators",
        "description": (
            "基于个股近60日历史K线，计算并返回技术指标：BOLL布林带（上轨/中轨/下轨）、"
            "5日/10日/20日均线（MA5/MA10/MA20），以及最近20日的收盘价序列。"
            "用于判断当前价格所处位置（BOLL位置）、趋势方向（均线多头/空头排列）、"
            "支撑压力位（Skill 02仓位管理、Skill 03买卖信号、Skill 09长线持仓）。"
            "数据来自本地 daily_records 表，无需 Tushare 调用。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH 或 000001.SZ"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_margin_data",
        "description": (
            "获取近10个交易日沪深两市融资融券余额及变化趋势。"
            "融资余额持续增加说明散户情绪亢奋，是风险信号；"
            "融资余额持续萎缩+低位企稳是磨底信号。"
            "用于行情阶段判断（Skill 01）、风险管理（Skill 07）、散户情绪分析（Skill 08）。"
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_sector_flow",
        "description": (
            "获取A股主要板块指数（科技/资源/消费/金融/医药等）近5日的涨跌幅和成交额，"
            "辅助判断板块轮动方向和当前主线题材。"
            "用于板块轮动分析（Skill 04）和市场参与者判断（Skill 08）。"
        ),
        "parameters": {
            "trade_date": {"type": "string", "description": "查询日期，格式 YYYYMMDD，默认取最近交易日"},
        },
        "required": [],
    },
    {
        "name": "get_futures_positions",
        "description": (
            "获取沪深300股指期货（IF）主力合约近5日的多空持仓量及变化趋势。"
            "主力多单增加说明机构看多；空单增加是看空信号。"
            "用于行情阶段判断（Skill 01）和量价分析（Skill 05）。"
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_disclosure_calendar",
        "description": (
            "查询个股最近或即将发布的财报披露日期（年报/半年报/季报）。"
            "财报发布前后是高风险窗口，需要控制仓位。"
            "用于仓位管理（Skill 02）和风险管理（Skill 07）。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_share_reduction",
        "description": (
            "查询个股近90天内大股东/高管的增持或减持记录。"
            "大股东减持是明确的卖出信号；增持则是看多信号。"
            "用于买卖信号判断（Skill 03）和风险管理（Skill 07）。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_etf_flow",
        "description": (
            "获取主要宽基ETF（沪深300ETF、科创50ETF、创业板ETF等）近5日的资金净流入情况，"
            "判断国家队（GJD）是否在通过ETF入市托底或加仓。"
            "用于市场参与者分析（Skill 08）和行情阶段判断（Skill 01）。"
        ),
        "parameters": {},
        "required": [],
    },
    {
        "name": "get_chip_distribution",
        "description": (
            "获取个股最近5日的筹码成本分布和胜率数据。"
            "包含5/15/50/85/95分位成本价、加权平均成本(weight_avg)和胜率(winner_rate，当前价格以下的筹码占比%)。"
            "胜率高（>80%）说明大多数持仓者盈利，上方套牢盘压力小；"
            "胜率低（<30%）说明大量筹码被套，反弹时抛压重。"
            "用于筹码结构分析、支撑压力位判断（Skill 02/03/05）。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
    {
        "name": "get_technical_factors",
        "description": (
            "获取个股最近3个交易日的技术指标：MACD柱值(macd)、RSI6/RSI12、KDJ的K值/D值、"
            "布林带上轨/中轨/下轨(boll_upper/mid/lower)。"
            "用于判断动量强弱（MACD/RSI）、超买超卖（KDJ/RSI）、价格所处通道位置（BOLL），"
            "辅助买卖点判断（Skill 03）和趋势确认（Skill 05）。"
            "所有指标均为前复权数据。"
        ),
        "parameters": {
            "ts_code": {"type": "string", "description": "股票代码，标准格式如 600519.SH"},
        },
        "required": ["ts_code"],
    },
]


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _is_trading_time() -> bool:
    """判断当前是否在 A 股交易时段（工作日 09:30–15:00）。"""
    import datetime
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    t = now.time()
    start = datetime.time(9, 30)
    middle_end = datetime.time(11, 30)
    middle_start = datetime.time(13, 0)
    end = datetime.time(15, 0)
    is_morning = start <= t <= middle_end
    is_afternoon = middle_start <= t <= end
    return is_morning or is_afternoon


def _save_intraday_snapshot(code: str, today: str, now_hhmm: str,
                             price: float, open_: float, high: float, low: float,
                             cum_vol: float, cum_amount_qianyuan: float) -> None:
    """将单只股票/指数的实时行情计算增量后写入 intraday_snapshots。

    cum_amount_qianyuan：当日累计成交额，单位千元。
    vol/amount 存储相邻两次快照之间的增量，而非累计值。
    """
    if price == 0 or cum_vol == 0:
        return

    # 读取今日已存增量之和，作为上次累计值
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
        prev_cum_vol = 0.0
        prev_cum_amount = 0.0

    delta_vol    = max(cum_vol            - prev_cum_vol,    0.0)
    delta_amount = max(cum_amount_qianyuan - prev_cum_amount, 0.0)

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO intraday_snapshots
                    (code, date, time, price, open, high, low, vol, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, today, now_hhmm, price, open_, high, low, delta_vol, delta_amount),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("intraday_fetch: %s %s price=%.3f delta_vol=%.0f", code, now_hhmm, price, delta_vol)
    except Exception as e:
        logger.error("intraday_fetch: 写入失败 code=%s %s", code, e)


# 三大指数：(get_realtime_quotes 查询代码, 存库用的 code)
_INDEX_RT_CODES = [
    ("sh000001", "000001.SH"),   # 上证指数
    ("399001",   "399001.SZ"),   # 深证成指
    ("399006",   "399006.SZ"),   # 创业板指
]


def _fetch_and_save_intraday_snapshots() -> None:
    """用 ts.get_realtime_quotes 逐只抓取自选股 + 三大指数实时行情，写入 intraday_snapshots。

    get_realtime_quotes 无严格调用次数限制，个股和指数均支持。
    vol/amount 存储相邻两次快照之间的增量，方便分时量能图展示各时段节奏。

    字段说明（get_realtime_quotes）：
        price  → 当前最新价
        volume → 当日累计成交量（股）
        amount → 当日累计成交额（元），存库时除以 1000 换算为千元
    """
    if not COMMON_STOCKS:
        return

    import datetime
    today = _today_str()
    now_hhmm = datetime.datetime.now().strftime("%H:%M")

    # 清理非今日旧数据，保持表轻量
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM intraday_snapshots WHERE date != ?", (today,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("intraday_fetch: 清理旧数据失败 %s", e)

    # ── 个股快照 ──────────────────────────────────────────────────────────────
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
            cum_vol    = float(df.loc[0, "volume"])          # 当日累计成交量（股）
            cum_amount = float(df.loc[0, "amount"]) / 1000.0 # 元 → 千元
        except Exception as e:
            logger.warning("intraday_fetch: 解析行情失败 code=%s %s", code, e)
            continue

        _save_intraday_snapshot(code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)

    # ── 三大指数快照 ──────────────────────────────────────────────────────────
    for rt_code, store_code in _INDEX_RT_CODES:
        try:
            df = ts.get_realtime_quotes(rt_code)
        except Exception as e:
            logger.warning("intraday_fetch: 指数 get_realtime_quotes 失败 code=%s %s", rt_code, e)
            continue

        if df is None or df.empty:
            continue

        try:
            price      = float(df.loc[0, "price"])
            high       = float(df.loc[0, "high"])
            low        = float(df.loc[0, "low"])
            open_      = float(df.loc[0, "open"])
            cum_vol    = float(df.loc[0, "volume"])          # 当日累计成交量（股/手，指数单位不同但增量逻辑一致）
            cum_amount = float(df.loc[0, "amount"]) / 1000.0 # 元 → 千元
        except Exception as e:
            logger.warning("intraday_fetch: 解析指数行情失败 code=%s %s", rt_code, e)
            continue

        _save_intraday_snapshot(store_code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)


def _intraday_bg_loop() -> None:
    """后台线程：每 3 分钟在交易时段抓取一次分时快照。"""
    logger.info("intraday_bg_loop: 后台线程已启动")
    while True:
        if _is_trading_time():
            logger.info("intraday_bg_loop: 开始抓取分时快照")
            _fetch_and_save_intraday_snapshots()
        time.sleep(1 * 60)


def _get_intraday_points(code: str) -> list:
    """从 intraday_snapshots 表读取当日分时数据，返回带黄白线的 points 列表。
    供页面渲染分时图和 AI 工具调用共用。
    返回格式：[{"time": "09:35", "price": 41.27, "avg": 41.26}, ...]
    无数据时返回空列表。
    """
    today = _today_str()
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT time, price, vol, amount FROM intraday_snapshots "
                "WHERE code = ? AND date = ? ORDER BY time ASC",
                (code, today),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error("_get_intraday_points: 读取失败 %s", e)
        return []

    cum_vol = 0.0
    cum_amount = 0.0
    points = []
    for t, price, vol, amount in rows:
        cum_vol    += vol    or 0.0
        cum_amount += amount or 0.0
        # amount 存库单位为千元，vol 单位为股；均价 = 千元*1000 / 股 = 元/股
        avg = round(cum_amount * 1000 / cum_vol, 4) if cum_vol > 0 else None
        points.append({"time": t, "price": round(price, 4), "avg": avg, "vol": vol or 0.0})
    return points


def _tool_get_intraday_lines(ts_code: str) -> dict:
    """从 intraday_snapshots 表读取当日分时快照，重建黄白线序列。
    数据由后台线程每 30 分钟通过 rt_min 接口抓取写入。
    """
    code = ts_code.split(".")[0]
    points = _get_intraday_points(code)
    if not points:
        return {"error": "暂无分时数据，后台任务尚未抓取（交易时段每30分钟更新一次）"}
    latest = points[-1]
    return {
        "ts_code": ts_code,
        "date": _today_str(),
        "latest_price": latest["price"],
        "latest_avg": latest["avg"],
        "points": points,
        "note": "price=白线（分钟收盘价），avg=黄线（分时均价），每30分钟更新一次",
    }


def _tool_get_index_intraday() -> dict:
    """读取三大指数今日分时快照，返回黄白线序列 + 量能节奏，供 AI 判断大盘盘中趋势。

    数据由后台线程每3分钟写入 intraday_snapshots，code 格式为 000001.SH 等。
    黄线（avg）= 累计成交额 / 累计成交量，反映当日资金成本重心。
    量能节奏（vol）= 每个时间片的增量成交量，用于判断各时段买卖力度。
    """
    INDEX_STORE_CODES = [
        ("000001.SH", "上证指数"),
        ("399001.SZ", "深证成指"),
        ("399006.SZ", "创业板指"),
    ]
    today = _today_str()
    result = {}

    for store_code, name in INDEX_STORE_CODES:
        points = _get_intraday_points(store_code)
        if not points:
            result[store_code] = {"name": name, "error": "暂无分时数据"}
            continue

        latest = points[-1]

        # 量能节奏：计算各时段增量成交量相对于全日均量的比值，判断放量/缩量时段
        vols = [p["vol"] for p in points if p["vol"] > 0]
        avg_vol = sum(vols) / len(vols) if vols else 0

        # 标注每个点的量能状态
        annotated = []
        for p in points:
            vol_ratio = round(p["vol"] / avg_vol, 2) if avg_vol > 0 else None
            annotated.append({
                "time":      p["time"],
                "price":     p["price"],    # 白线：当前价
                "avg":       p["avg"],      # 黄线：分时均价
                "vol":       p["vol"],      # 增量成交量
                "vol_ratio": vol_ratio,     # 相对均量倍数，>1.5 为放量，<0.5 为缩量
            })

        result[store_code] = {
            "name":         name,
            "date":         today,
            "latest_price": latest["price"],
            "latest_avg":   latest["avg"],
            "price_vs_avg": "价格高于均线" if latest["price"] and latest["avg"] and latest["price"] > latest["avg"] else "价格低于均线",
            "points":       annotated,
        }

    return {
        "indexes": result,
        "note": (
            "price=白线（当前价），avg=黄线（分时均价/资金成本重心）；"
            "价格持续高于黄线为多头主导，低于黄线为空头主导；"
            "vol_ratio>1.5为放量时段，<0.5为缩量时段；"
            "每3分钟更新一次。"
        ),
    }


def _tool_get_moneyflow(ts_code: str, trade_date: str = "") -> dict:
    """获取最近5日主力资金流向。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        df = pro.moneyflow(ts_code=ts_code, limit=5)
    except Exception as e:
        return {"error": f"moneyflow 调用失败: {e}"}
    if df is None or df.empty:
        return {"error": "暂无资金流向数据"}

    fields = ["trade_date", "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
              "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
              "net_mf_vol", "net_mf_amount"]
    available = [f for f in fields if f in df.columns]
    records = df[available].sort_values("trade_date", ascending=False).head(5).to_dict("records")
    return {"ts_code": ts_code, "records": records,
            "note": "elg=超大单，lg=大单，net_mf=主力净流入，amount单位万元"}


def _tool_get_top_list(ts_code: str, trade_date: str = "") -> dict:
    """获取近10个交易日龙虎榜记录。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        # 取最近10个交易日范围
        from datetime import datetime, timedelta
        end = datetime.today()
        start = end - timedelta(days=14)
        df = pro.top_list(
            ts_code=ts_code,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"top_list 调用失败: {e}"}
    if df is None or df.empty:
        return {"ts_code": ts_code, "records": [], "note": "近期未上榜龙虎榜"}

    fields = ["trade_date", "reason", "buy_amount", "sell_amount", "net_amount", "turnover_rate"]
    available = [f for f in fields if f in df.columns]
    records = df[available].sort_values("trade_date", ascending=False).head(10).to_dict("records")
    return {"ts_code": ts_code, "records": records, "note": "amount单位万元"}


def _tool_get_daily_basic(ts_code: str, trade_date: str = "") -> dict:
    """获取最新一日基本面指标。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        df = pro.daily_basic(ts_code=ts_code, limit=1)
    except Exception as e:
        return {"error": f"daily_basic 调用失败: {e}"}
    if df is None or df.empty:
        return {"error": "暂无基本面数据"}

    fields = ["trade_date", "pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm",
              "total_mv", "circ_mv", "turnover_rate", "turnover_rate_f", "volume_ratio"]
    available = [f for f in fields if f in df.columns]
    record = df[available].iloc[0].to_dict()
    return {"ts_code": ts_code, "data": record,
            "note": "pe=市盈率，pb=市净率，total_mv=总市值(万元)，turnover_rate=换手率(%)"}


def _calc_macd(code: str) -> dict | None:
    """从 daily_records 读取近90日收盘价，计算 MACD(12,26,9) 并检测顶/底背离。
    返回 dict 或 None（数据不足时）。
    """
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date ASC LIMIT 90",
                (q,),
            )
            rows = cur.fetchall()
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

    # EMA 计算
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
    hist  = [dif[i] - dea[i] for i in range(n)]  # MACD 柱

    # 最新值
    latest = {
        "date": dates[-1],
        "dif":  round(dif[-1],  4),
        "dea":  round(dea[-1],  4),
        "hist": round(hist[-1], 4),
    }

    # ── 背离检测（在近 40 根 K 线内寻找相邻极值点）──────────────
    WIN   = min(40, n)
    seg_c = closes[-WIN:]
    seg_h = hist[-WIN:]
    seg_d = dates[-WIN:]

    def find_peaks(arr, mode="high"):
        """找局部极值：前后各2根满足条件。"""
        pts = []
        for i in range(2, len(arr) - 2):
            if mode == "high":
                if arr[i] > arr[i-1] and arr[i] > arr[i-2] and \
                   arr[i] > arr[i+1] and arr[i] > arr[i+2]:
                    pts.append(i)
            else:
                if arr[i] < arr[i-1] and arr[i] < arr[i-2] and \
                   arr[i] < arr[i+1] and arr[i] < arr[i+2]:
                    pts.append(i)
        return pts

    divergence = None  # None | "top" | "bottom"
    div_detail = ""

    # 顶背离：取最近两个价格高点，价格新高但 MACD 柱未新高
    peak_idx = find_peaks(seg_c, "high")
    if len(peak_idx) >= 2:
        i1, i2 = peak_idx[-2], peak_idx[-1]
        p1_c, p2_c = seg_c[i1], seg_c[i2]
        p1_h, p2_h = seg_h[i1], seg_h[i2]
        if p2_c > p1_c and p2_h < p1_h:
            divergence = "top"
            div_detail = (
                f"价格高点 {seg_d[i1]}({p1_c:.2f}) → {seg_d[i2]}({p2_c:.2f}) 创新高，"
                f"但 MACD 柱 {p1_h:.4f} → {p2_h:.4f} 未同步新高，"
                f"上涨动能衰竭，警惕回调。"
            )

    # 底背离：取最近两个价格低点，价格新低但 MACD 柱未新低
    if divergence is None:
        trough_idx = find_peaks(seg_c, "low")
        if len(trough_idx) >= 2:
            i1, i2 = trough_idx[-2], trough_idx[-1]
            t1_c, t2_c = seg_c[i1], seg_c[i2]
            t1_h, t2_h = seg_h[i1], seg_h[i2]
            if t2_c < t1_c and t2_h > t1_h:
                divergence = "bottom"
                div_detail = (
                    f"价格低点 {seg_d[i1]}({t1_c:.2f}) → {seg_d[i2]}({t2_c:.2f}) 创新低，"
                    f"但 MACD 柱 {t1_h:.4f} → {t2_h:.4f} 未同步新低，"
                    f"下跌动能衰竭，关注反弹机会。"
                )

    # 金叉/死叉（最近一次）
    cross = None
    for i in range(n - 1, max(n - 10, 0), -1):
        if hist[i] > 0 and hist[i - 1] <= 0:
            cross = {"type": "golden", "date": dates[i], "label": "金叉（DIF上穿DEA）"}
            break
        if hist[i] < 0 and hist[i - 1] >= 0:
            cross = {"type": "dead", "date": dates[i], "label": "死叉（DIF下穿DEA）"}
            break

    return {
        "latest":     latest,
        "divergence": divergence,   # None | "top" | "bottom"
        "div_detail": div_detail,
        "cross":      cross,
        "above_zero": dif[-1] > 0,  # DIF 在零轴上方
        # 近20日序列，供 MACD 副图渲染
        "series": {
            "dates": dates[-20:],
            "dif":   [round(v, 4) for v in dif[-20:]],
            "dea":   [round(v, 4) for v in dea[-20:]],
            "hist":  [round(v, 4) for v in hist[-20:]],
        },
    }


def _calc_yidong(code: str, current_price: float) -> dict | None:
    """
    计算异动线：取近31个交易日的收盘价，用第31条（即30个交易日前）作为基准，
    异动线 = 基准收盘价 × 3.0（即30日内涨幅累计达到200%）。
    返回 dict 或 None（数据不足时）：
      - base_date:   基准日期
      - base_close:  基准收盘价
      - yidong_line: 异动线价格
      - pct_to_line: 当前价距异动线的百分比（负=未到，正=已超过）
      - alert:       True/False，当前价 >= 异动线 × 0.9 时触发
      - triggered:   True/False，当前价 >= 异动线（已触发异动）
    """
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date DESC LIMIT 31",
                (q,),
            )
            rows = cur.fetchall()
            if rows:
                break
        conn.close()
    except Exception:
        return None

    if not rows or len(rows) < 31:
        return None

    # rows 是降序，rows[-1] 是最早的那条（30个交易日前）
    base_date, base_close = rows[-1][0], float(rows[-1][1])
    if base_close <= 0:
        return None

    yidong_line = round(base_close * 3.0, 4)
    pct_to_line = round((current_price - yidong_line) / yidong_line * 100, 2)
    alert       = current_price >= yidong_line * 0.9
    triggered   = current_price >= yidong_line

    return {
        "base_date":   base_date,
        "base_close":  round(base_close, 4),
        "yidong_line": yidong_line,
        "pct_to_line": pct_to_line,   # 负值=还差多少%，正值=已超过多少%
        "alert":       alert,
        "triggered":   triggered,
    }


def _calc_boll(code: str) -> dict:
    """从 daily_records 读取近60日收盘价，计算 BOLL(20,2) 和 MA5/10/20。
    返回 dict，key: upper/mid/lower/ma5/ma10/ma20/position/closes（最近20日）。
    数据不足或出错时返回 None。
    """
    ts_code = code if "." in code else None
    short_code = code.split(".")[0] if "." in code else code
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q_code in ([ts_code, short_code] if ts_code else [short_code]):
            if q_code is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT 60",
                (q_code,),
            )
            rows = cur.fetchall()
            if rows:
                break
        conn.close()
    except Exception:
        return None

    if not rows or len(rows) < 5:
        return None

    rows = list(reversed(rows))
    closes = [r[1] for r in rows if r[1] is not None]
    n = len(closes)

    def sma(data, period):
        return round(sum(data[-period:]) / period, 4) if len(data) >= period else None

    def boll_calc(data, period=20, k=2.0):
        if len(data) < period:
            return None, None, None
        w = data[-period:]
        mid = sum(w) / period
        std = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
        return round(mid + k * std, 4), round(mid, 4), round(mid - k * std, 4)

    upper, mid, lower = boll_calc(closes)
    ma5  = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    latest = closes[-1]

    if upper and lower and mid:
        if latest >= upper:
            position = "上轨附近（超买，注意压力）"
            advice_class = "danger"
        elif latest <= lower:
            position = "下轨附近（超卖，关注支撑）"
            advice_class = "success"
        elif latest > mid:
            position = "中轨↑上轨（强势区间）"
            advice_class = "warning"
        else:
            position = "下轨↑中轨（弱势区间）"
            advice_class = "secondary"
    else:
        position = "数据不足"
        advice_class = "secondary"

    # 近20日滚动均线序列（供 K 线图叠加均线使用）
    recent_rows = rows[max(0, n - 20):]
    all_closes  = closes  # 全量 closes，用于计算滚动均线
    ma_series_dates = []
    ma_series_ma5   = []
    ma_series_ma10  = []
    ma_series_ma20  = []
    for i in range(max(0, n - 20), n):
        ma_series_dates.append(rows[i][0])
        offset = i + 1  # 截止到第 i 条（含）的数据长度
        ma_series_ma5.append(round(sum(all_closes[max(0, offset - 5):offset]) / min(5, offset), 4))
        ma_series_ma10.append(round(sum(all_closes[max(0, offset - 10):offset]) / min(10, offset), 4))
        ma_series_ma20.append(round(sum(all_closes[max(0, offset - 20):offset]) / min(20, offset), 4))

    return {
        "upper": upper,
        "mid":   mid,
        "lower": lower,
        "ma5":   ma5,
        "ma10":  ma10,
        "ma20":  ma20,
        "position":     position,
        "advice_class": advice_class,
        "data_points":  n,
        "recent_closes": [{"date": rows[i][0], "close": rows[i][1]} for i in range(max(0, n - 20), n)],
        "ma_series": {
            "dates": ma_series_dates,
            "ma5":   ma_series_ma5,
            "ma10":  ma_series_ma10,
            "ma20":  ma_series_ma20,
        },
    }


def _tool_get_technical_indicators(ts_code: str) -> dict:
    """基于 daily_records 历史数据计算 BOLL 布林带和均线指标。"""
    boll_data = _calc_boll(ts_code)
    if boll_data is None:
        return {"error": "暂无历史K线数据，请先运行 fetch_history.py 拉取数据"}

    closes    = [r["close"] for r in boll_data["recent_closes"]]
    n         = boll_data["data_points"]
    boll_upper = boll_data["upper"]
    boll_mid   = boll_data["mid"]
    boll_lower = boll_data["lower"]
    ma5        = boll_data["ma5"]
    ma10       = boll_data["ma10"]
    ma20       = boll_data["ma20"]

    latest_close = closes[-1] if closes else None
    latest_date  = boll_data["recent_closes"][-1]["date"] if boll_data["recent_closes"] else ""

    # 均线多空排列判断
    ma_trend = "数据不足"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_trend = "多头排列（MA5>MA10>MA20，趋势向上）"
        elif ma5 < ma10 < ma20:
            ma_trend = "空头排列（MA5<MA10<MA20，趋势向下）"
        else:
            ma_trend = "均线纠缠（无明确趋势）"

    boll_position = boll_data["position"]
    # 补充完整描述供 AI 使用
    if boll_upper and boll_lower and boll_mid and latest_close:
        if latest_close >= boll_upper:
            boll_position = "价格在BOLL上轨附近或以上（超买区，注意压力）"
        elif latest_close <= boll_lower:
            boll_position = "价格在BOLL下轨附近或以下（超卖区，关注支撑）"
        elif latest_close > boll_mid:
            boll_position = "价格在BOLL中轨与上轨之间（强势区间）"
        else:
            boll_position = "价格在BOLL中轨与下轨之间（弱势区间）"

    latest_close = closes[-1]
    latest_date  = rows[-1][0]

    # 均线多空排列判断
    ma_trend = "数据不足"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_trend = "多头排列（MA5>MA10>MA20，趋势向上）"
        elif ma5 < ma10 < ma20:
            ma_trend = "空头排列（MA5<MA10<MA20，趋势向下）"
        else:
            ma_trend = "均线纠缠（无明确趋势）"

    return {
        "ts_code": ts_code,
        "latest_date": latest_date,
        "latest_close": latest_close,
        "data_points": n,
        "ma": {
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "trend": ma_trend,
        },
        "boll": {
            "upper": boll_upper,
            "mid": boll_mid,
            "lower": boll_lower,
            "position": boll_position,
        },
        "recent_closes": boll_data["recent_closes"],
        "note": "BOLL参数：20日，2倍标准差；均线：简单移动平均",
    }


def _tool_get_margin_data() -> dict:
    """获取近10个交易日沪深两市融资融券余额。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取融资融券数据"}
    try:
        from datetime import datetime, timedelta
        end = datetime.today()
        start = end - timedelta(days=20)  # 多取几天以覆盖10个交易日
        df = pro.margin(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"margin 调用失败: {e}"}

    if df is None or df.empty:
        return {"error": "暂无融资融券数据"}

    # 按日期汇总（沪深合计）
    try:
        agg = (
            df.groupby("trade_date")[["rzye", "rqye", "rzrqye"]]
            .sum()
            .reset_index()
            .sort_values("trade_date", ascending=False)
            .head(10)
        )
        records = agg.to_dict("records")
        # 计算环比变化
        for i in range(len(records) - 1):
            prev_rzye = records[i + 1].get("rzye", 0)
            curr_rzye = records[i].get("rzye", 0)
            if prev_rzye and prev_rzye != 0:
                records[i]["rzye_chg_pct"] = round((curr_rzye - prev_rzye) / prev_rzye * 100, 2)
            else:
                records[i]["rzye_chg_pct"] = None
        if records:
            records[-1]["rzye_chg_pct"] = None

        # 趋势判断（最近5日）
        recent5 = [r.get("rzye", 0) for r in records[:5] if r.get("rzye")]
        trend = "数据不足"
        if len(recent5) >= 3:
            if recent5[0] > recent5[1] > recent5[2]:
                trend = "融资余额连续上升（散户情绪偏热，注意风险）"
            elif recent5[0] < recent5[1] < recent5[2]:
                trend = "融资余额连续下降（散户情绪收缩，关注磨底信号）"
            else:
                trend = "融资余额震荡（情绪中性）"

        return {
            "records": records,
            "trend": trend,
            "note": "rzye=融资余额(元)，rqye=融券余额(元)，rzrqye=两融合计余额(元)，chg_pct=较前日环比变化%",
        }
    except Exception as e:
        return {"error": f"数据处理失败: {e}"}


# 主要板块指数代码（申万一级行业代表性指数）
SECTOR_INDEX_CODES = {
    "801080.SI": "电子",
    "801010.SI": "农林牧渔",
    "801750.SI": "计算机",
    "801760.SI": "传媒",
    "801770.SI": "通信",
    "801050.SI": "有色金属",
    "801020.SI": "采掘",
    "801030.SI": "化工",
    "801110.SI": "家用电器",
    "801120.SI": "食品饮料",
    "801150.SI": "医药生物",
    "801160.SI": "公用事业",
    "801170.SI": "交通运输",
    "801180.SI": "房地产",
    "801190.SI": "银行",
    "801200.SI": "非银金融",
    "801210.SI": "综合",
    "801230.SI": "综合金融",
    "801710.SI": "建筑材料",
    "801720.SI": "建筑装饰",
    "801730.SI": "电气设备",
    "801740.SI": "国防军工",
    "801880.SI": "汽车",
    "801890.SI": "机械设备",
}


def _tool_get_sector_flow(trade_date: str = "") -> dict:
    """获取主要板块指数近5日涨跌幅，辅助判断板块轮动方向。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取板块数据"}

    from datetime import datetime, timedelta
    if not trade_date:
        trade_date = datetime.today().strftime("%Y%m%d")

    # 取近5个交易日的数据
    start_date = (datetime.today() - timedelta(days=10)).strftime("%Y%m%d")

    sector_results = []
    try:
        # 使用 index_daily 获取板块指数行情
        ts_codes = list(SECTOR_INDEX_CODES.keys())
        # 批量查询，每次最多查几个以避免超时
        all_rows = []
        for ts_code in ts_codes:
            try:
                df = pro.index_daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=trade_date,
                    limit=5,
                )
                if df is not None and not df.empty:
                    df["sector_name"] = SECTOR_INDEX_CODES[ts_code]
                    all_rows.append(df)
            except Exception:
                continue  # 单个板块失败不影响整体

        if not all_rows:
            return {"error": "未能获取任何板块数据，可能需要更高 Tushare 权限"}

        import pandas as pd
        combined = pd.concat(all_rows, ignore_index=True)

        # 取最新一日各板块涨跌幅，排序
        latest_date = combined["trade_date"].max()
        latest = combined[combined["trade_date"] == latest_date].copy()
        latest = latest.sort_values("pct_chg", ascending=False)

        top5_up = latest.head(5)[["sector_name", "ts_code", "close", "pct_chg", "amount"]].to_dict("records")
        top5_down = latest.tail(5)[["sector_name", "ts_code", "close", "pct_chg", "amount"]].to_dict("records")

        # 近5日累计涨跌幅（用于判断持续性）
        sector_5d = []
        for ts_code, name in SECTOR_INDEX_CODES.items():
            sub = combined[combined["ts_code"] == ts_code].sort_values("trade_date")
            if len(sub) >= 2:
                chg_5d = round(
                    (sub.iloc[-1]["close"] - sub.iloc[0]["close"]) / sub.iloc[0]["close"] * 100, 2
                )
                sector_5d.append({"sector_name": name, "ts_code": ts_code, "chg_5d_pct": chg_5d})

        sector_5d.sort(key=lambda x: x["chg_5d_pct"], reverse=True)

        return {
            "latest_date": latest_date,
            "top5_gainers_today": top5_up,
            "top5_losers_today": top5_down,
            "sector_5d_ranking": sector_5d,
            "note": "pct_chg=当日涨跌幅(%)，chg_5d_pct=近5日累计涨跌幅(%)，amount=成交额(千元)",
        }
    except Exception as e:
        return {"error": f"板块数据处理失败: {e}"}


def _tool_get_futures_positions() -> dict:
    """获取沪深300股指期货（IF）主力合约近5日多空持仓。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取期指数据"}
    from datetime import datetime, timedelta
    end = datetime.today()
    start = end - timedelta(days=14)
    try:
        # IF 为沪深300股指期货，取主力合约持仓
        df = pro.fut_holding(
            symbol="IF",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"fut_holding 调用失败: {e}"}

    if df is None or df.empty:
        return {"error": "暂无期指持仓数据，可能需要更高 Tushare 权限"}

    try:
        # 按日期汇总多空总持仓
        agg = (
            df.groupby("trade_date")[["long_hld", "short_hld"]]
            .sum()
            .reset_index()
            .sort_values("trade_date", ascending=False)
            .head(5)
        )
        records = agg.to_dict("records")

        # 计算净多头（多单-空单）及趋势
        for r in records:
            r["net_long"] = round(r.get("long_hld", 0) - r.get("short_hld", 0), 0)

        # 趋势判断
        if len(records) >= 2:
            net_latest = records[0].get("net_long", 0)
            net_prev = records[1].get("net_long", 0)
            if net_latest > net_prev:
                trend = "净多头增加（机构偏多，看涨信号）"
            elif net_latest < net_prev:
                trend = "净多头减少（机构偏空，注意风险）"
            else:
                trend = "持仓变化不明显"
        else:
            trend = "数据不足"

        return {
            "symbol": "IF（沪深300股指期货）",
            "records": records,
            "trend": trend,
            "note": "long_hld=多头持仓量，short_hld=空头持仓量，net_long=净多头（多-空）",
        }
    except Exception as e:
        return {"error": f"数据处理失败: {e}"}


def _tool_get_disclosure_calendar(ts_code: str) -> dict:
    """查询个股近期财报披露日期。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取财报日历"}
    from datetime import datetime, timedelta
    today = datetime.today()
    # 查询前后90天的财报披露计划
    start = (today - timedelta(days=30)).strftime("%Y%m%d")
    end = (today + timedelta(days=90)).strftime("%Y%m%d")
    try:
        df = pro.disclosure_date(
            ts_code=ts_code,
            start_date=start,
            end_date=end,
        )
    except Exception as e:
        return {"error": f"disclosure_date 调用失败: {e}"}

    if df is None or df.empty:
        return {"ts_code": ts_code, "records": [], "note": "未查到近期财报披露计划"}

    fields = ["ann_date", "end_date", "pre_date", "actual_date", "modify_date"]
    available = [f for f in fields if f in df.columns]
    records = df[available].sort_values("end_date", ascending=False).to_dict("records")

    # 找出最近即将发布的财报
    today_str = today.strftime("%Y%m%d")
    upcoming = [r for r in records if r.get("pre_date", "") >= today_str or r.get("actual_date", "") >= today_str]
    warning = None
    if upcoming:
        next_report = upcoming[0]
        pre_date = next_report.get("pre_date") or next_report.get("actual_date", "")
        if pre_date:
            days_left = (datetime.strptime(pre_date, "%Y%m%d") - today).days
            if days_left <= 14:
                warning = f"⚠️ 距下次财报披露仅剩 {days_left} 天（{pre_date}），建议控制仓位"
            else:
                warning = f"下次财报披露预计 {pre_date}，距今 {days_left} 天"

    return {
        "ts_code": ts_code,
        "records": records[:6],
        "upcoming_warning": warning,
        "note": "ann_date=公告日，end_date=报告期，pre_date=预计披露日，actual_date=实际披露日",
    }


def _tool_get_share_reduction(ts_code: str) -> dict:
    """查询个股近90天大股东/高管增减持记录。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取增减持数据"}
    from datetime import datetime, timedelta
    end = datetime.today()
    start = end - timedelta(days=90)
    try:
        df = pro.stk_holdertrade(
            ts_code=ts_code,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"stk_holdertrade 调用失败: {e}"}

    if df is None or df.empty:
        return {"ts_code": ts_code, "records": [], "note": "近90天无大股东增减持记录"}

    fields = ["ann_date", "holder_name", "holder_type", "in_de", "change_vol", "change_ratio",
              "after_share", "after_ratio", "avg_price", "total_share"]
    available = [f for f in fields if f in df.columns]
    records = df[available].sort_values("ann_date", ascending=False).head(10).to_dict("records")

    # 汇总增减持方向
    in_de_col = "in_de" if "in_de" in df.columns else None
    reduction_count = 0
    increase_count = 0
    if in_de_col:
        reduction_count = int((df[in_de_col] == "减持").sum())
        increase_count = int((df[in_de_col] == "增持").sum())

    summary = f"近90天：增持{increase_count}次，减持{reduction_count}次"
    if reduction_count > increase_count:
        signal = "⚠️ 减持次数多于增持，注意大股东出货风险"
    elif increase_count > reduction_count:
        signal = "✅ 增持次数多于减持，大股东看多信号"
    else:
        signal = "增减持持平或无记录"

    return {
        "ts_code": ts_code,
        "summary": summary,
        "signal": signal,
        "records": records,
        "note": "in_de=增持/减持，change_vol=变动股数，change_ratio=变动比例，avg_price=均价",
    }


# 主要宽基/行业ETF代码（用于GJD行为判断）
KEY_ETF_CODES = {
    "510300.SH": "沪深300ETF（华泰柏瑞）",
    "510500.SH": "中证500ETF",
    "588000.SH": "科创50ETF",
    "159915.SZ": "创业板ETF",
    "512010.SH": "医疗ETF",
    "512660.SH": "军工ETF",
    "512480.SH": "半导体ETF",
    "159869.SZ": "游戏ETF",
}


def _tool_get_etf_flow() -> dict:
    """获取主要宽基ETF近5日资金净流入，判断GJD行为。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取ETF数据"}
    from datetime import datetime, timedelta
    end = datetime.today()
    start = end - timedelta(days=10)

    etf_results = []
    try:
        import pandas as pd
        for ts_code, name in KEY_ETF_CODES.items():
            try:
                df = pro.fund_daily(
                    ts_code=ts_code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
                if df is None or df.empty:
                    continue
                df = df.sort_values("trade_date", ascending=False).head(5)
                latest = df.iloc[0]
                # 近5日成交额合计（千元）
                total_amount = df["amount"].sum() if "amount" in df.columns else None
                etf_results.append({
                    "ts_code": ts_code,
                    "name": name,
                    "latest_date": latest.get("trade_date"),
                    "latest_close": round(float(latest.get("close", 0)), 4),
                    "pct_chg": round(float(latest.get("pct_chg", 0)), 2) if "pct_chg" in df.columns else None,
                    "amount_5d_yi": round(float(total_amount) / 100000, 2) if total_amount else None,
                })
            except Exception:
                continue

        if not etf_results:
            return {"error": "未能获取ETF数据，可能需要更高 Tushare 权限"}

        # 按5日成交额排序（成交额放大 = 资金关注度高）
        etf_results.sort(key=lambda x: x.get("amount_5d_yi") or 0, reverse=True)

        # GJD信号判断：沪深300ETF成交额是否异常放大
        hs300_etf = next((e for e in etf_results if e["ts_code"] == "510300.SH"), None)
        gjd_signal = "无明显GJD信号"
        if hs300_etf and hs300_etf.get("amount_5d_yi"):
            if hs300_etf["amount_5d_yi"] > 50:  # 5日合计超50亿
                gjd_signal = f"⚠️ 沪深300ETF近5日成交额合计{hs300_etf['amount_5d_yi']}亿，资金关注度较高，可能有GJD介入"
            else:
                gjd_signal = f"沪深300ETF近5日成交额合计{hs300_etf['amount_5d_yi']}亿，未见异常放量"

        return {
            "etf_list": etf_results,
            "gjd_signal": gjd_signal,
            "note": "amount_5d_yi=近5日成交额合计(亿元)，pct_chg=最新日涨跌幅(%)；成交额放大可能反映GJD或机构资金流入",
        }
    except Exception as e:
        return {"error": f"ETF数据处理失败: {e}"}


def _tool_get_chip_distribution(ts_code: str) -> dict:
    """获取个股最近5日筹码成本分布和胜率。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        import datetime
        end = datetime.date.today().strftime("%Y%m%d")
        start = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%Y%m%d")
        df = pro.cyq_perf(ts_code=ts_code, start_date=start, end_date=end)
    except Exception as e:
        return {"error": f"cyq_perf 调用失败: {e}"}
    if df is None or df.empty:
        return {"error": "暂无筹码数据"}

    df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
    records = []
    for _, row in df.iterrows():
        records.append({
            "date":        str(row["trade_date"]),
            "cost_5pct":   round(float(row["cost_5pct"]),  2),
            "cost_15pct":  round(float(row["cost_15pct"]), 2),
            "cost_50pct":  round(float(row["cost_50pct"]), 2),
            "cost_85pct":  round(float(row["cost_85pct"]), 2),
            "cost_95pct":  round(float(row["cost_95pct"]), 2),
            "weight_avg":  round(float(row["weight_avg"]),  2),
            "winner_rate": round(float(row["winner_rate"]), 2),
        })

    latest = records[0]
    return {
        "ts_code": ts_code,
        "latest_winner_rate": latest["winner_rate"],
        "latest_weight_avg":  latest["weight_avg"],
        "records": records,
        "note": (
            "winner_rate=当前价格以下筹码占比(%)，越高说明套牢盘越少；"
            "cost_50pct=中位成本价，是重要支撑/压力参考；"
            "weight_avg=筹码加权均价"
        ),
    }


def _tool_get_technical_factors(ts_code: str) -> dict:
    """获取个股最近3日技术指标：MACD、RSI、KDJ、布林带（前复权）。"""
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        df = pro.stk_factor_pro(
            ts_code=ts_code,
            start_date=(
                __import__("datetime").date.today()
                - __import__("datetime").timedelta(days=14)
            ).strftime("%Y%m%d"),
            end_date=__import__("datetime").date.today().strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"stk_factor_pro 调用失败: {e}"}
    if df is None or df.empty:
        return {"error": "暂无技术因子数据"}

    df = df.sort_values("trade_date", ascending=False).head(3).reset_index(drop=True)

    FIELDS = {
        "macd_bfq":        "macd",
        "rsi_bfq_6":       "rsi6",
        "rsi_bfq_12":      "rsi12",
        "kdj_k_bfq":       "kdj_k",
        "kdj_d_bfq":       "kdj_d",
        "boll_upper_bfq":  "boll_upper",
        "boll_mid_bfq":    "boll_mid",
        "boll_lower_bfq":  "boll_lower",
    }

    records = []
    for _, row in df.iterrows():
        rec = {"date": str(row["trade_date"])}
        for src, dst in FIELDS.items():
            val = row.get(src)
            rec[dst] = round(float(val), 3) if val is not None and val == val else None
        records.append(rec)

    return {
        "ts_code": ts_code,
        "records": records,
        "note": (
            "macd>0且增大为多头动能增强；rsi>70超买，<30超卖；"
            "kdj_k上穿kdj_d为金叉买入信号；"
            "价格在boll_upper附近为强势但需警惕回调，在boll_lower附近为超跌支撑。"
            "所有指标均为前复权(bfq)数据。"
        ),
    }


def execute_tool(tool_name: str, tool_args: dict) -> str:
    """执行工具调用，返回 JSON 字符串结果（供 LLM 消费）。"""
    logger.info("execute_tool: name=%s args=%s", tool_name, tool_args)
    try:
        if tool_name == "get_intraday_lines":
            result = _tool_get_intraday_lines(tool_args["ts_code"])
        elif tool_name == "get_index_intraday":
            result = _tool_get_index_intraday()
        elif tool_name == "get_moneyflow":
            result = _tool_get_moneyflow(tool_args["ts_code"], tool_args.get("trade_date", ""))
        elif tool_name == "get_top_list":
            result = _tool_get_top_list(tool_args["ts_code"], tool_args.get("trade_date", ""))
        elif tool_name == "get_daily_basic":
            result = _tool_get_daily_basic(tool_args["ts_code"], tool_args.get("trade_date", ""))
        elif tool_name == "get_technical_indicators":
            result = _tool_get_technical_indicators(tool_args["ts_code"])
        elif tool_name == "get_margin_data":
            result = _tool_get_margin_data()
        elif tool_name == "get_sector_flow":
            result = _tool_get_sector_flow(tool_args.get("trade_date", ""))
        elif tool_name == "get_futures_positions":
            result = _tool_get_futures_positions()
        elif tool_name == "get_disclosure_calendar":
            result = _tool_get_disclosure_calendar(tool_args["ts_code"])
        elif tool_name == "get_share_reduction":
            result = _tool_get_share_reduction(tool_args["ts_code"])
        elif tool_name == "get_etf_flow":
            result = _tool_get_etf_flow()
        elif tool_name == "get_chip_distribution":
            result = _tool_get_chip_distribution(tool_args["ts_code"])
        elif tool_name == "get_technical_factors":
            result = _tool_get_technical_factors(tool_args["ts_code"])
        else:
            result = {"error": f"未知工具: {tool_name}"}
    except Exception as e:
        logger.error("execute_tool error: name=%s error=%s", tool_name, e)
        result = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False)


def _build_claude_tools() -> list:
    """将 TOOL_DEFINITIONS 转换为 Anthropic tool_use 格式。"""
    tools = []
    for t in TOOL_DEFINITIONS:
        tools.append({
            "name": t["name"],
            "description": t["description"],
            "input_schema": {
                "type": "object",
                "properties": {k: v for k, v in t["parameters"].items()},
                "required": t["required"],
            },
        })
    return tools


def _build_openai_tools() -> list:
    """将 TOOL_DEFINITIONS 转换为 OpenAI function calling 格式。"""
    tools = []
    for t in TOOL_DEFINITIONS:
        tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": {k: v for k, v in t["parameters"].items()},
                    "required": t["required"],
                },
            },
        })
    return tools


def _build_gemini_tools():
    """将 TOOL_DEFINITIONS 转换为 Gemini FunctionDeclaration 格式。"""
    import google.generativeai as genai
    declarations = []
    for t in TOOL_DEFINITIONS:
        props = {}
        for param_name, param_info in t["parameters"].items():
            props[param_name] = genai.types.Schema(
                type=genai.types.Type.STRING,
                description=param_info.get("description", ""),
            )
        declarations.append(
            genai.types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=genai.types.Schema(
                    type=genai.types.Type.OBJECT,
                    properties=props,
                    required=t["required"],
                ),
            )
        )
    return genai.types.Tool(function_declarations=declarations)


def call_ai_model_with_tools(system_prompt: str, user_prompt: str) -> str:
    """
    带工具调用的 AI 分析入口。
    LLM 可在分析过程中主动调用 Tushare 工具获取额外数据（分时黄白线、资金流等）。
    三个 provider 均支持 tool_use / function calling。
    """
    provider = AI_PROVIDER
    MAX_TOKENS = 4096

    # ── Claude ──────────────────────────────────────────────────────────────
    if provider == "claude":
        import anthropic
        kwargs: dict = {"api_key": CLAUDE_API_KEY, "timeout": 180.0, "default_headers": {"api-key": CLAUDE_API_KEY}}
        if CLAUDE_BASE_URL:
            kwargs["base_url"] = CLAUDE_BASE_URL
        client = anthropic.Anthropic(**kwargs)
        claude_tools = _build_claude_tools()
        messages = [{"role": "user", "content": user_prompt}]
        for _round in range(MAX_TOOL_ROUNDS):
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=claude_tools,
                messages=messages,
            )
            logger.info("claude tool_use round=%d stop_reason=%s", _round, resp.stop_reason)

            # Defensive coding: handle cases where content is None (e.g. API error, safety filter)
            response_content = resp.content or []

            if resp.stop_reason == "end_turn":
                return "".join(b.text for b in response_content if hasattr(b, "text"))

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response_content})
                tool_results = []
                for block in response_content:
                    if block.type == "tool_use":
                        result_str = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                # max_tokens, stop_reason=None, or other reasons
                if not response_content:
                    return "AI model returned no content. This might be due to safety settings, a timeout, or an API error."
                return "".join(b.text for b in response_content if hasattr(b, "text"))

        # 超过最大轮次，返回最后一次响应的文本
        logger.warning("claude tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        final_content = resp.content or []
        return "".join(b.text for b in final_content if hasattr(b, "text"))

    # ── OpenAI ───────────────────────────────────────────────────────────────
    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": OPENAI_API_KEY, "timeout": 180.0}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        openai_tools = _build_openai_tools()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]

        for _round in range(MAX_TOOL_ROUNDS):
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=MAX_TOKENS,
                tools=openai_tools,
                messages=messages,
            )
            choice = resp.choices[0]
            logger.info("openai tool_use round=%d finish_reason=%s", _round, choice.finish_reason)

            if choice.finish_reason == "stop":
                return choice.message.content or ""

            if choice.finish_reason == "tool_calls":
                msg = choice.message
                # DeepSeek requires passing back 'reasoning_content'.
                # Dumping the whole message object is the safest way to preserve all fields.
                messages.append(msg.model_dump())
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result_str = execute_tool(tc.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
            else:
                return choice.message.content or ""

        logger.warning("openai tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        return resp.choices[0].message.content or ""

    # ── Gemini ───────────────────────────────────────────────────────────────
    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_tool = _build_gemini_tools()
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt,
            tools=[gemini_tool],
        )
        chat = model.start_chat()

        resp = chat.send_message(
            user_prompt,
            generation_config=genai_types.GenerationConfig(max_output_tokens=MAX_TOKENS),
            request_options={"timeout": 180},
        )

        for _round in range(MAX_TOOL_ROUNDS):
            # 检查是否有 function_call
            fc_parts = [p for p in resp.parts if p.function_call.name]
            logger.info("gemini tool_use round=%d fc_count=%d", _round, len(fc_parts))

            if not fc_parts:
                # 没有工具调用，返回文本
                return resp.text

            # 执行所有工具，构造 FunctionResponse 列表
            fn_responses = []
            for part in fc_parts:
                fc = part.function_call
                result_str = execute_tool(fc.name, dict(fc.args))
                fn_responses.append(
                    genai_types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result_str},
                    )
                )

            resp = chat.send_message(
                fn_responses,
                generation_config=genai_types.GenerationConfig(max_output_tokens=MAX_TOKENS),
                request_options={"timeout": 180},
            )

        logger.warning("gemini tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        return resp.text

    else:
        raise ValueError(f"不支持的 AI_PROVIDER: {provider}，请设置为 claude / openai / gemini")


def call_ai_model(system_prompt: str, user_prompt: str) -> str:
    """统一调用接口，根据 AI_PROVIDER 分发到对应模型"""
    provider = AI_PROVIDER
    # 分析报告含4个部分，1024 token 容易截断；放大到 4096 确保输出完整
    MAX_TOKENS = 4096
    # 网络请求超时（秒）：连接超时 10s，读取超时 120s
    TIMEOUT = (10, 120)

    if provider == "claude":
        import anthropic
        kwargs: dict = {"api_key": CLAUDE_API_KEY, "timeout": 120.0, "default_headers": {"api-key": CLAUDE_API_KEY}}
        if CLAUDE_BASE_URL:
            kwargs["base_url"] = CLAUDE_BASE_URL
        client = anthropic.Anthropic(**kwargs)
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(b.text for b in (msg.content or []) if hasattr(b, "text"))

    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": OPENAI_API_KEY, "timeout": 120.0}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content

    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt,
        )
        resp = model.generate_content(
            user_prompt,
            generation_config=genai_types.GenerationConfig(max_output_tokens=MAX_TOKENS),
            request_options={"timeout": 120},
        )
        return resp.text

    else:
        raise ValueError(f"不支持的 AI_PROVIDER: {provider}，请设置为 claude / openai / gemini")


def calculate_8848(code: str):
    try:
        # Fetch real-time data
        # Note: tushare.get_realtime_quotes returns a DataFrame
        df = ts.get_realtime_quotes(code)
        
        if df is None or df.empty:
            return {"error": "Stock code not found or data unavailable."}

        # Extract data
        name = str(df.loc[0, 'name'])
        # 更新名称缓存（内存 + SQLite），供常用股票列表等复用
        set_cached_name(code, name)
        price = float(df.loc[0, 'price'])
        high = float(df.loc[0, 'high'])
        low = float(df.loc[0, 'low'])

        volume = float(df.loc[0, 'volume']) # Volume in shares
        amount = float(df.loc[0, 'amount']) # Amount in Yuan
        
        if volume == 0:
            return {"error": "Volume is 0, cannot calculate average price (Market might be closed or just opened)."}

        if price == 0:
             return {"error": "Current price is 0, cannot calculate (Stock might be suspended)."}

        # Calculate Intraday Average Price (ZSTJJ)
        avg_price = amount / volume
        
        # Heuristic check
        if abs(avg_price - price) / price > 0.5:
             if abs((avg_price * 100) - price) / price < 0.5:
                 avg_price *= 100
        
        # Save daily record
        save_daily_record(code, name, {
            "price": price, "high": high, "low": low, "avg_price": avg_price
        })

        # Load Portfolio Settings
        portfolio = get_portfolio(code)
        cost_price = portfolio['cost']
        stage_high = portfolio['stage_high']  # 保留原始值，0 表示未设置
        stage_low  = portfolio['stage_low']   # 保留原始值，0 表示未设置
        max_price  = portfolio['max_price']   # 持仓以来历史最高价

        # 自动维护 max_price：仅在持仓时，用当日最高价刷新历史最高价
        if cost_price > 0 and high > max_price:
            max_price = high
            save_portfolio(code, cost_price, stage_high, stage_low, max_price)

        # st_high：持仓以来历史最高价（用于动态止盈线计算），首次持仓当日 fallback 到当日最高
        st_high = max_price if max_price > 0 else high

        # stage_params_set：用户是否设置了有效的阶段高低点
        stage_params_set = (
            stage_high > 0
            and stage_low > 0
            and stage_high > stage_low  # 合理性校验，防止 diff 为负
        )

        # Strategy Logic
        strat = calculate_strategy(price, cost_price, st_high, stage_high, stage_low, stage_params_set)

        # 8848 Formula
        upper_line = avg_price / 0.98848
        lower_line = avg_price * 0.98848

        # N-Day Stats（20日 + 60日，用于页面展示建议值）
        n_day = get_n_day_stats(code)

        boll_data = _calc_boll(code)
        macd_data = _calc_macd(code)

        # 个股近20日相对首日涨跌幅序列（供大盘走势图叠加对比）
        stock_pct = None
        if boll_data and boll_data.get("recent_closes"):
            rc = boll_data["recent_closes"]
            if rc and rc[0]["close"]:
                base = rc[0]["close"]
                stock_pct = [
                    round((r["close"] - base) / base * 100, 2) if r["close"] else None
                    for r in rc
                ]

        return {
            "code": code,
            "name": name,
            "current_price": price,
            "avg_price": round(avg_price, 4),
            "upper_line": round(upper_line, 4),
            "lower_line": round(lower_line, 4),
            "status": "success",
            "high": high,
            "low": low,
            "cost_price": cost_price,
            "stage_high": stage_high,
            "stage_low": stage_low,
            "max_price": round(max_price, 4),
            "stage_params_set": stage_params_set,
            "signal": strat["signal"],
            "advice_class": strat["advice_class"],
            "f382": strat["f382"],
            "f618": strat["f618"],
            "f786": strat["f786"],
            "n20_high": n_day["n20_high"],
            "n20_low":  n_day["n20_low"],
            "n60_high": n_day["n60_high"],
            "n60_low":  n_day["n60_low"],
            "intraday_points": _get_intraday_points(code),
            "boll": boll_data,
            "macd": macd_data,
            "stock_pct": stock_pct,
            "yidong": _calc_yidong(code, price),
        }

    except Exception as e:
        return {"error": str(e)}

@app.post("/update_portfolio", response_class=HTMLResponse)
async def update_portfolio(
    request: Request,
    code:       str   = Form(...),
    cost_price: float = Form(0.0),
    stage_high: float = Form(0.0),
    stage_low:  float = Form(0.0),
    max_price:  float = Form(0.0),  # 允许用户手动修正历史最高价
):
    import uuid
    # 若表单传入的 max_price > 0 则使用表单值，否则保留数据库中的旧值，防止意外清零
    current = get_portfolio(code)
    effective_max_price = max_price if max_price > 0 else current['max_price']
    save_portfolio(code, cost_price, stage_high, stage_low, effective_max_price)

    result = calculate_8848(code)
    if isinstance(result, dict) and result.get("status") == "success":
        save_query_history(result["code"], result["name"])

    history_results = calculate_8848_history(code, days=20)
    stock_volume    = get_stock_volume_chart_data(history_results)
    index_trend    = get_index_trend_chart_data(days=20)

    rid = str(uuid.uuid4())
    save_temp_result(rid, {
        "result":          result,
        "last_code":       code,
        "batch_results":   None,
        "history_results": history_results,
        "stock_volume":    stock_volume,
        "index_trend":    index_trend,
        "ai_analysis":     None,
        "ai_error":        None,
        "ai_provider":     AI_PROVIDER,
        "ai_mode":         "intraday",
        "user_hint":       "",
    })
    return RedirectResponse(url=f"/?result_id={rid}", status_code=303)


def to_ts_code(code: str) -> str:
    """
    将各种格式的股票代码规范化为 Tushare Pro ts_code 格式。
    支持：600519 / sh600519 / 600519.SH -> 600519.SH
    返回空字符串表示无法识别。
    """
    raw = code.strip().lower()
    if "." in raw:
        return raw.upper()
    elif raw.startswith(("sh", "sz")) and len(raw) >= 8:
        num = raw[-6:]
        market = "SH" if raw.startswith("sh") else "SZ"
        return f"{num}.{market}"
    elif len(raw) == 6 and raw.isdigit():
        if raw.startswith(("600", "601", "603", "605", "688", "689")):
            return f"{raw}.SH"
        else:
            return f"{raw}.SZ"
    return ""


def calculate_8848_history(code: str, days: int = 20):
    """
    计算最近 days 个交易日的 8848 上下轨信息。
    依赖 pro 日线数据，如果未配置 Tushare Token，则返回空列表。
    """
    if pro is None:
        logger.warning("calculate_8848_history: pro client is None, skip history. code=%s", code)
        return []

    try:
        ts_code = to_ts_code(code)
        if not ts_code:
            logger.warning("calculate_8848_history: unrecognized code format code=%s", code)
            return []

        logger.info("calculate_8848_history: fetching history ts_code=%s days=%d", ts_code, days)
        # 获取最近若干交易日数据，这里多取一点再截断，避免停牌等情况
        df = pro.daily(ts_code=ts_code, limit=days * 5)
    except Exception as e:
        logger.exception("Failed to fetch history for code=%s", code)
        return []

    if df is None or df.empty:
        logger.warning("calculate_8848_history: empty dataframe for ts_code=%s", ts_code)
        return []

    # 兼容不同字段命名，优先使用收盘价
    # tushare pro.daily 默认有 'trade_date','close','amount','vol' 等
    logger.info(
        "calculate_8848_history: got %d raw rows for ts_code=%s", len(df.index), ts_code
    )

    records = []
    for _, row in df.iterrows():
        try:
            close_price = float(row["close"])
        except Exception:
            continue

        amount = float(row.get("amount", 0))  # 单位通常为千元
        volume = float(row.get("vol", 0))     # 单位通常为手

        if volume > 0 and amount > 0:
            # 将成交额和成交量缩放到与价格同一量级，简单按常见单位做近似换算
            avg_price = (amount * 1000) / (volume * 100)  # 千元->元，手->股
        else:
            avg_price = close_price

        upper_line = avg_price / 0.98848
        lower_line = avg_price * 0.98848

        if close_price > upper_line:
            position = "high"
        elif close_price < lower_line:
            position = "low"
        else:
            position = "neutral"

        try:
            open_price = float(row.get("open", close_price))
        except Exception:
            open_price = close_price

        records.append(
            {
                "date": str(row.get("trade_date", "")),
                "open": round(open_price, 4),
                "close": round(close_price, 4),
                "high": round(float(row.get("high", close_price)), 4),
                "low": round(float(row.get("low", close_price)), 4),
                "avg_price": round(avg_price, 4),
                "upper_line": round(upper_line, 4),
                "lower_line": round(lower_line, 4),
                "position": position,
                "amount_yi": round(amount / 100000, 2) if amount > 0 else 0,
            }
        )

    # 按日期排序，取最近 days 条
    records_sorted = sorted(records, key=lambda x: x["date"], reverse=True)
    logger.info(
        "calculate_8848_history: built %d records (limit=%d) for code=%s",
        len(records_sorted),
        days,
        code,
    )
    return records_sorted[:days]


@app.post("/analyze_batch", response_class=HTMLResponse)
async def analyze_batch(request: Request):
    import uuid
    results = []
    for item in COMMON_STOCKS:
        code = item.get("code")
        if not code:
            continue
        res = calculate_8848(code)
        if res.get("status") == "success":
            results.append(res)

    rid = str(uuid.uuid4())
    save_temp_result(rid, {
        "result":          None,
        "last_code":       "",
        "batch_results":   results,
        "history_results": None,
        "ai_analysis":     None,
        "ai_error":        None,
        "ai_provider":     AI_PROVIDER,
        "ai_mode":         "intraday",
        "user_hint":       "",
    })
    return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, result_id: str = None):
    defaults = {
        "common_stocks":   build_common_stocks_with_name(),
        "batch_results":   None,
        "history_results": None,
        "stock_volume":    None,
        "index_trend":    get_index_trend_chart_data(days=20),
        "last_code":       "",
        "query_history":   get_query_history(),
        "ai_analysis":     None,
        "ai_error":        None,
        "ai_provider":     AI_PROVIDER,
        "ai_mode":         "intraday",
        "user_hint":       "",
        "result":          None,
    }
    ctx = load_temp_result(result_id) if result_id else {}
    # query_history 和 common_stocks 总是刷新，不从缓存取
    ctx.pop("query_history", None)
    ctx.pop("common_stocks", None)
    # index_trend 始终从 DB 刷新，不用缓存中的旧值
    ctx.pop("index_trend", None)
    return templates.TemplateResponse("index.html", {"request": request, **defaults, **ctx})

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_stock(request: Request, stock_code: str = Form(...)):
    import uuid
    logger.info("analyze_stock: start code=%s", stock_code)
    result = calculate_8848(stock_code)

    # 查询成功时记录历史
    if isinstance(result, dict) and result.get("status") == "success":
        save_query_history(result["code"], result["name"])

    logger.info(
        "analyze_stock: done code=%s status=%s",
        stock_code,
        result.get("status") if isinstance(result, dict) else "unknown",
    )

    history_results = calculate_8848_history(stock_code, days=20)
    stock_volume    = get_stock_volume_chart_data(history_results)
    index_trend    = get_index_trend_chart_data(days=20)

    rid = str(uuid.uuid4())
    save_temp_result(rid, {
        "result":          result,
        "last_code":       stock_code,
        "batch_results":   None,
        "history_results": history_results,
        "stock_volume":    stock_volume,
        "index_trend":    index_trend,
        "ai_analysis":     None,
        "ai_error":        None,
        "ai_provider":     AI_PROVIDER,
        "ai_mode":         "intraday",
        "user_hint":       "",
    })
    return RedirectResponse(url=f"/?result_id={rid}", status_code=303)


@app.post("/ai_analyze", response_class=HTMLResponse)
async def ai_analyze(request: Request, stock_code: str = Form(...), ai_mode: str = Form("intraday"), user_hint: str = Form("")):
    """手动触发 AI 分析，基于阿狼技能库给出操作建议
    ai_mode: 'intraday' = 盘中分析 | 'next_day' = 盘后/明日计划
    """
    import uuid
    logger.info("ai_analyze: start code=%s provider=%s mode=%s", stock_code, AI_PROVIDER, ai_mode)

    # 1. 先跑一次 8848 获取最新数据
    result = calculate_8848(stock_code)
    if result.get("error"):
        rid = str(uuid.uuid4())
        save_temp_result(rid, {
            "result":          result,
            "last_code":       stock_code,
            "batch_results":   None,
            "history_results": [],
            "ai_analysis":     None,
            "ai_error":        f"获取股票数据失败：{result.get('error')}",
            "ai_provider":     AI_PROVIDER,
            "ai_mode":         ai_mode,
            "user_hint":       user_hint,
        })
        return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

    # 2. 取近 60 日历史数据
    history = []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT date, close, high, low, avg_price FROM daily_records "
            "WHERE code = ? ORDER BY date DESC LIMIT 60",
            (stock_code,),
        ).fetchall()
        conn.close()
        history = [dict(r) for r in rows]
    except Exception as e:
        logger.warning("ai_analyze: failed to load history for %s: %s", stock_code, e)

    # 3. 加载 skills + 大盘数据 + 构建 prompt
    skills_text = load_skills()
    system_prompt = (
        "你是基于阿狼投资体系的 A 股分析助手。"
        "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
        + skills_text
    )
    index_data = get_index_market_data(days=20)
    user_prompt = build_ai_prompt(result, history, mode=ai_mode, user_hint=user_hint, index_data=index_data)

    # 4. 调用 AI 模型（带工具调用）
    ai_analysis = None
    ai_error = None
    try:
        ai_analysis = call_ai_model_with_tools(system_prompt, user_prompt)
        logger.info("ai_analyze: done code=%s", stock_code)
    except Exception as e:
        ai_error = str(e)
        logger.error("ai_analyze: failed code=%s error=%s", stock_code, e)

    hist_for_chart = calculate_8848_history(stock_code, days=20)
    stock_volume   = get_stock_volume_chart_data(hist_for_chart)
    index_trend   = get_index_trend_chart_data(days=20)

    rid = str(uuid.uuid4())
    save_temp_result(rid, {
        "result":          result,
        "last_code":       stock_code,
        "batch_results":   None,
        "history_results": hist_for_chart,
        "stock_volume":    stock_volume,
        "index_trend":    index_trend,
        "ai_analysis":     ai_analysis,
        "ai_error":        ai_error,
        "ai_provider":     AI_PROVIDER,
        "ai_mode":         ai_mode,
        "user_hint":       user_hint,
    })
    return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

@app.post("/update_common_stocks", response_class=HTMLResponse)
async def update_common_stocks(request: Request, codes: str = Form(...)):
    """页面内管理常用股票：更新 .env 并热重载全局变量。"""
    global COMMON_STOCKS
    code_list = [c.strip() for c in codes.replace("，", ",").split(",") if c.strip()]
    new_val = ",".join(code_list)

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    _update_env_key(env_path, "COMMON_STOCK_CODES", new_val)

    load_dotenv(override=True)
    COMMON_STOCKS = load_common_stocks()

    return RedirectResponse(url="/", status_code=303)


@app.post("/clear_history", response_class=HTMLResponse)
async def clear_history(request: Request):
    """清空查询历史记录。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM query_history")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to clear query history: {e}")
    return RedirectResponse(url="/", status_code=303)


# ── 板块轮动分析 ──────────────────────────────────────────────────────────────

def build_sector_prompt_data() -> dict:
    """
    拉取板块轮动分析所需的原始数据，返回结构化 dict。
    每个 Tushare 接口独立 try/except，失败时 logger.warning 并填充空值，不崩溃。

    返回结构：
    {
        "sector_perf": [{"name", "ts_code", "pct_change_1d", "pct_change_5d", "amount_5d_yi"}],
        "hsgt_flow":   [{"trade_date", "north_money", "south_money"}],
        "sector_stocks": {
            "板块名称": [{"ts_code", "name", "turnover_rate", "pe_ttm", "total_mv_yi", "pct_change"}]
        },
        "errors": ["接口名: 错误信息", ...]
    }
    """
    from datetime import datetime, timedelta

    result = {
        "sector_perf": [],
        "hsgt_flow": [],
        "sector_stocks": {},
        "errors": [],
    }

    if pro is None:
        result["errors"].append("Tushare Pro 未初始化（缺少 TUSHARE_TOKEN）")
        return result

    today = datetime.today()
    # 往前推15个自然日，确保能覆盖5个交易日
    start_date = (today - timedelta(days=15)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    # ── 1. 获取同花顺行业指数列表 ──────────────────────────────────────────
    sector_list = []
    try:
        df_idx = pro.ths_index(exchange="A", type="N")
        if df_idx is not None and not df_idx.empty:
            sector_list = df_idx[["ts_code", "name"]].dropna().to_dict("records")
            logger.info("build_sector_prompt_data: got %d ths_index sectors", len(sector_list))
        else:
            result["errors"].append("ths_index: 返回空数据")
    except Exception as e:
        logger.warning("build_sector_prompt_data: ths_index failed: %s", e)
        result["errors"].append(f"ths_index: {e}")

    # ── 2. 获取各板块近期日线数据（涨跌幅、成交额）────────────────────────
    sector_daily = {}  # ts_code -> list of daily records
    if sector_list:
        # 批量拉取所有行业指数日线（一次调用，按日期范围）
        try:
            df_daily = pro.ths_daily(
                start_date=start_date,
                end_date=end_date,
            )
            if df_daily is not None and not df_daily.empty:
                for _, row in df_daily.iterrows():
                    code = str(row.get("ts_code", ""))
                    if not code:
                        continue
                    if code not in sector_daily:
                        sector_daily[code] = []
                    sector_daily[code].append({
                        "trade_date": str(row.get("trade_date", "")),
                        "close":      float(row.get("close", 0) or 0),
                        "pct_change": float(row.get("pct_change", 0) or 0),
                        "amount":     float(row.get("turnover_rate", 0) or 0),  # ths_daily 无 amount，用换手率代替
                    })
                logger.info("build_sector_prompt_data: got ths_daily for %d sectors", len(sector_daily))
            else:
                result["errors"].append("ths_daily: 返回空数据")
        except Exception as e:
            logger.warning("build_sector_prompt_data: ths_daily failed: %s", e)
            result["errors"].append(f"ths_daily: {e}")

    # ── 3. 计算各板块近1日/近5日涨幅，排序 ────────────────────────────────
    sector_code_to_name = {s["ts_code"]: s["name"] for s in sector_list}
    perf_list = []
    for ts_code, records in sector_daily.items():
        # 按日期降序排列
        records_sorted = sorted(records, key=lambda x: x["trade_date"], reverse=True)
        pct_1d = records_sorted[0]["pct_change"] if records_sorted else 0
        # 近5日累计涨幅：(1+r1)*(1+r2)*...
        pct_5d = 0.0
        if len(records_sorted) >= 2:
            cum = 1.0
            for r in records_sorted[:5]:
                cum *= (1 + r["pct_change"] / 100)
            pct_5d = round((cum - 1) * 100, 2)
        perf_list.append({
            "ts_code":      ts_code,
            "name":         sector_code_to_name.get(ts_code, ts_code),
            "pct_change_1d": round(pct_1d, 2),
            "pct_change_5d": pct_5d,
            "days_data":    len(records_sorted),
        })

    # 按近5日涨幅降序排列，取前20
    perf_list.sort(key=lambda x: x["pct_change_5d"], reverse=True)
    result["sector_perf"] = perf_list[:20]
    logger.info("build_sector_prompt_data: sector_perf built, top=%s",
                result["sector_perf"][0]["name"] if result["sector_perf"] else "N/A")

    # ── 4. 获取北向资金近5日 ────────────────────────────────────────────────
    try:
        df_hsgt = pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
        if df_hsgt is not None and not df_hsgt.empty:
            df_hsgt = df_hsgt.sort_values("trade_date", ascending=False).head(5)
            for _, row in df_hsgt.iterrows():
                result["hsgt_flow"].append({
                    "trade_date":   str(row.get("trade_date", "")),
                    "north_money":  round(float(row.get("north_money", 0) or 0), 2),
                    "south_money":  round(float(row.get("south_money", 0) or 0), 2),
                })
            logger.info("build_sector_prompt_data: got %d hsgt records", len(result["hsgt_flow"]))
        else:
            result["errors"].append("moneyflow_hsgt: 返回空数据")
    except Exception as e:
        logger.warning("build_sector_prompt_data: moneyflow_hsgt failed: %s", e)
        result["errors"].append(f"moneyflow_hsgt: {e}")

    # ── 5. 对涨幅前8的板块，拉取成分股及基本面 ─────────────────────────────
    top_sectors = result["sector_perf"][:8]
    for sector in top_sectors:
        sector_ts_code = sector["ts_code"]
        sector_name = sector["name"]
        stocks_in_sector = []

        # 5a. 获取成分股列表
        member_codes = []
        try:
            df_members = pro.ths_member(ts_code=sector_ts_code)
            if df_members is not None and not df_members.empty:
                member_codes = df_members["con_code"].dropna().tolist()[:30]  # 最多30只
                logger.info("build_sector_prompt_data: sector=%s members=%d", sector_name, len(member_codes))
            else:
                result["errors"].append(f"ths_member({sector_name}): 返回空数据")
        except Exception as e:
            logger.warning("build_sector_prompt_data: ths_member(%s) failed: %s", sector_name, e)
            result["errors"].append(f"ths_member({sector_name}): {e}")

        # 5b. 对成分股拉取基本面（换手率、PE、市值）
        for raw_code in member_codes[:20]:  # 最多20只，避免调用过多
            ts_code_stock = to_ts_code(str(raw_code))
            if not ts_code_stock:
                continue
            try:
                df_basic = pro.daily_basic(ts_code=ts_code_stock, limit=1,
                                           fields="ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate,pct_chg")
                if df_basic is not None and not df_basic.empty:
                    row = df_basic.iloc[0]
                    # 获取股票名称（从缓存）
                    stock_name = get_cached_name(ts_code_stock) or ts_code_stock
                    total_mv_yi = round(float(row.get("total_mv", 0) or 0) / 10000, 1)  # 万元 -> 亿元
                    stocks_in_sector.append({
                        "ts_code":      ts_code_stock,
                        "name":         stock_name,
                        "turnover_rate": round(float(row.get("turnover_rate", 0) or 0), 2),
                        "pe_ttm":       round(float(row.get("pe_ttm", 0) or 0), 1),
                        "total_mv_yi":  total_mv_yi,
                        "pct_chg":      round(float(row.get("pct_chg", 0) or 0), 2),
                    })
            except Exception as e:
                logger.warning("build_sector_prompt_data: daily_basic(%s) failed: %s", ts_code_stock, e)
                # 单只股票失败不记录到 errors，避免信息过多

        # 按换手率降序排列，取前5（换手率高 = 活跃度高）
        stocks_in_sector.sort(key=lambda x: x["turnover_rate"], reverse=True)
        result["sector_stocks"][sector_name] = stocks_in_sector[:5]

    return result


def build_sector_ai_prompt(data: dict, user_hint: str = "") -> str:
    """
    将 build_sector_prompt_data() 的结果组装成 AI 分析 prompt。
    """
    today_str = time.strftime("%Y-%m-%d")

    # ── 大盘风向标 ──────────────────────────────────────────────────────────
    index_data = get_index_market_data(days=10)
    index_sections = []
    for ts_code, idata in index_data.items():
        name = idata.get("name", ts_code)
        records = idata.get("records", [])
        if records:
            lines = "\n".join(
                f"  {r['date']}: 收{r['close']} 高{r['high']} 低{r['low']} 成交额{r['amount_yi']}亿"
                for r in records[:5]
            )
            index_sections.append(f"{name}（{ts_code}）近5日：\n{lines}")
        else:
            index_sections.append(f"{name}（{ts_code}）：暂无数据")
    index_text = "\n\n".join(index_sections) if index_sections else "暂无大盘数据"

    # ── 北向资金 ────────────────────────────────────────────────────────────
    if data["hsgt_flow"]:
        hsgt_lines = "\n".join(
            f"  {r['trade_date']}: 北向净流入 {r['north_money']:.1f}亿"
            for r in data["hsgt_flow"]
        )
        hsgt_text = hsgt_lines
    else:
        hsgt_text = "暂无数据（接口不可用或无权限）"

    # ── 板块涨跌幅排名 ──────────────────────────────────────────────────────
    if data["sector_perf"]:
        # 近5日涨幅前10
        top5d = data["sector_perf"][:10]
        perf_lines_5d = "\n".join(
            f"  {i+1}. {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}%  今日{s['pct_change_1d']:+.2f}%"
            for i, s in enumerate(top5d)
        )
        # 近5日跌幅前5（末尾）
        bottom5d = sorted(data["sector_perf"], key=lambda x: x["pct_change_5d"])[:5]
        perf_lines_bottom = "\n".join(
            f"  {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}%  今日{s['pct_change_1d']:+.2f}%"
            for s in bottom5d
        )
        sector_text = f"近5日涨幅前10：\n{perf_lines_5d}\n\n近5日跌幅前5（弱势板块）：\n{perf_lines_bottom}"
    else:
        sector_text = "暂无板块涨跌幅数据（ths_daily 接口不可用或无权限）"

    # ── 各板块候选个股 ──────────────────────────────────────────────────────
    stocks_sections = []
    for sector_name, stocks in data["sector_stocks"].items():
        if not stocks:
            continue
        stock_lines = "\n".join(
            f"    {s['ts_code']} {s['name']}: 今日{s['pct_chg']:+.2f}% 换手{s['turnover_rate']:.1f}% "
            f"PE(TTM){s['pe_ttm']:.1f} 市值{s['total_mv_yi']:.0f}亿"
            for s in stocks
        )
        stocks_sections.append(f"  【{sector_name}】活跃个股（按换手率排序）：\n{stock_lines}")
    stocks_text = "\n\n".join(stocks_sections) if stocks_sections else "暂无个股基本面数据"

    # ── 数据质量说明 ────────────────────────────────────────────────────────
    if data["errors"]:
        errors_text = "（以下接口数据不可用，分析时请忽略对应部分）：\n" + "\n".join(f"  - {e}" for e in data["errors"])
    else:
        errors_text = "（所有数据接口正常）"

    user_hint_text = f"\n【用户补充说明】\n{user_hint.strip()}" if user_hint and user_hint.strip() else ""

    return f"""【分析日期】{today_str}

【大盘风向标（近5日，按日期倒序）】
{index_text}

【北向资金（沪深港通，近5日）】
{hsgt_text}

【同花顺行业板块涨跌幅排名】
{sector_text}

【涨幅前8板块的活跃个股（换手率前5，含基本面）】
{stocks_text}

【数据说明】{errors_text}
{user_hint_text}

【分析要求】
你是一位基于阿狼投资体系的 A 股分析师。请根据以上数据，给出板块轮动分析和个股推荐。

以下是分析框架（仅供参考，请结合你自己的判断，不要机械套用）：

1. **大盘阶段判断**（参考 Skill 01 的 3-X 框架）：根据三大指数的量能和价格走势，当前大盘处于哪个阶段？对操作有何影响？

2. **当前主线板块**：根据近期涨跌幅和资金流向，哪 2-3 个板块处于主升或启动阶段？请说明判断依据（量能持续性、资金来源）。

3. **板块轮动方向**（参考 Skill 04 的轮动逻辑）：资金从哪里流出，往哪里流入？当前处于哪个轮动节点？

4. **个股推荐**（每个推荐板块 1-2 只，从上方候选个股中选择或根据你的知识补充）：
   - 股票代码 + 名称
   - 类型判断（参考 Skill 11：A/B/C/D/E 类）
   - 推荐理由（不超过 3 句，重点说明为什么是这只而不是其他）
   - 操作建议（买入条件 / 关键风险提示）

5. **不建议参与的方向**：当前哪些板块或个股应该回避？原因是什么？

注意：
- 以上分析基于有限的量化数据，仅供参考，不构成投资建议
- 如果某类数据不可用，请跳过依赖该数据的分析，不要编造数据
- 个股推荐要有明确的选择理由，不要仅因为涨幅高就推荐"""


@app.get("/sector", response_class=HTMLResponse)
async def sector_page(request: Request, result_id: str = None):
    """板块轮动分析页面入口（GET，渲染空页面或从临时结果恢复）。"""
    defaults = {
        "ai_analysis": None,
        "ai_error":    None,
        "sector_data": None,
        "ai_provider": AI_PROVIDER,
        "user_hint":   "",
    }
    ctx = load_temp_result(result_id) if result_id else {}
    return templates.TemplateResponse("sector.html", {"request": request, **defaults, **ctx})


@app.post("/sector_analyze", response_class=HTMLResponse)
async def sector_analyze(request: Request, user_hint: str = Form("")):
    """触发板块轮动 AI 分析。"""
    logger.info("sector_analyze: start provider=%s", AI_PROVIDER)

    # 1. 拉取板块数据
    sector_data = build_sector_prompt_data()
    logger.info(
        "sector_analyze: data ready sectors=%d hsgt=%d errors=%d",
        len(sector_data["sector_perf"]),
        len(sector_data["hsgt_flow"]),
        len(sector_data["errors"]),
    )

    # 2. 构建 prompt
    skills_text = load_skills()
    system_prompt = (
        "你是基于阿狼投资体系的 A 股板块轮动分析助手。"
        "以下是阿狼投资体系的技能库，请在分析中参考它，但也要结合你自己的知识进行独立判断，"
        "不要过度依赖框架导致分析僵化：\n\n"
        + skills_text
    )
    user_prompt = build_sector_ai_prompt(sector_data, user_hint=user_hint)

    # 3. 调用 AI（不带工具调用，板块分析数据已在 prompt 中）
    ai_analysis = None
    ai_error = None
    try:
        ai_analysis = call_ai_model(system_prompt, user_prompt)
        logger.info("sector_analyze: done")
    except Exception as e:
        ai_error = str(e)
        logger.error("sector_analyze: failed error=%s", e)

    import uuid
    rid = str(uuid.uuid4())
    save_temp_result(rid, {
        "ai_analysis": ai_analysis,
        "ai_error":    ai_error,
        "sector_data": sector_data,
        "ai_provider": AI_PROVIDER,
        "user_hint":   user_hint,
    })
    return RedirectResponse(url=f"/sector?result_id={rid}", status_code=303)
