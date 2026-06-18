import time
import json
import math
import sqlite3

import tushare as ts

from core.config import (
    DB_PATH, logger, pro,
    AI_PROVIDER,
    CLAUDE_API_KEY, CLAUDE_MODEL, CLAUDE_BASE_URL,
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL, OPENAI_MAX_TOKENS,
    GEMINI_API_KEY, GEMINI_MODEL,
    COMMON_STOCKS,
)

MAX_TOOL_ROUNDS = 5

# ── 工具安全上限 ────────────────────────────────────────────────────────────────

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

# ── 板块指数代码 ────────────────────────────────────────────────────────────────

SECTOR_INDEX_CODES = {
    "801080.SI": "电子",    "801010.SI": "农林牧渔",  "801750.SI": "计算机",
    "801760.SI": "传媒",    "801770.SI": "通信",      "801050.SI": "有色金属",
    "801020.SI": "采掘",    "801030.SI": "化工",      "801110.SI": "家用电器",
    "801120.SI": "食品饮料","801150.SI": "医药生物",  "801160.SI": "公用事业",
    "801170.SI": "交通运输","801180.SI": "房地产",    "801190.SI": "银行",
    "801200.SI": "非银金融","801210.SI": "综合",      "801230.SI": "综合金融",
    "801710.SI": "建筑材料","801720.SI": "建筑装饰",  "801730.SI": "电气设备",
    "801740.SI": "国防军工","801880.SI": "汽车",      "801890.SI": "机械设备",
}

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

_INDEX_RT_CODES = [
    ("sh000001", "000001.SH"),
    ("399001",   "399001.SZ"),
    ("399006",   "399006.SZ"),
]


# ── 分时快照 ────────────────────────────────────────────────────────────────────

def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


def _is_trading_time() -> bool:
    import datetime
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    start        = datetime.time(9, 30)
    middle_end   = datetime.time(11, 30)
    middle_start = datetime.time(13, 0)
    end          = datetime.time(15, 0)
    return (start <= t <= middle_end) or (middle_start <= t <= end)


def _save_intraday_snapshot(code: str, today: str, now_hhmm: str,
                             price: float, open_: float, high: float, low: float,
                             cum_vol: float, cum_amount_qianyuan: float) -> None:
    if price == 0 or cum_vol == 0:
        return
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
                "INSERT OR REPLACE INTO intraday_snapshots "
                "(code, date, time, price, open, high, low, vol, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code, today, now_hhmm, price, open_, high, low, delta_vol, delta_amount),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("intraday_fetch: %s %s price=%.3f delta_vol=%.0f", code, now_hhmm, price, delta_vol)
    except Exception as e:
        logger.error("intraday_fetch: 写入失败 code=%s %s", code, e)


def _fetch_and_save_intraday_snapshots() -> None:
    if not COMMON_STOCKS:
        return
    import datetime
    today    = _today_str()
    now_hhmm = datetime.datetime.now().strftime("%H:%M")

    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM intraday_snapshots WHERE date != ?", (today,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("intraday_fetch: 清理旧数据失败 %s", e)

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
            cum_vol    = float(df.loc[0, "volume"])
            cum_amount = float(df.loc[0, "amount"]) / 1000.0
        except Exception as e:
            logger.warning("intraday_fetch: 解析行情失败 code=%s %s", code, e)
            continue
        _save_intraday_snapshot(code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)

    for rt_code, store_code in _INDEX_RT_CODES:
        try:
            df = ts.get_realtime_quotes(rt_code)
        except Exception as e:
            logger.warning("intraday_fetch: 指数失败 code=%s %s", rt_code, e)
            continue
        if df is None or df.empty:
            continue
        try:
            price      = float(df.loc[0, "price"])
            high       = float(df.loc[0, "high"])
            low        = float(df.loc[0, "low"])
            open_      = float(df.loc[0, "open"])
            cum_vol    = float(df.loc[0, "volume"])
            cum_amount = float(df.loc[0, "amount"]) / 1000.0
        except Exception as e:
            logger.warning("intraday_fetch: 解析指数失败 code=%s %s", rt_code, e)
            continue
        _save_intraday_snapshot(store_code, today, now_hhmm, price, open_, high, low, cum_vol, cum_amount)


def _intraday_bg_loop() -> None:
    logger.info("intraday_bg_loop: 后台线程已启动")
    while True:
        if _is_trading_time():
            logger.info("intraday_bg_loop: 开始抓取分时快照")
            _fetch_and_save_intraday_snapshots()
        time.sleep(1 * 60)


def _get_intraday_points(code: str) -> list:
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
        avg = round(cum_amount * 1000 / cum_vol, 4) if cum_vol > 0 else None
        points.append({"time": t, "price": round(price, 4), "avg": avg, "vol": vol or 0.0})
    return points


def _build_intraday_candles(points: list, window_minutes: int = 30) -> list:
    if not points:
        return []

    def _to_minutes(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    candles = []
    bucket_start  = None
    bucket_points = []

    for p in points:
        t_min = _to_minutes(p["time"])
        if bucket_start is None:
            offset       = t_min - 570
            bucket_idx   = offset // window_minutes
            bucket_start = 570 + bucket_idx * window_minutes
        elif t_min >= bucket_start + window_minutes:
            if bucket_points:
                prices = [x["price"] for x in bucket_points]
                candles.append({
                    "window_start": f"{bucket_start // 60:02d}:{bucket_start % 60:02d}",
                    "open":  bucket_points[0]["price"],
                    "close": bucket_points[-1]["price"],
                    "high":  max(prices),
                    "low":   min(prices),
                    "vol":   sum(x["vol"] for x in bucket_points),
                    "n":     len(bucket_points),
                })
            offset       = t_min - 570
            bucket_idx   = offset // window_minutes
            bucket_start = 570 + bucket_idx * window_minutes
            bucket_points = []
        bucket_points.append(p)

    if bucket_points:
        prices = [x["price"] for x in bucket_points]
        candles.append({
            "window_start": f"{bucket_start // 60:02d}:{bucket_start % 60:02d}",
            "open":  bucket_points[0]["price"],
            "close": bucket_points[-1]["price"],
            "high":  max(prices),
            "low":   min(prices),
            "vol":   sum(x["vol"] for x in bucket_points),
            "n":     len(bucket_points),
        })
    return candles


# ── 揉搓线分析 ──────────────────────────────────────────────────────────────────

def analyze_rousu_lines(records: list, n: int = 10, label: str = "日K") -> list:
    INTERPRETATIONS = {
        ("下跌趋势", "黑K", "下影接上影"): "中继下跌",
        ("下跌趋势", "红K", "下影接上影"): "支撑位震荡选方向",
        ("下跌趋势", "红K", "上影接下影"): "支撑位资金抢反弹",
        ("下跌趋势", "黑K", "上影接下影"): "短期止跌",
        ("上涨趋势", "黑K", "下影接上影"): "开始有分歧",
        ("上涨趋势", "红K", "下影接上影"): "分歧但强势继续看新高",
        ("上涨趋势", "红K", "上影接下影"): "承接力度大，但只承接不追高",
        ("上涨趋势", "黑K", "上影接下影"): "承接低，可能出现短期顶",
    }
    results = []
    target = records[:n]
    for i, rec in enumerate(target):
        try:
            o = float(rec["open"]); c = float(rec["close"])
            h = float(rec["high"]); lo = float(rec["low"])
        except (KeyError, TypeError, ValueError):
            continue
        candle_range = h - lo
        if candle_range < 1e-6:
            continue
        body_ratio         = abs(c - o) / candle_range
        upper_shadow_ratio = (h - max(o, c)) / candle_range
        lower_shadow_ratio = (min(o, c) - lo) / candle_range
        if not (body_ratio < 0.4 and upper_shadow_ratio > 0.2 and lower_shadow_ratio > 0.2):
            continue
        prior_slice = records[i + 1: i + 6]
        if len(prior_slice) < 5:
            continue
        prior_closes = []
        for r in prior_slice:
            try:
                prior_closes.append(float(r["close"]))
            except (KeyError, TypeError, ValueError):
                pass
        if len(prior_closes) < 3:
            continue
        prior_avg    = sum(prior_closes) / len(prior_closes)
        trend        = "上涨趋势" if c > prior_avg else "下跌趋势"
        color        = "红K" if c >= o else "黑K"
        open_position = (o - lo) / candle_range
        shadow_order  = "下影接上影" if open_position > 0.5 else "上影接下影"
        interpretation = INTERPRETATIONS.get((trend, color, shadow_order), "未知形态")
        date_key = rec.get("date") or rec.get("window_start") or f"index-{i}"
        results.append({
            "date":           date_key,
            "label":          label,
            "trend":          trend,
            "color":          color,
            "shadow_order":   shadow_order,
            "interpretation": interpretation,
            "body_ratio":     round(body_ratio, 3),
            "open_position":  round(open_position, 3),
        })
    return results


def analyze_rousu_lines_intraday(code: str, date: str = None) -> list:
    if not date:
        date = _today_str()
    bare_code = code.split(".")[0]
    rows = []
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT time, price, vol, amount FROM intraday_snapshots "
                "WHERE code = ? AND date = ? ORDER BY time ASC",
                (bare_code, date),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error("analyze_rousu_lines_intraday: DB error %s", e)
        return []

    if not rows:
        try:
            conn = sqlite3.connect(DB_PATH)
            try:
                cur = conn.execute(
                    "SELECT time, price, vol, amount FROM intraday_snapshots "
                    "WHERE code = ? AND date = ? ORDER BY time ASC",
                    (code, date),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
        except Exception as e:
            logger.error("analyze_rousu_lines_intraday: DB error (full code) %s", e)
            return []

    if not rows:
        return []

    points  = [{"time": r[0], "price": r[1], "vol": r[2] or 0.0, "amount": r[3] or 0.0} for r in rows]
    candles = _build_intraday_candles(points, window_minutes=30)
    if not candles:
        return []
    return analyze_rousu_lines(list(reversed(candles)), n=len(candles), label="30分钟K")


def _get_daily_records_for_rousu(code: str, n: int = 15) -> list:
    short_code = code.split(".")[0]
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT date, COALESCE(open, close) AS open, close, high, low "
            "FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT ?",
            (short_code, n),
        ).fetchall()
        conn.close()
        return [{"date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4]} for r in rows]
    except Exception:
        return []


# ── 技术指标计算 ────────────────────────────────────────────────────────────────

def _calc_macd(code: str) -> dict | None:
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date ASC LIMIT 90", (q,)
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
    hist  = [dif[i] - dea[i] for i in range(n)]

    latest = {"date": dates[-1], "dif": round(dif[-1], 4), "dea": round(dea[-1], 4), "hist": round(hist[-1], 4)}

    WIN   = min(40, n)
    seg_c = closes[-WIN:]
    seg_h = hist[-WIN:]
    seg_d = dates[-WIN:]

    def find_peaks(arr, mode="high"):
        pts = []
        for i in range(2, len(arr) - 2):
            if mode == "high":
                if arr[i] > arr[i-1] and arr[i] > arr[i-2] and arr[i] > arr[i+1] and arr[i] > arr[i+2]:
                    pts.append(i)
            else:
                if arr[i] < arr[i-1] and arr[i] < arr[i-2] and arr[i] < arr[i+1] and arr[i] < arr[i+2]:
                    pts.append(i)
        return pts

    divergence = None
    div_detail = ""
    peak_idx = find_peaks(seg_c, "high")
    if len(peak_idx) >= 2:
        i1, i2 = peak_idx[-2], peak_idx[-1]
        if seg_c[i2] > seg_c[i1] and seg_h[i2] < seg_h[i1]:
            divergence = "top"
            div_detail = (f"价格高点 {seg_d[i1]}({seg_c[i1]:.2f}) → {seg_d[i2]}({seg_c[i2]:.2f}) 创新高，"
                          f"但 MACD 柱 {seg_h[i1]:.4f} → {seg_h[i2]:.4f} 未同步新高，上涨动能衰竭，警惕回调。")

    if divergence is None:
        trough_idx = find_peaks(seg_c, "low")
        if len(trough_idx) >= 2:
            i1, i2 = trough_idx[-2], trough_idx[-1]
            if seg_c[i2] < seg_c[i1] and seg_h[i2] > seg_h[i1]:
                divergence = "bottom"
                div_detail = (f"价格低点 {seg_d[i1]}({seg_c[i1]:.2f}) → {seg_d[i2]}({seg_c[i2]:.2f}) 创新低，"
                              f"但 MACD 柱 {seg_h[i1]:.4f} → {seg_h[i2]:.4f} 未同步新低，下跌动能衰竭，关注反弹机会。")

    cross = None
    for i in range(n - 1, max(n - 10, 0), -1):
        if hist[i] > 0 and hist[i - 1] <= 0:
            cross = {"type": "golden", "date": dates[i], "label": "金叉（DIF上穿DEA）"}
            break
        if hist[i] < 0 and hist[i - 1] >= 0:
            cross = {"type": "dead", "date": dates[i], "label": "死叉（DIF下穿DEA）"}
            break

    return {
        "latest": latest, "divergence": divergence, "div_detail": div_detail,
        "cross": cross, "above_zero": dif[-1] > 0,
        "series": {
            "dates": dates[-20:],
            "dif":   [round(v, 4) for v in dif[-20:]],
            "dea":   [round(v, 4) for v in dea[-20:]],
            "hist":  [round(v, 4) for v in hist[-20:]],
        },
    }


def _get_benchmark_index(short_code: str) -> str:
    if short_code.startswith("60"):
        return "000001.SH"
    elif short_code.startswith("00"):
        return "399001.SZ"
    elif short_code.startswith("30"):
        return "399006.SZ"
    elif short_code.startswith("68"):
        return "000688.SH"
    return "000001.SH"


def _calc_yidong(code: str, current_price: float) -> dict | None:
    short_code = code.split(".")[0] if "." in code else code
    ts_code    = code if "." in code else None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q in ([ts_code, short_code] if ts_code else [short_code]):
            if q is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code=? ORDER BY date DESC LIMIT 31", (q,)
            )
            rows = cur.fetchall()
            if rows:
                break
        if not rows or len(rows) < 31:
            conn.close()
            return None
        base_date, base_close = rows[-1][0], float(rows[-1][1])
        if base_close <= 0:
            conn.close()
            return None

        index_code    = _get_benchmark_index(short_code)
        index_base    = None
        index_current = None
        idx_rows = conn.execute(
            "SELECT date, close FROM daily_records WHERE code=? ORDER BY date DESC LIMIT 31", (index_code,)
        ).fetchall()
        conn.close()
        if idx_rows and len(idx_rows) >= 31:
            index_current = float(idx_rows[0][1])
            index_base    = float(idx_rows[-1][1])
    except Exception:
        return None

    r_stock = round((current_price - base_close) / base_close * 100, 4)
    if index_base and index_base > 0 and index_current and index_current > 0:
        r_index     = round((index_current - index_base) / index_base * 100, 4)
        deviation   = round(r_stock - r_index, 4)
        yidong_line = round(base_close * (1 + (2.0 + r_index / 100)), 4)
        fallback    = False
    else:
        r_index     = None
        deviation   = r_stock
        yidong_line = round(base_close * 3.0, 4)
        index_base  = None
        index_current = None
        fallback    = True

    pct_to_line = round((current_price - yidong_line) / yidong_line * 100, 2)
    if fallback:
        alert     = current_price >= yidong_line * 0.9
        triggered = current_price >= yidong_line
    else:
        alert     = deviation >= 180.0
        triggered = deviation >= 200.0

    limit_rate           = 0.20 if short_code.startswith("68") else 0.10
    next_day_gap_pct     = round((yidong_line - current_price) / current_price * 100, 2)
    next_day_limit_price = round(current_price * (1 + limit_rate), 2)
    boards_needed = math.ceil(math.log(yidong_line / current_price) / math.log(1 + limit_rate)) \
        if yidong_line > current_price and current_price > 0 else 0

    return {
        "base_date": base_date, "base_close": round(base_close, 4),
        "yidong_line": yidong_line, "pct_to_line": pct_to_line,
        "r_stock": r_stock, "r_index": r_index, "deviation": round(deviation, 2),
        "index_code": index_code,
        "index_base":    round(index_base, 2) if index_base else None,
        "index_current": round(index_current, 2) if index_current else None,
        "alert": alert, "triggered": triggered, "fallback": fallback,
        "next_day_gap_pct": next_day_gap_pct,
        "next_day_limit_price": next_day_limit_price,
        "next_day_boards_needed": boards_needed,
        "limit_rate": limit_rate,
    }


def _calc_boll(code: str) -> dict:
    ts_code    = code if "." in code else None
    short_code = code.split(".")[0] if "." in code else code
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = None
        for q_code in ([ts_code, short_code] if ts_code else [short_code]):
            if q_code is None:
                continue
            cur = conn.execute(
                "SELECT date, close FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT 60", (q_code,)
            )
            rows = cur.fetchall()
            if rows:
                break
        conn.close()
    except Exception:
        return None

    if not rows or len(rows) < 5:
        return None

    rows   = list(reversed(rows))
    closes = [r[1] for r in rows if r[1] is not None]
    n      = len(closes)

    def sma(data, period):
        return round(sum(data[-period:]) / period, 4) if len(data) >= period else None

    def boll_calc(data, period=20, k=2.0):
        if len(data) < period:
            return None, None, None
        w   = data[-period:]
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
            position, advice_class = "上轨附近（超买，注意压力）", "danger"
        elif latest <= lower:
            position, advice_class = "下轨附近（超卖，关注支撑）", "success"
        elif latest > mid:
            position, advice_class = "中轨↑上轨（强势区间）", "warning"
        else:
            position, advice_class = "下轨↑中轨（弱势区间）", "secondary"
    else:
        position, advice_class = "数据不足", "secondary"

    all_closes      = closes
    ma_series_dates = []
    ma_series_ma5   = []
    ma_series_ma10  = []
    ma_series_ma20  = []
    for i in range(max(0, n - 20), n):
        ma_series_dates.append(rows[i][0])
        offset = i + 1
        ma_series_ma5.append(round(sum(all_closes[max(0, offset - 5):offset]) / min(5, offset), 4))
        ma_series_ma10.append(round(sum(all_closes[max(0, offset - 10):offset]) / min(10, offset), 4))
        ma_series_ma20.append(round(sum(all_closes[max(0, offset - 20):offset]) / min(20, offset), 4))

    return {
        "upper": upper, "mid": mid, "lower": lower,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "position": position, "advice_class": advice_class, "data_points": n,
        "recent_closes": [{"date": rows[i][0], "close": rows[i][1]} for i in range(max(0, n - 20), n)],
        "ma_series": {"dates": ma_series_dates, "ma5": ma_series_ma5, "ma10": ma_series_ma10, "ma20": ma_series_ma20},
    }
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
                max_tokens=OPENAI_MAX_TOKENS,
                tools=openai_tools,
                messages=messages,
            )
            choice = resp.choices[0]
            logger.info("openai tool_use round=%d finish_reason=%s", _round, choice.finish_reason)

            if choice.finish_reason == "length":
                logger.warning("openai tool_use round=%d truncated by max_tokens=%d", _round, OPENAI_MAX_TOKENS)
                return choice.message.content or ""

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


def call_ai_model_streaming(system_prompt: str, messages: list):
    """
    带工具调用的流式生成器。
    工具调用轮次推送 SSE progress 事件，最终文本逐 token 推送 token 事件。
    messages: OpenAI 格式的消息列表（不含 system），支持多轮对话。
    最终 yield ("done", full_text) 表示完成，full_text 是完整助手回复。
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

        # 转换 messages 为 Claude 格式（claude 的 system 单独传，messages 只含 user/assistant）
        claude_messages = []
        for m in messages:
            role = m["role"]
            content = m["content"]
            if role in ("user", "assistant"):
                claude_messages.append({"role": role, "content": content})

        for _round in range(MAX_TOOL_ROUNDS):
            yield ("progress", f"AI 第 {_round + 1} 轮推理中，请稍候…")
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=claude_tools,
                messages=claude_messages,
            )
            logger.info("claude streaming round=%d stop_reason=%s", _round, resp.stop_reason)
            response_content = resp.content or []

            if resp.stop_reason == "end_turn":
                # 最终文本：逐字符模拟流式（Claude 同步 API 不直接流式，整块文本切片推送）
                full_text = "".join(b.text for b in response_content if hasattr(b, "text"))
                chunk_size = 4
                for i in range(0, len(full_text), chunk_size):
                    yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

            if resp.stop_reason == "tool_use":
                tool_names = [b.name for b in response_content if b.type == "tool_use"]
                yield ("progress", f"AI 决定调用 {len(tool_names)} 个工具：{', '.join(tool_names)}")
                claude_messages.append({"role": "assistant", "content": response_content})
                tool_results = []
                for block in response_content:
                    if block.type == "tool_use":
                        yield ("progress", f"正在获取数据：{block.name}…")
                        result_str = execute_tool(block.name, block.input)
                        yield ("progress", f"{block.name} 数据就绪，等待下一轮推理…")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })
                claude_messages.append({"role": "user", "content": tool_results})
            else:
                full_text = "".join(b.text for b in response_content if hasattr(b, "text"))
                if full_text:
                    chunk_size = 4
                    for i in range(0, len(full_text), chunk_size):
                        yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

        logger.warning("claude streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        final_content = resp.content or []
        full_text = "".join(b.text for b in final_content if hasattr(b, "text"))
        yield ("done", full_text)

    # ── OpenAI ───────────────────────────────────────────────────────────────
    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": OPENAI_API_KEY, "timeout": 180.0}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        openai_tools = _build_openai_tools()

        oai_messages = [{"role": "system", "content": system_prompt}] + list(messages)

        for _round in range(MAX_TOOL_ROUNDS):
            yield ("progress", f"AI 第 {_round + 1} 轮推理中，请稍候…")
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=OPENAI_MAX_TOKENS,
                tools=openai_tools,
                messages=oai_messages,
            )
            choice = resp.choices[0]
            logger.info("openai streaming round=%d finish_reason=%s", _round, choice.finish_reason)

            if choice.finish_reason == "length":
                logger.warning("openai streaming round=%d truncated by max_tokens=%d", _round, OPENAI_MAX_TOKENS)
                full_text = choice.message.content or ""
                chunk_size = 4
                for i in range(0, len(full_text), chunk_size):
                    yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

            if choice.finish_reason == "stop":
                full_text = choice.message.content or ""
                chunk_size = 4
                for i in range(0, len(full_text), chunk_size):
                    yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

            if choice.finish_reason == "tool_calls":
                msg = choice.message
                tool_names = [tc.function.name for tc in msg.tool_calls]
                yield ("progress", f"AI 决定调用 {len(tool_names)} 个工具：{', '.join(tool_names)}")
                oai_messages.append(msg.model_dump())
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    yield ("progress", f"正在获取数据：{tc.function.name}…")
                    result_str = execute_tool(tc.function.name, args)
                    yield ("progress", f"{tc.function.name} 数据就绪，等待下一轮推理…")
                    oai_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
            else:
                full_text = choice.message.content or ""
                chunk_size = 4
                for i in range(0, len(full_text), chunk_size):
                    yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

        logger.warning("openai streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        full_text = resp.choices[0].message.content or ""
        yield ("done", full_text)

    # ── Gemini ───────────────────────────────────────────────────────────────
    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_tool = _build_gemini_tools()
        model_obj = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt,
            tools=[gemini_tool],
        )
        chat = model_obj.start_chat()

        # 重放历史（除最后一条 user 消息）
        history_msgs = list(messages)
        last_user = None
        for i in range(len(history_msgs) - 1, -1, -1):
            if history_msgs[i]["role"] == "user":
                last_user = history_msgs.pop(i)
                break
        for m in history_msgs:
            role = "user" if m["role"] == "user" else "model"
            try:
                chat.send_message(m["content"], generation_config=genai_types.GenerationConfig(max_output_tokens=1))
            except Exception:
                pass

        user_content = last_user["content"] if last_user else ""
        yield ("progress", "AI 第 1 轮推理中，请稍候…")
        resp = chat.send_message(
            user_content,
            generation_config=genai_types.GenerationConfig(max_output_tokens=MAX_TOKENS),
            request_options={"timeout": 180},
        )

        for _round in range(MAX_TOOL_ROUNDS):
            fc_parts = [p for p in resp.parts if p.function_call.name]
            logger.info("gemini streaming round=%d fc_count=%d", _round, len(fc_parts))

            if not fc_parts:
                full_text = resp.text
                chunk_size = 4
                for i in range(0, len(full_text), chunk_size):
                    yield ("token", full_text[i:i+chunk_size])
                yield ("done", full_text)
                return

            tool_names = [p.function_call.name for p in fc_parts]
            yield ("progress", f"AI 决定调用 {len(tool_names)} 个工具：{', '.join(tool_names)}")
            fn_responses = []
            for part in fc_parts:
                fc = part.function_call
                yield ("progress", f"正在获取数据：{fc.name}…")
                result_str = execute_tool(fc.name, dict(fc.args))
                yield ("progress", f"{fc.name} 数据就绪，等待下一轮推理…")
                fn_responses.append(
                    genai_types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result_str},
                    )
                )
            yield ("progress", f"AI 第 {_round + 2} 轮推理中，请稍候…")
            resp = chat.send_message(
                fn_responses,
                generation_config=genai_types.GenerationConfig(max_output_tokens=MAX_TOKENS),
                request_options={"timeout": 180},
            )

        logger.warning("gemini streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        full_text = resp.text
        yield ("done", full_text)

    else:
        yield ("error", f"不支持的 AI_PROVIDER: {provider}")
        yield ("done", "")


def _save_ai_conversation(session_id: str, stock_code: str, messages: list) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO ai_conversations(session_id, stock_code, messages, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET messages=excluded.messages, updated_at=excluded.updated_at
            """,
            (session_id, stock_code, json.dumps(messages, ensure_ascii=False), now, now),
        )
        # 清理 2 小时前的会话
        conn.execute("DELETE FROM ai_conversations WHERE updated_at < ?", (now - 7200,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("_save_ai_conversation failed: %s", e)


def _load_ai_conversation(session_id: str) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT messages FROM ai_conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.error("_load_ai_conversation failed: %s", e)
    return []


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


