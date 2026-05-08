"""
fetch_history.py — 历史日线数据采集脚本

功能：
    从 Tushare Pro 拉取股票/ETF 最近 60 个交易日的日线数据，
    写入 daily_records 表，供 get_n_day_stats() 使用。
    ETF（上交所 5 开头、深交所 1 开头）自动使用 fund_daily 接口。

用法：
    uv run python fetch_history.py                          # 常规模式：拉取 COMMON_STOCK_CODES 最近 60 个交易日
    uv run python fetch_history.py --backfill               # 回填模式：拉取最近 90 个交易日，用于首次部署初始化
    uv run python fetch_history.py --codes 600519,588170    # 仅拉取指定股票/ETF，忽略 COMMON_STOCK_CODES
    uv run python fetch_history.py --codes 600519 --backfill  # 指定代码 + 回填模式

crontab 示例（每个工作日 15:35 收盘后自动运行）：
    35 15 * * 1-5 cd /path/to/MeanDeviation-Web && uv run python fetch_history.py >> fetch_history.log 2>&1
"""

import os
import sqlite3
import logging
import argparse
import time
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
        # 上交所：6/5/688 开头
        if raw.startswith(("600", "601", "603", "605", "688", "689", "588", "589", "510", "511", "512", "513", "515", "516", "517", "518", "519", "52")):
            return f"{raw}.SH"
        # 深交所：0/1/2/3 开头（含 ETF 159xxx 等）
        return f"{raw}.SZ"
    raise ValueError(f"无法识别的股票代码格式: {code!r}")


def is_etf(ts_code: str) -> bool:
    """
    判断一个 ts_code 是否为 ETF/基金，需使用 fund_daily 接口。
    上交所 ETF：5 开头（510xxx、512xxx、588xxx 等）
    深交所 ETF：1 开头（159xxx 等）
    """
    num = ts_code.split(".")[0]
    market = ts_code.split(".")[-1].upper()
    if market == "SH" and num.startswith("5"):
        return True
    if market == "SZ" and num.startswith("1"):
        return True
    return False


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
    amount: float = 0.0,
) -> None:
    """写入一条日线记录，已存在则覆盖（幂等）。amount 单位：千元。"""
    conn.execute(
        """
        INSERT INTO daily_records(date, code, name, close, high, low, avg_price, amount)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, code) DO UPDATE SET
            close     = excluded.close,
            high      = excluded.high,
            low       = excluded.low,
            avg_price = excluded.avg_price,
            name      = excluded.name,
            amount    = excluded.amount
        """,
        (date, code, name, close, high, low, avg_price, amount),
    )


# ── 核心逻辑 ─────────────────────────────────────────────────────────────────
def fetch_one(pro, conn: sqlite3.Connection, code: str, limit: int = FETCH_DAYS) -> int:
    """
    拉取单只股票或 ETF 的历史日线数据并写入 DB。
    ETF（上交所 5 开头、深交所 1 开头）自动使用 fund_daily 接口。
    返回成功写入的记录条数，失败时抛出异常。
    """
    ts_code = to_ts_code(code)
    name = get_cached_name(conn, code)
    use_fund_api = is_etf(ts_code)
    api_name = "fund_daily" if use_fund_api else "daily"

    logger.info("拉取 %s (%s) via %s，limit=%d", ts_code, name or "未知", api_name, limit)
    if use_fund_api:
        df = pro.fund_daily(ts_code=ts_code, limit=limit)
    else:
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
                amount=round(amount, 2),
            )
            count += 1
        except Exception as e:
            logger.warning("%s 某行数据处理失败：%s", ts_code, e)
            continue

    conn.commit()
    logger.info("%s 写入 %d 条记录", ts_code, count)
    return count


def fetch_index_one(pro, conn: sqlite3.Connection, ts_code: str, name: str, limit: int = FETCH_DAYS) -> int:
    """
    用 pro.index_daily 拉取单个指数的历史日线数据并写入 DB。
    amount 单位与 pro.daily 一致（千元），avg_price 用收盘价占位（指数无均价概念）。
    返回成功写入的记录条数，失败时抛出异常。
    """
    logger.info("拉取指数 %s (%s)，limit=%d", ts_code, name, limit)
    df = pro.index_daily(ts_code=ts_code, limit=limit)

    if df is None or df.empty:
        logger.warning("%s 返回空数据", ts_code)
        return 0

    count = 0
    for _, row in df.iterrows():
        try:
            trade_date = str(row["trade_date"])
            close  = float(row["close"])
            high   = float(row["high"])
            low    = float(row["low"])
            amount = float(row.get("amount", 0) or 0)  # 千元，与个股一致

            upsert_daily_record(
                conn,
                date=fmt_date(trade_date),
                code=ts_code,
                name=name,
                close=close,
                high=high,
                low=low,
                avg_price=close,      # 指数无均价概念，用收盘价占位
                amount=round(amount, 2),
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
    parser = argparse.ArgumentParser(description="拉取股票/ETF 历史日线数据")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：拉取最近 90 个交易日数据，用于首次部署初始化",
    )
    parser.add_argument(
        "--codes",
        type=str,
        default="",
        help="指定要拉取的股票/ETF 代码，逗号分隔，如 600519,588170,159206。"
             "指定后忽略 COMMON_STOCK_CODES 环境变量。",
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

    # --codes 优先；未指定则读取环境变量
    if args.codes.strip():
        raw_codes = args.codes.replace("，", ",")
        codes = [c.strip() for c in raw_codes.split(",") if c.strip()]
        codes_source = f"--codes 参数（{len(codes)} 只）"
    else:
        codes = load_common_codes()
        codes_source = f"COMMON_STOCK_CODES（{len(codes)} 只）"

    if not codes:
        logger.warning("未指定任何股票代码，无需拉取")
        print("提示：请通过 --codes 参数或 .env 中的 COMMON_STOCK_CODES 指定股票代码")
        return

    ts.set_token(token)
    pro = ts.pro_api()

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_tables(conn)

        success, failed = 0, 0
        start_time = datetime.now()
        logger.info("=== 开始拉取历史数据 [%s]，来源：%s ===", mode_label, codes_source)
        print(f"模式：{mode_label}，来源：{codes_source}")

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

        # ── 拉取三大指数数据（大盘风向标）────────────────────────────────────
        # 使用 pro.index_daily 接口，amount 单位与 pro.daily 个股一致（千元）
        INDEX_CODES = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
        ]
        print("\n── 拉取大盘指数数据 ──")
        logger.info("=== 开始拉取大盘指数数据，共 %d 个指数 ===", len(INDEX_CODES))

        # 预写指数名称到缓存，保证 fetch_index_one 能取到正确名称
        for ts_code, idx_name in INDEX_CODES:
            conn.execute(
                "INSERT INTO stock_name_cache(code, name, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (ts_code, idx_name, int(time.time())),
            )
        conn.commit()

        idx_success, idx_failed = 0, 0
        for ts_code, idx_name in INDEX_CODES:
            try:
                n = fetch_index_one(pro, conn, ts_code, idx_name, limit=limit)
                idx_success += 1
                print(f"  ✓ {ts_code}（{idx_name}）写入 {n} 条")
                logger.info("指数 %s 写入 %d 条", ts_code, n)
            except Exception as e:
                idx_failed += 1
                logger.error("拉取指数 %s 失败：%s", ts_code, e)
                print(f"  ✗ {ts_code}（{idx_name}）失败：{e}")

        idx_summary = f"指数完成：成功 {idx_success} 个，失败 {idx_failed} 个"
        logger.info("=== %s ===", idx_summary)
        print(f"\n{idx_summary}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
