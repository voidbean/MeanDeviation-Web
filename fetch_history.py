"""
fetch_history.py — 历史日线数据采集脚本

功能：
    从 Tushare Pro 拉取股票/ETF 最近 60 个交易日的前复权（qfq）日线数据，
    写入 daily_records 表，供 get_n_day_stats() 使用。
    ETF（上交所 5 开头、深交所 1 开头）自动使用 fund_daily 接口。
    使用前复权价格可避免除权日导致 BOLL/MA 等技术指标严重失真。
    每日定时拉取近 60 日数据，复权因子变化时历史价格会自动覆盖更新。

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

from core.db import upsert_valuation_history, VALUATION_HIST_LIMIT

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

# stk_factor_pro 落库的技术指标字段（均取前复权版本 _qfq）
# 格式：(db列名, stk_factor_pro字段名)
FACTOR_FIELDS: list[tuple[str, str]] = [
    ("ma5",          "ma_qfq_5"),
    ("ma10",         "ma_qfq_10"),
    ("ma20",         "ma_qfq_20"),
    ("ma60",         "ma_qfq_60"),
    ("ema5",         "ema_qfq_5"),
    ("ema10",        "ema_qfq_10"),
    ("ema20",        "ema_qfq_20"),
    ("macd",         "macd_qfq"),
    ("macd_dif",     "macd_dif_qfq"),
    ("macd_dea",     "macd_dea_qfq"),
    ("rsi6",         "rsi_qfq_6"),
    ("rsi12",        "rsi_qfq_12"),
    ("kdj_k",        "kdj_k_qfq"),
    ("kdj_d",        "kdj_d_qfq"),
    ("kdj_j",        "kdj_qfq"),
    ("boll_upper",   "boll_upper_qfq"),
    ("boll_mid",     "boll_mid_qfq"),
    ("boll_lower",   "boll_lower_qfq"),
    ("turnover_rate","turnover_rate"),
    ("pe",           "pe"),
    ("pb",           "pb"),
    ("updays",       "updays"),
    ("downdays",     "downdays"),
]

# stk_factor_pro 请求字段列表（ts_code + trade_date + 所有指标原始字段名）
_FACTOR_API_FIELDS = "ts_code,trade_date," + ",".join(api_col for _, api_col in FACTOR_FIELDS)


def ensure_tables(conn: sqlite3.Connection) -> None:
    """确保所需表存在，并为 daily_records 补充技术指标列（幂等）。"""
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS valuation_history (
            date   TEXT NOT NULL,
            code   TEXT NOT NULL,
            pe_ttm REAL,
            pb     REAL,
            PRIMARY KEY (date, code)
        )
        """
    )
    conn.commit()

    # 动态补列：amount、open 及所有技术指标列（SQLite 不支持 IF NOT EXISTS，用 try/except）
    extra_cols = [("amount", "REAL DEFAULT 0"), ("open", "REAL DEFAULT 0"), ("vol", "REAL")] + [(db_col, "REAL") for db_col, _ in FACTOR_FIELDS]
    for col_name, col_def in extra_cols:
        try:
            conn.execute(f"ALTER TABLE daily_records ADD COLUMN {col_name} {col_def}")
            conn.commit()
            logger.info("ensure_tables: 新增列 daily_records.%s", col_name)
        except Exception:
            pass  # 列已存在，忽略


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
    open: float = 0.0,
    vol: float | None = None,
) -> None:
    """写入一条日线记录。amount 单位千元，vol 单位股；缺失不覆盖历史量。"""
    conn.execute(
        """
        INSERT INTO daily_records(date, code, name, close, high, low, avg_price, amount, open, vol)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, code) DO UPDATE SET
            close     = excluded.close,
            high      = excluded.high,
            low       = excluded.low,
            avg_price = excluded.avg_price,
            name      = excluded.name,
            amount    = excluded.amount,
            open      = excluded.open,
            vol       = COALESCE(excluded.vol, daily_records.vol)
        """,
        (date, code, name, close, high, low, avg_price, amount, open, vol),
    )


def fetch_factors_one(pro, conn: sqlite3.Connection, code: str, limit: int = FETCH_DAYS) -> int:
    """
    用 stk_factor_pro 拉取单只股票的技术指标，UPDATE 到已有 daily_records 行。
    只更新已存在的行（日线数据必须先由 fetch_one 写入），不新增行。
    返回成功更新的记录条数，失败时抛出异常。

    注意：stk_factor_pro 不支持多 ts_code 批量，只能逐只调用。
    积分要求：5000 分以上，每分钟 30 次。
    """
    ts_code = to_ts_code(code)
    if is_etf(ts_code):
        # ETF 无技术指标数据，跳过
        logger.info("fetch_factors: %s 为 ETF，跳过技术指标", ts_code)
        return 0

    logger.info("fetch_factors: 拉取 %s 技术指标，limit=%d", ts_code, limit)
    df = pro.stk_factor_pro(
        ts_code=ts_code,
        fields=_FACTOR_API_FIELDS,
        limit=limit,
    )

    if df is None or df.empty:
        logger.warning("fetch_factors: %s 返回空数据", ts_code)
        return 0

    # 构建 UPDATE 语句：只更新技术指标列，不动 close/high/low/avg_price
    set_clause = ", ".join(f"{db_col} = ?" for db_col, _ in FACTOR_FIELDS)
    sql = f"UPDATE daily_records SET {set_clause} WHERE date = ? AND code = ?"

    count = 0
    for _, row in df.iterrows():
        try:
            trade_date = fmt_date(str(row["trade_date"]))
            values = []
            for db_col, api_col in FACTOR_FIELDS:
                raw = row.get(api_col)
                values.append(float(raw) if raw is not None and str(raw) not in ("", "nan") else None)
            values.append(trade_date)  # WHERE date = ?
            values.append(code)        # WHERE code = ?
            conn.execute(sql, values)
            count += 1
        except Exception as e:
            logger.warning("fetch_factors: %s 某行处理失败：%s", ts_code, e)
            continue

    conn.commit()
    logger.info("fetch_factors: %s 更新 %d 条技术指标", ts_code, count)
    return count


def fetch_valuation_one(pro, code: str, limit: int = 10) -> int:
    """
    拉取 pe_ttm / pb 估值数据写入 valuation_history。
    常规模式增量拉最近 10 天；--backfill 拉 3 年用于首次初始化。
    """
    ts_code = to_ts_code(code)
    if is_etf(ts_code):
        logger.info("fetch_valuation: %s 为 ETF，跳过", ts_code)
        return 0

    logger.info("fetch_valuation: 拉取 %s 估值数据，limit=%d", ts_code, limit)
    try:
        df = pro.daily_basic(
            ts_code=ts_code,
            fields="trade_date,pe_ttm,pb",
            limit=limit,
        )
    except Exception as e:
        logger.error("fetch_valuation: %s API 失败：%s", ts_code, e)
        raise

    if df is None or df.empty:
        logger.warning("fetch_valuation: %s 返回空数据", ts_code)
        return 0

    records = []
    for _, row in df.iterrows():
        pe_raw = row.get("pe_ttm")
        pb_raw = row.get("pb")
        pe_ttm = float(pe_raw) if pe_raw is not None and str(pe_raw) not in ("", "nan") else None
        pb = float(pb_raw) if pb_raw is not None and str(pb_raw) not in ("", "nan") else None
        records.append((fmt_date(str(row["trade_date"])), code, pe_ttm, pb))

    count = upsert_valuation_history(records)
    logger.info("fetch_valuation: %s 写入 %d 条", ts_code, count)
    return count


# ── 核心逻辑 ─────────────────────────────────────────────────────────────────
def get_etf_adj_factors(pro, ts_code: str, limit: int) -> dict[str, float]:
    """
    拉取 ETF 复权因子表，返回 {trade_date_str: adj_factor} 字典。
    前复权计算：price_qfq = price_raw / adj_factor * latest_adj_factor
    若接口失败则返回空字典（调用方降级为不复权）。

    Tushare fund_daily 不支持 adj 参数，需手动用 fund_adj 复权因子计算前复权价格。
    """
    try:
        df = pro.fund_adj(ts_code=ts_code, limit=limit)
        if df is None or df.empty:
            return {}
        return {str(row["trade_date"]): float(row["adj_factor"]) for _, row in df.iterrows()}
    except Exception as e:
        logger.warning("fund_adj %s 失败，降级为不复权：%s", ts_code, e)
        return {}


def fetch_one(pro, conn: sqlite3.Connection, code: str, limit: int = FETCH_DAYS) -> int:
    """
    拉取单只股票或 ETF 的历史日线数据并写入 DB。
    - 个股：pro.daily(adj='qfq') 直接返回前复权价格
    - ETF：fund_daily 不支持 adj，手动用 fund_adj 复权因子换算前复权价格
    返回成功写入的记录条数，失败时抛出异常。
    """
    ts_code = to_ts_code(code)
    name = get_cached_name(conn, code)
    use_fund_api = is_etf(ts_code)
    api_name = "fund_daily" if use_fund_api else "daily"

    logger.info("拉取 %s (%s) via %s，limit=%d", ts_code, name or "未知", api_name, limit)
    if use_fund_api:
        df = pro.fund_daily(ts_code=ts_code, limit=limit)
        # fund_daily 不支持 adj 参数，手动拉取复权因子做前复权换算
        adj_factors = get_etf_adj_factors(pro, ts_code, limit)
        latest_adj = max(adj_factors.values()) if adj_factors else 1.0
        logger.info("%s 复权因子条数=%d，最新因子=%.4f", ts_code, len(adj_factors), latest_adj)
    else:
        df = pro.daily(ts_code=ts_code, limit=limit, adj='qfq')
        adj_factors = {}
        latest_adj = 1.0

    if df is None or df.empty:
        logger.warning("%s 返回空数据，可能停牌或代码有误", ts_code)
        return 0

    count = 0
    for _, row in df.iterrows():
        try:
            trade_date = str(row["trade_date"])
            close_raw = float(row["close"])
            high_raw  = float(row["high"])
            low_raw   = float(row["low"])
            open_raw  = float(row.get("open", row["close"]) or row["close"])
            amount = float(row.get("amount", 0) or 0)  # 千元
            vol    = float(row.get("vol", 0) or 0)     # 手

            # ETF 前复权换算：price_qfq = price_raw * (factor / latest_adj)
            # 逻辑：除权后 latest_adj 变大，历史 factor 较小，历史价格等比缩小，
            # 使历史价格与当前价格处于同一尺度。
            # 例：1拆3后 latest_adj=3，除权前 factor=1，历史价格 × (1/3) 对齐现价。
            if use_fund_api and adj_factors:
                factor = adj_factors.get(trade_date, latest_adj)
                ratio  = factor / latest_adj
                close = round(close_raw * ratio, 4)
                high  = round(high_raw  * ratio, 4)
                low   = round(low_raw   * ratio, 4)
                open_ = round(open_raw  * ratio, 4)
            else:
                close, high, low, open_ = close_raw, high_raw, low_raw, open_raw

            # 均价换算：千元→元，手→股（用原始价格计算，再同步复权）
            if vol > 0 and amount > 0:
                avg_price_raw = (amount * 1000) / (vol * 100)
                if use_fund_api and adj_factors:
                    factor = adj_factors.get(trade_date, latest_adj)
                    avg_price = round(avg_price_raw * (factor / latest_adj), 4)
                else:
                    avg_price = round(avg_price_raw, 4)
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
                avg_price=avg_price,
                amount=round(amount, 2),
                open=open_,
                vol=vol * 100 if row.get("vol") is not None else None,
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

        # ── 拉取技术指标（stk_factor_pro，需 5000 积分）────────────────────
        print("\n── 拉取技术指标（stk_factor_pro）──")
        logger.info("=== 开始拉取技术指标，共 %d 只 ===", len(codes))
        fac_success, fac_failed = 0, 0
        for code in codes:
            try:
                n = fetch_factors_one(pro, conn, code, limit=limit)
                fac_success += 1
                print(f"  ✓ {code}  更新 {n} 条技术指标")
            except Exception as e:
                fac_failed += 1
                logger.error("技术指标 %s 失败：%s", code, e)
                print(f"  ✗ {code}  技术指标失败：{e}")
            # stk_factor_pro 每分钟 30 次限制，保守间隔 2s
            time.sleep(2)

        fac_summary = f"技术指标完成：成功 {fac_success} 只，失败 {fac_failed} 只"
        logger.info("=== %s ===", fac_summary)
        print(f"\n{fac_summary}")

        # ── 拉取估值历史（daily_basic，pe_ttm/pb）────────────────────────────
        val_limit = VALUATION_HIST_LIMIT if args.backfill else 10
        val_label = f"回填 {val_limit} 日" if args.backfill else f"增量 {val_limit} 日"
        print(f"\n── 拉取估值历史（daily_basic · {val_label}）──")
        logger.info("=== 开始拉取估值历史，共 %d 只，limit=%d ===", len(codes), val_limit)
        val_success, val_failed = 0, 0
        for code in codes:
            try:
                n = fetch_valuation_one(pro, code, limit=val_limit)
                val_success += 1
                print(f"  ✓ {code}  写入 {n} 条估值")
            except Exception as e:
                val_failed += 1
                logger.error("估值 %s 失败：%s", code, e)
                print(f"  ✗ {code}  估值失败：{e}")
            time.sleep(0.3)

        val_summary = f"估值历史完成：成功 {val_success} 只，失败 {val_failed} 只"
        logger.info("=== %s ===", val_summary)
        print(f"\n{val_summary}")

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
