"""
services/tushare_tools.py — Tushare 工具定义、执行层及 AI provider 格式构建器
"""
import json

from core.config import logger, pro
from services.indicators import (
    _get_intraday_points,
    _today_str,
    _calc_boll,
)

MAX_TOOL_ROUNDS = 5

TOOL_DEFINITIONS = [
    {
        "name": "get_intraday_lines",
        "description": (
            "获取个股今日分时数据，包含白线（当前价）、黄线（分时均价/资金成本重心）和每时段量能节奏（vol_ratio=相对均量倍数）。"
            "黄白线解读规则（参考 Skill 05）：\n"
            "  - 黄线持续高于白线：大资金净流入，多头主导，可加仓\n"
            "  - 白线持续高于黄线：小盘/个股活跃但大资金偏弱，谨慎追高\n"
            "  - 白线主动下穿黄线（化解放量智障）：散户未大量进场，行情可延续\n"
            "  - 黄线快速下穿白线 + vol_ratio>1.5：顶部信号，停止加仓并逢高减仓\n"
            "  - 缩量（vol_ratio<0.5）+ 白线在上 + 过开盘点：顶级诱多走势，卖点\n"
            "  - 放量（vol_ratio>1.5）不过新高：上方压力大，主力出货\n"
            "每30分钟一个采样点，仅交易时段（09:30-15:00）有数据。"
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

# ── 常量 ─────────────────────────────────────────────────────────────────────

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


# ── 工具实现 ─────────────────────────────────────────────────────────────────

def _tool_get_intraday_lines(ts_code: str) -> dict:
    code = ts_code.split(".")[0]
    points = _get_intraday_points(code)
    if not points:
        return {"error": "暂无分时数据，后台任务尚未抓取（交易时段每30分钟更新一次）"}

    vols = [p["vol"] for p in points if p["vol"] > 0]
    avg_vol = sum(vols) / len(vols) if vols else 0
    annotated = []
    for p in points:
        vol_ratio = round(p["vol"] / avg_vol, 2) if avg_vol > 0 else None
        annotated.append({
            "time":      p["time"],
            "price":     p["price"],
            "avg":       p["avg"],
            "vol":       p["vol"],
            "vol_ratio": vol_ratio,
        })

    latest = annotated[-1]
    white_vs_yellow = (
        "白线在上" if (latest["price"] and latest["avg"] and latest["price"] > latest["avg"])
        else "黄线在上"
    )
    return {
        "ts_code":        ts_code,
        "date":           _today_str(),
        "latest_price":   latest["price"],
        "latest_avg":     latest["avg"],
        "white_vs_yellow": white_vs_yellow,
        "points":         annotated,
        "note": (
            "price=白线（当前价），avg=黄线（分时均价/资金成本重心）；"
            "vol_ratio=当前时段成交量/全天均量，>1.5为放量，<0.5为缩量；"
            "每30分钟更新一次。"
        ),
    }


def _tool_get_index_intraday() -> dict:
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

        vols = [p["vol"] for p in points if p["vol"] > 0]
        avg_vol = sum(vols) / len(vols) if vols else 0

        annotated = []
        for p in points:
            vol_ratio = round(p["vol"] / avg_vol, 2) if avg_vol > 0 else None
            annotated.append({
                "time":      p["time"],
                "price":     p["price"],
                "avg":       p["avg"],
                "vol":       p["vol"],
                "vol_ratio": vol_ratio,
            })

        latest_a = annotated[-1]
        white_vs_yellow = (
            "白线在上" if (latest_a["price"] and latest_a["avg"] and latest_a["price"] > latest_a["avg"])
            else "黄线在上"
        )
        result[store_code] = {
            "name":            name,
            "date":            today,
            "latest_price":    latest_a["price"],
            "latest_avg":      latest_a["avg"],
            "white_vs_yellow": white_vs_yellow,
            "points":          annotated,
        }

    return {
        "indexes": result,
        "note": (
            "price=白线（当前价），avg=黄线（分时均价/资金成本重心）；"
            "价格持续高于黄线为多头主导，低于黄线为空头主导；"
            "vol_ratio>1.5为放量时段，<0.5为缩量时段；每3分钟更新一次。"
        ),
    }


def _tool_get_moneyflow(ts_code: str, trade_date: str = "") -> dict:
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
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
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
    boll_data = _calc_boll(ts_code)
    if boll_data is None:
        return {"error": "暂无历史K线数据，请先运行 fetch_history.py 拉取数据"}
    closes     = [r["close"] for r in boll_data["recent_closes"]]
    n          = boll_data["data_points"]
    boll_upper = boll_data["upper"]
    boll_mid   = boll_data["mid"]
    boll_lower = boll_data["lower"]
    ma5        = boll_data["ma5"]
    ma10       = boll_data["ma10"]
    ma20       = boll_data["ma20"]
    latest_close = closes[-1] if closes else None
    latest_date  = boll_data["recent_closes"][-1]["date"] if boll_data["recent_closes"] else ""
    ma_trend = "数据不足"
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            ma_trend = "多头排列（MA5>MA10>MA20，趋势向上）"
        elif ma5 < ma10 < ma20:
            ma_trend = "空头排列（MA5<MA10<MA20，趋势向下）"
        else:
            ma_trend = "均线纠缠（无明确趋势）"
    boll_position = boll_data["position"]
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
        "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "trend": ma_trend},
        "boll": {"upper": boll_upper, "mid": boll_mid, "lower": boll_lower, "position": boll_position},
        "recent_closes": boll_data["recent_closes"],
        "note": "BOLL参数：20日，2倍标准差；均线：简单移动平均",
    }


def _tool_get_margin_data() -> dict:
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取融资融券数据"}
    try:
        from datetime import datetime, timedelta
        end = datetime.today()
        start = end - timedelta(days=20)
        df = pro.margin(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        return {"error": f"margin 调用失败: {e}"}
    if df is None or df.empty:
        return {"error": "暂无融资融券数据"}
    try:
        agg = (
            df.groupby("trade_date")[["rzye", "rqye", "rzrqye"]]
            .sum()
            .reset_index()
            .sort_values("trade_date", ascending=False)
            .head(10)
        )
        records = agg.to_dict("records")
        for i in range(len(records) - 1):
            prev_rzye = records[i + 1].get("rzye", 0)
            curr_rzye = records[i].get("rzye", 0)
            if prev_rzye and prev_rzye != 0:
                records[i]["rzye_chg_pct"] = round((curr_rzye - prev_rzye) / prev_rzye * 100, 2)
            else:
                records[i]["rzye_chg_pct"] = None
        if records:
            records[-1]["rzye_chg_pct"] = None
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


def _tool_get_sector_flow(trade_date: str = "") -> dict:
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取板块数据"}
    from datetime import datetime, timedelta
    if not trade_date:
        trade_date = datetime.today().strftime("%Y%m%d")
    start_date = (datetime.today() - timedelta(days=10)).strftime("%Y%m%d")
    try:
        all_rows = []
        for ts_code in SECTOR_INDEX_CODES:
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
                continue
        if not all_rows:
            return {"error": "未能获取任何板块数据，可能需要更高 Tushare 权限"}
        import pandas as pd
        combined = pd.concat(all_rows, ignore_index=True)
        latest_date = combined["trade_date"].max()
        latest = combined[combined["trade_date"] == latest_date].copy()
        latest = latest.sort_values("pct_chg", ascending=False)
        top5_up   = latest.head(5)[["sector_name", "ts_code", "close", "pct_chg", "amount"]].to_dict("records")
        top5_down = latest.tail(5)[["sector_name", "ts_code", "close", "pct_chg", "amount"]].to_dict("records")
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
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取期指数据"}
    from datetime import datetime, timedelta
    end = datetime.today()
    start = end - timedelta(days=14)
    try:
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
        agg = (
            df.groupby("trade_date")[["long_hld", "short_hld"]]
            .sum()
            .reset_index()
            .sort_values("trade_date", ascending=False)
            .head(5)
        )
        records = agg.to_dict("records")
        for r in records:
            r["net_long"] = round(r.get("long_hld", 0) - r.get("short_hld", 0), 0)
        trend = "数据不足"
        if len(records) >= 2:
            net_latest = records[0].get("net_long", 0)
            net_prev   = records[1].get("net_long", 0)
            if net_latest > net_prev:
                trend = "净多头增加（机构偏多，看涨信号）"
            elif net_latest < net_prev:
                trend = "净多头减少（机构偏空，注意风险）"
            else:
                trend = "持仓变化不明显"
        return {
            "symbol": "IF（沪深300股指期货）",
            "records": records,
            "trend": trend,
            "note": "long_hld=多头持仓量，short_hld=空头持仓量，net_long=净多头（多-空）",
        }
    except Exception as e:
        return {"error": f"数据处理失败: {e}"}


def _tool_get_disclosure_calendar(ts_code: str) -> dict:
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取财报日历"}
    from datetime import datetime, timedelta
    today = datetime.today()
    start = (today - timedelta(days=30)).strftime("%Y%m%d")
    end   = (today + timedelta(days=90)).strftime("%Y%m%d")
    try:
        df = pro.disclosure_date(ts_code=ts_code, start_date=start, end_date=end)
    except Exception as e:
        return {"error": f"disclosure_date 调用失败: {e}"}
    if df is None or df.empty:
        return {"ts_code": ts_code, "records": [], "note": "未查到近期财报披露计划"}
    fields = ["ann_date", "end_date", "pre_date", "actual_date", "modify_date"]
    available = [f for f in fields if f in df.columns]
    records = df[available].sort_values("end_date", ascending=False).to_dict("records")
    today_str = today.strftime("%Y%m%d")
    upcoming = [r for r in records if r.get("pre_date", "") >= today_str or r.get("actual_date", "") >= today_str]
    warning = None
    if upcoming:
        next_report = upcoming[0]
        pre_date = next_report.get("pre_date") or next_report.get("actual_date", "")
        if pre_date:
            days_left = (datetime.strptime(pre_date, "%Y%m%d") - today).days
            if days_left <= 14:
                warning = f"距下次财报披露仅剩 {days_left} 天（{pre_date}），建议控制仓位"
            else:
                warning = f"下次财报披露预计 {pre_date}，距今 {days_left} 天"
    return {
        "ts_code": ts_code,
        "records": records[:6],
        "upcoming_warning": warning,
        "note": "ann_date=公告日，end_date=报告期，pre_date=预计披露日，actual_date=实际披露日",
    }


def _tool_get_share_reduction(ts_code: str) -> dict:
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取增减持数据"}
    from datetime import datetime, timedelta
    end   = datetime.today()
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
    in_de_col = "in_de" if "in_de" in df.columns else None
    reduction_count = increase_count = 0
    if in_de_col:
        reduction_count = int((df[in_de_col] == "减持").sum())
        increase_count  = int((df[in_de_col] == "增持").sum())
    summary = f"近90天：增持{increase_count}次，减持{reduction_count}次"
    if reduction_count > increase_count:
        signal = "减持次数多于增持，注意大股东出货风险"
    elif increase_count > reduction_count:
        signal = "增持次数多于减持，大股东看多信号"
    else:
        signal = "增减持持平或无记录"
    return {
        "ts_code": ts_code,
        "summary": summary,
        "signal": signal,
        "records": records,
        "note": "in_de=增持/减持，change_vol=变动股数，change_ratio=变动比例，avg_price=均价",
    }


def _tool_get_etf_flow() -> dict:
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN，无法获取ETF数据"}
    from datetime import datetime, timedelta
    end   = datetime.today()
    start = end - timedelta(days=10)
    try:
        etf_results = []
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
        etf_results.sort(key=lambda x: x.get("amount_5d_yi") or 0, reverse=True)
        hs300_etf  = next((e for e in etf_results if e["ts_code"] == "510300.SH"), None)
        gjd_signal = "无明显GJD信号"
        if hs300_etf and hs300_etf.get("amount_5d_yi"):
            if hs300_etf["amount_5d_yi"] > 50:
                gjd_signal = f"沪深300ETF近5日成交额合计{hs300_etf['amount_5d_yi']}亿，资金关注度较高，可能有GJD介入"
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
    if pro is None:
        return {"error": "未配置 TUSHARE_TOKEN"}
    try:
        import datetime
        end   = datetime.date.today().strftime("%Y%m%d")
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


# ── 执行入口 ─────────────────────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_args: dict) -> str:
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


# ── AI provider 工具格式构建器 ────────────────────────────────────────────────

def _build_claude_tools() -> list:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": {
                "type": "object",
                "properties": {k: v for k, v in t["parameters"].items()},
                "required": t["required"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]


def _build_openai_tools() -> list:
    return [
        {
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
        }
        for t in TOOL_DEFINITIONS
    ]


def _build_gemini_tools():
    import google.generativeai as genai
    declarations = []
    for t in TOOL_DEFINITIONS:
        props = {
            param_name: genai.types.Schema(
                type=genai.types.Type.STRING,
                description=param_info.get("description", ""),
            )
            for param_name, param_info in t["parameters"].items()
        }
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
