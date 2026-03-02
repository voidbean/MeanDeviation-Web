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


def init_cache_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_name_cache (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    updated_at INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"Failed to init cache db: {e}")


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


init_cache_db()


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
        # Tushare volume is in shares (hand * 100), amount is in Yuan
        # However, get_realtime_quotes returns volume in 'hands' (100 shares) usually?
        # Let's check the raw values carefully. 
        # Actually standard Sina source: volume is in Shares, amount is in Yuan.
        # But sometimes amount is in 10k. 
        # Let's use a heuristic: Average price should be close to current price.
        
        volume = float(df.loc[0, 'volume']) # Volume in shares
        amount = float(df.loc[0, 'amount']) # Amount in Yuan
        
        if volume == 0:
            return {"error": "Volume is 0, cannot calculate average price (Market might be closed or just opened)."}

        if price == 0:
             return {"error": "Current price is 0, cannot calculate (Stock might be suspended)."}

        # Calculate Intraday Average Price (ZSTJJ)
        # Verify unit scaling. If avg_price is way off price, adjust.
        avg_price = amount / volume
        
        # Heuristic check: if avg_price is 100x smaller than price, volume might be in shares but amount in 100s? 
        # Or if volume is in hands.
        # Standard Sina API: volume (shares), amount (yuan).
        # But let's add a safety factor.
        if abs(avg_price - price) / price > 0.5:
             # If significant deviation, maybe volume is in hands?
             # Try adjusting by 100
             if abs((avg_price * 100) - price) / price < 0.5:
                 avg_price *= 100
        
        # 8848 Formula
        # Red Line (Resistance/High) = ZSTJJ / 0.98848
        upper_line = avg_price / 0.98848
        
        # Green Line (Support/Low) = ZSTJJ * 0.98848
        lower_line = avg_price * 0.98848
        
        return {
            "code": code,
            "name": name,
            "current_price": price,
            "avg_price": round(avg_price, 3),
            "upper_line": round(upper_line, 3),
            "lower_line": round(lower_line, 3),
            "status": "success"
        }

    except Exception as e:
        return {"error": str(e)}


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
