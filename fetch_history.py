"""
fetch_history.py — 历史日线数据采集脚本

功能：
    从 Tushare Pro 拉取 COMMON_STOCKS 最近 60 个交易日的日线数据，
    写入 daily_records 表，供 get_n_day_stats() 使用。

用法：
    uv run python fetch_history.py              # 常规模式：拉取最近 60 个交易日
    uv run python fetch_history.py --backfill   # 回填模式：拉取最近 90 个交易日，用于首次部署初始化

crontab 示例（每个工作日 15:35 收盘后自动运行）：
    35 15 * * 1-5 cd /path/to/MeanDeviation-Web && uv run python fetch_history.py >> fetch_history.log 2>&1
"""

import os
import sqlite3
import logging
import argparse
from datetime import datetime

import tushare as ts
from dotenv import load_dotenv

# ── 日志配置 ────────────────────────────────────────────────────────────────
LOG_PATH = os.path.join(os.path.dirname(__file__), "fetch_history.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "stock_cache.db")
FETCH_DAYS = 60  # 拉取最近 60 个交易日，保证 N=20 统计有足够余量


# ── 初始化 DB ────────────────────────────────────────────────────────────────
def ensure_tables(conn: sqlite3.Connection) -> None:
    """确保所需表存在（幂等）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_records (
            date      TEXT,
            code      TEXT,
            name      TEXT,
            close     REAL,
            high      REAL,
            low       REAL,
            avg_price REAL,
            PRIMARY KEY (date, code)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_name_cache (
            code       TEXT PRIMARY KEY,
            name       TEXT,
            updated_at INTEGER
        )
        """
    )
    conn.commit()


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def to_ts_code(code: str) -> str:
    """
    将 6 位股票代码转换为 Tushare Pro 格式的 ts_code。
    支持：600519 / sh600519 / 600519.SH 三种输入形式。
    """
    raw = code.strip().lower()
    if "." in raw:
        return raw.upper()
    if raw.startswith(("sh", "sz")) and len(raw) >= 8:
        num = raw[-6:]
        market = "SH" if raw.startswith("sh") else "SZ"
        return f"{num}.{market}"
    if len(raw) == 6 and raw.isdigit():
        # 588xxx/589xxx 是上交所 ETF，其余 6 开头也归 SH
        if raw.startswith(("600", "601", "603", "605", "688", "689", "588", "589")):
            return f"{raw}.SH"
        return f"{raw}.SZ"
    raise ValueError(f"无法识别的股票代码格式: {code!r}")


def fmt_date(trade_date: str) -> str:
    """将 tushare 日期格式 '20260422' 转换为 '2026-04-22'。"""
    return datetime.strptime(trade_date, "%Y%m%d").strftime("%Y-%m-%d")


def get_cached_name(conn: sqlite3.Connection, code: str) -> str:
    """从 stock_name_cache 表查询股票名称，查不到返回空字符串。"""
    cur = conn.execute("SELECT name FROM stock_name_cache WHERE code = ?", (code,))
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else ""


def upsert_daily_record(
    conn: sqlite3.Connection,
    date: str,
    code: str,
    name: str,
    close: float,
    high: float,
    low: float,
    avg_price: float,
) -> None:
    """写入一条日线记录，已存在则覆盖（幂等）。"""
    conn.execute(
        """
        INSERT INTO daily_records(date, code, name, close, high, low, avg_price)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, code) DO UPDATE SET
            close     = excluded.close,
            high      = excluded.high,
            low       = excluded.low,
            avg_price = excluded.avg_price,
            name      = excluded.name
        """,
        (date, code, name, close, high, low, avg_price),
    )


# ── 核心逻辑 ─────────────────────────────────────────────────────────────────
def fetch_one(pro, conn: sqlite3.Connection, code: str, limit: int = FETCH_DAYS) -> int:
    """
    拉取单只股票的历史日线数据并写入 DB。
    返回成功写入的记录条数，失败时抛出异常。
    """
    ts_code = to_ts_code(code)
    name = get_cached_name(conn, code)

    logger.info("拉取 %s (%s)，limit=%d", ts_code, name or "未知", limit)
    df = pro.daily(ts_code=ts_code, limit=limit)

    if df is None or df.empty:
        logger.warning("%s 返回空数据，可能停牌或代码有误", ts_code)
        return 0

    count = 0
    for _, row in df.iterrows():
        try:
            trade_date = str(row["trade_date"])
            close = float(row["close"])
            high  = float(row["high"])
            low   = float(row["low"])
            amount = float(row.get("amount", 0) or 0)  # 千元
            vol    = float(row.get("vol", 0) or 0)     # 手

            # 均价换算：千元→元，手→股
            if vol > 0 and amount > 0:
                avg_price = (amount * 1000) / (vol * 100)
            else:
                avg_price = close  # 停牌日 fallback

            upsert_daily_record(
                conn,
                date=fmt_date(trade_date),
                code=code,
                name=name,
                close=close,
                high=high,
                low=low,
                avg_price=round(avg_price, 4),
            )
            count += 1
        except Exception as e:
            logger.warning("%s 某行数据处理失败：%s", ts_code, e)
            continue

    conn.commit()
    logger.info("%s 写入 %d 条记录", ts_code, count)
    return count


def load_common_codes() -> list[str]:
    """从 .env 读取 COMMON_STOCK_CODES，返回代码列表。"""
    raw = os.getenv("COMMON_STOCK_CODES", "") or ""
    raw = raw.replace("，", ",")  # 兼容中文逗号
    return [c.strip() for c in raw.split(",") if c.strip()]


# ── 入口 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="拉取常用股票历史日线数据")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：拉取最近 90 个交易日数据，用于首次部署初始化",
    )
    args = parser.parse_args()

    limit = 90 if args.backfill else FETCH_DAYS
    mode_label = "回填模式（90日）" if args.backfill else f"常规模式（{FETCH_DAYS}日）"

    load_dotenv()

    token = os.getenv("TUSHARE_TOKEN", "")
    if not token:
        logger.error("未配置 TUSHARE_TOKEN，退出")
        print("错误：请在 .env 中配置 TUSHARE_TOKEN")
        return

    codes = load_common_codes()
    if not codes:
        logger.warning("COMMON_STOCK_CODES 为空，无需拉取")
        print("提示：COMMON_STOCK_CODES 为空，请在 .env 中配置")
        return

    ts.set_token(token)
    pro = ts.pro_api()

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_tables(conn)

        success, failed = 0, 0
        start_time = datetime.now()
        logger.info("=== 开始拉取历史数据 [%s]，共 %d 只股票 ===", mode_label, len(codes))
        print(f"模式：{mode_label}，共 {len(codes)} 只股票")

        for code in codes:
            try:
                n = fetch_one(pro, conn, code, limit=limit)
                success += 1
                print(f"  ✓ {code}  写入 {n} 条")
            except Exception as e:
                failed += 1
                logger.error("拉取 %s 失败：%s", code, e)
                print(f"  ✗ {code}  失败：{e}")

        elapsed = (datetime.now() - start_time).seconds
        summary = f"完成：成功 {success} 只，失败 {failed} 只，耗时 {elapsed}s"
        logger.info("=== %s ===", summary)
        print(f"\n{summary}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
