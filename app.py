from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
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
        cur = conn.execute("SELECT cost_price, stage_high, stage_low FROM portfolio WHERE code = ?", (code,))
        row = cur.fetchone()
        conn.close()
        if row:
            return {"cost": row[0], "stage_high": row[1], "stage_low": row[2]}
    except Exception as e:
        logger.error(f"Failed to get portfolio for {code}: {e}")
    return {"cost": 0, "stage_high": 0, "stage_low": 0}

def save_portfolio(code: str, cost: float, high: float, low: float):
    """
    Save portfolio settings.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        ts_now = int(time.time())
        conn.execute(
            """
            INSERT INTO portfolio(code, cost_price, stage_high, stage_low, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                cost_price = excluded.cost_price,
                stage_high = excluded.stage_high,
                stage_low = excluded.stage_low,
                updated_at = excluded.updated_at
            """,
            (code, cost, high, low, ts_now)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save portfolio for {code}: {e}")

def get_n_day_stats(code: str, days: int = 20):
    """
    Get N-day high/low from daily_records.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            """
            SELECT MAX(high), MIN(low) FROM daily_records
            WHERE code = ? AND date >= date('now', ?)
            """,
            (code, f'-{days} days')
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
             return {"n_high": row[0], "n_low": row[1]}
    except Exception as e:
        logger.error(f"Failed to get stats for {code}: {e}")
    return {"n_high": 0, "n_low": 0}

def calculate_strategy(now, cost, st_high, stage_high, stage_low):
    """
    Implement the strategy logic from stock.html
    """
    signal = "观望"
    advice_class = "advice-normal" # mapping to Bootstrap colors: advice-danger->danger, advice-gold->warning, advice-blue->primary, advice-cyan->info
    
    # Calculate Fibonacci
    diff = stage_high - stage_low
    f382 = stage_high - diff * 0.382
    f618 = stage_high - diff * 0.618
    f786 = stage_high - diff * 0.786
    
    is_break_low = False
    
    if cost > 0:
        # === Position Mode ===
        profit_rate = (now - cost) / cost if cost > 0 else 0
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
        # === Observation Mode ===
        if stage_low > 0 and now < stage_low:
            is_break_low = True
            signal = "破位严禁"
            advice_class = "danger"
        elif stage_low > 0 and now <= f786:
            signal = "黄金坑"
            advice_class = "warning"
        elif stage_low > 0 and now <= f618:
            signal = "强支撑"
            advice_class = "primary"
        elif stage_low > 0 and now <= f382:
            signal = "常规买点"
            advice_class = "info"
        elif stage_high > 0 and now > stage_high:
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
        "is_break_low": is_break_low
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
        stage_high = portfolio['stage_high'] or high # Default to day high if not set
        stage_low = portfolio['stage_low'] or low   # Default to day low if not set
        
        # Short-term high logic: For simplicity, use max of stage_high and day high
        st_high = max(stage_high, high)

        # Strategy Logic
        strat = calculate_strategy(price, cost_price, st_high, stage_high, stage_low)
        
        # 8848 Formula
        upper_line = avg_price / 0.98848
        lower_line = avg_price * 0.98848
        
        # N-Day Stats
        n_day = get_n_day_stats(code)

        return {
            "code": code,
            "name": name,
            "current_price": price,
            "avg_price": round(avg_price, 3),
            "upper_line": round(upper_line, 3),
            "lower_line": round(lower_line, 3),
            "status": "success",
            # New Fields
            "high": high,
            "low": low,
            "cost_price": cost_price,
            "stage_high": stage_high,
            "stage_low": stage_low,
            "signal": strat["signal"],
            "advice_class": strat["advice_class"],
            "f382": strat["f382"],
            "f618": strat["f618"],
            "f786": strat["f786"],
            "n_day_high": n_day["n_high"],
            "n_day_low": n_day["n_low"]
        }

    except Exception as e:
        return {"error": str(e)}

@app.post("/update_portfolio", response_class=HTMLResponse)
async def update_portfolio(
    request: Request, 
    code: str = Form(...),
    cost_price: float = Form(0.0),
    stage_high: float = Form(0.0),
    stage_low: float = Form(0.0)
):
    save_portfolio(code, cost_price, stage_high, stage_low)
    # Redirect back to analyze
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
            # 简单忽略失败的个股，保持页面整洁
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
        },
    )

@app.post("/analyze", response_class=HTMLResponse)
async def analyze_stock(request: Request, stock_code: str = Form(...)):
    # Basic validation for stock code (add sh/sz if missing)
    # Tushare usually expects 6 digits.
    # If purely digits, we might need to guess the market, but get_realtime_quotes works with just 6 digits often.
    # However, for uniqueness, let's keep it as is or try to append likely suffix if it fails?
    # Actually get_realtime_quotes is smart enough with just '600519' etc.

    logger.info("analyze_stock: start code=%s", stock_code)
    result = calculate_8848(stock_code)
    # 为了提升速度，这里暂时不再请求近 5 日日线历史
    history_results = []
    logger.info(
        "analyze_stock: done code=%s status=%s history_len=0",
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
            "history_results": history_results,
        },
    )
