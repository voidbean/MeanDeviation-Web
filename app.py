from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import tushare as ts
import os
import sqlite3
import time
import logging
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

app = FastAPI()
templates = Jinja2Templates(directory="templates")

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
            INSERT INTO daily_records(date, code, name, close, high, low, avg_price)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, code) DO UPDATE SET
                close = excluded.close,
                high = excluded.high,
                low = excluded.low,
                avg_price = excluded.avg_price,
                name = excluded.name
            """,
            (today, code, name, data['price'], data['high'], data['low'], data['avg_price'])
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
        "f382": round(f382, 3),
        "f618": round(f618, 3),
        "f786": round(f786, 3),
        "is_break_low": is_break_low,
    }

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

        return {
            "code": code,
            "name": name,
            "current_price": price,
            "avg_price": round(avg_price, 3),
            "upper_line": round(upper_line, 3),
            "lower_line": round(lower_line, 3),
            "status": "success",
            "high": high,
            "low": low,
            "cost_price": cost_price,
            "stage_high": stage_high,
            "stage_low": stage_low,
            "max_price": round(max_price, 3),
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
    # 若表单传入的 max_price > 0 则使用表单值，否则保留数据库中的旧值，防止意外清零
    current = get_portfolio(code)
    effective_max_price = max_price if max_price > 0 else current['max_price']
    save_portfolio(code, cost_price, stage_high, stage_low, effective_max_price)
    return await analyze_stock(request, stock_code=code)


def calculate_8848_history(code: str, days: int = 5):
    """
    计算最近 days 个交易日的 8848 上下轨信息。
    依赖 pro 日线数据，如果未配置 Tushare Token，则返回空列表。
    """
    if pro is None:
        logger.warning("calculate_8848_history: pro client is None, skip history. code=%s", code)
        return []

    try:
        # 规范化成 tushare pro 的 ts_code，例如：
        # 600519 / sh600519 / 600519.SH -> 600519.SH
        raw = code.strip().lower()
        ts_code = ""
        if "." in raw:
            # 已经是 ts_code 形式
            ts_code = raw.upper()
        elif raw.startswith(("sh", "sz")) and len(raw) >= 8:
            num = raw[-6:]
            market = "SH" if raw.startswith("sh") else "SZ"
            ts_code = f"{num}.{market}"
        elif len(raw) == 6 and raw.isdigit():
            if raw.startswith(("600", "601", "603", "605", "688", "689")):
                ts_code = f"{raw}.SH"
            else:
                ts_code = f"{raw}.SZ"
        else:
            # 无法识别的代码，直接返回空
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

        records.append(
            {
                "date": str(row.get("trade_date", "")),
                "close": round(close_price, 3),
                "avg_price": round(avg_price, 3),
                "upper_line": round(upper_line, 3),
                "lower_line": round(lower_line, 3),
                "position": position,
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
    results = []
    for item in COMMON_STOCKS:
        code = item.get("code")
        if not code:
            continue
        res = calculate_8848(code)
        if res.get("status") == "success":
            results.append(res)
        else:
            continue

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "result": None,
            "last_code": "",
            "common_stocks": build_common_stocks_with_name(),
            "batch_results": results,
            "history_results": None,
            "query_history": get_query_history(),
        },
    )

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "common_stocks": build_common_stocks_with_name(),
            "batch_results": None,
            "history_results": None,
            "last_code": "",
            "query_history": get_query_history(),
        },
    )

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_stock(request: Request, stock_code: str = Form(...)):
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "result": result,
            "last_code": stock_code,
            "common_stocks": build_common_stocks_with_name(),
            "batch_results": None,
            "history_results": [],
            "query_history": get_query_history(),
        },
    )

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
