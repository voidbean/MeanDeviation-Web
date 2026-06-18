from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import threading
import asyncio
import tushare as ts
import os
import sqlite3
import time
import json
from dotenv import load_dotenv
from pathlib import Path

import config as _cfg
from config import logger, DB_PATH, SKILLS_DIR, pro
from db import (
    init_db,
    get_cached_name, set_cached_name,
    save_daily_record, get_portfolio, save_portfolio,
    save_query_history, get_query_history,
    save_temp_result, load_temp_result,
    get_n_day_stats, calc_atr,
    get_index_market_data, get_index_trend_chart_data,
    get_klines_around_date,
    save_ai_conversation, load_ai_conversation,
)
from tools import (
    _intraday_bg_loop,
    analyze_rousu_lines, analyze_rousu_lines_intraday,
    _calc_macd, _calc_boll, _calc_yidong,
    _get_intraday_points, _build_intraday_candles,
    _get_daily_records_for_rousu,
    call_ai_model, call_ai_model_with_tools, call_ai_model_streaming,
    _save_ai_conversation, _load_ai_conversation,
)

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化 DB 并开启后台分时快照抓取线程。"""
    init_db()
    t = threading.Thread(target=_intraday_bg_loop, daemon=True, name="intraday-fetcher")
    t.start()
    logger.info("lifespan: 后台分时快照线程已启动")
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

# 热切换时直接写 _cfg.AI_PROVIDER，整个进程共享同一个模块对象
AI_PROVIDER = _cfg.AI_PROVIDER



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
    """读取 skills/*.md 和 skills/personal/*.md，拼接为字符串。
    personal/ 内容置于末尾，AI 更容易记住个人画像。"""
    if not SKILLS_DIR.exists():
        return ""
    parts = []
    # 通用体系
    for f in sorted(SKILLS_DIR.glob("*.md")):
        parts.append(f"## {f.name}\n\n" + f.read_text(encoding="utf-8"))
    # 个人画像（子目录）
    personal_dir = SKILLS_DIR / "personal"
    if personal_dir.exists():
        personal_files = sorted(personal_dir.glob("*.md"))
        if personal_files:
            parts.append("## 个人交易画像（优先参考）")
            for f in personal_files:
                parts.append(f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)



def build_review_prompt(trade: dict, stock_klines: list, index_klines: list) -> tuple:
    """构建单笔复盘的 system_prompt 和 user_prompt"""
    skills_text = load_skills()

    system_prompt = (
        "你是一位严格的交易复盘导师，使用阿狼交易体系标准进行复盘分析。\n\n"
        + skills_text
        + "\n\n## 复盘分析框架\n\n"
        "对每笔操作，从以下维度评判：\n"
        "1. **趋势判断**：操作时处于什么趋势阶段？均线系统是否支持该方向？\n"
        "2. **时机选择**：买卖点是否符合阿狼体系信号标准？\n"
        "3. **大盘环境**：操作当日大盘状态？是否逆势操作？\n"
        f"4. **情绪影响**：情绪状态（{trade['emotion']}）是否影响了决策质量？\n"
        "5. **综合评分**：A/B/C/D 四档（A=教科书级，B=合格，C=有明显失误，D=严重违规）\n"
        "6. **改进建议**：如果重来，最关键的一个改变是什么？\n\n"
        "输出格式：Markdown，结构清晰，语气直接不客套。"
    )

    def fmt_klines(klines: list, center: str) -> str:
        if not klines:
            return "（无数据）"
        lines = ["日期 | 开 | 高 | 低 | 收", "---|---|---|---|---"]
        for k in klines:
            marker = " ◀ 操作日" if k["date"] == center else ""
            lines.append(
                f"{k['date']}{marker} | {k['open']} | {k['high']} | {k['low']} | {k['close']}"
            )
        return "\n".join(lines)

    trade_date = trade["trade_time"][:10]
    user_prompt = (
        f"## 待复盘的交易记录\n\n"
        f"- **股票**：{trade['name']}（{trade['code']}）\n"
        f"- **操作时间**：{trade['trade_time']}\n"
        f"- **操作方向**：{trade['direction']}\n"
        f"- **成交价格**：{trade['price']} 元\n"
        f"- **操作手数**：{trade['volume']} 手（{trade['volume'] * 100} 股）\n"
        f"- **当时想法**：{trade['thought'] or '（未记录）'}\n"
        f"- **情绪状态**：{trade['emotion']}\n\n"
        f"---\n\n## {trade['name']}（{trade['code']}）K 线（操作日前后各约10个交易日）\n\n"
        f"{fmt_klines(stock_klines, trade_date)}\n\n"
        f"---\n\n## 上证指数同期走势\n\n"
        f"{fmt_klines(index_klines, trade_date)}\n\n"
        "请按阿狼体系标准对这笔操作进行完整复盘分析。"
    )
    return system_prompt, user_prompt


def build_ai_prompt(result: dict, history: list, mode: str = "intraday", user_hint: str = "", index_data: dict = None, rousu_data: dict = None) -> str:
    """将股票数据 + 持仓参数 + 历史数据 + 大盘指数数据组装成分析 prompt
    mode: 'intraday' = 盘中（今天怎么操作）| 'next_day' = 盘后（明天怎么操作）
    index_data: get_index_market_data() 的返回值，None 时不展示大盘段落
    rousu_data: 揉搓线预计算结果，None 时不展示揉搓线段落
    """
    history_text = "\n".join(
        f"  {r['date']}: 开{r.get('open', r['close'])} 收{r['close']} 高{r['high']} 低{r['low']} 均价{r['avg_price']}"
        for r in history
    )
    holding = result['cost_price'] > 0

    # ── 揉搓线形态文本块 ──────────────────────────────────────────────────────
    def _fmt_rousu(patterns: list) -> str:
        """将 analyze_rousu_lines() 结果格式化为单行文本列表。"""
        if not patterns:
            return "  （近期无揉搓线形态）"
        lines = []
        for p in patterns:
            lines.append(
                f"  {p['date']} [{p['label']}] {p['trend']} {p['color']} {p['shadow_order']}"
                f" → **{p['interpretation']}**"
            )
        return "\n".join(lines)

    if rousu_data:
        stock_daily_text    = _fmt_rousu(rousu_data.get("stock_daily", []))
        stock_intraday_text = _fmt_rousu(rousu_data.get("stock_intraday", []))

        index_rousu_parts = []
        for ts_code, idx_info in (rousu_data.get("index_daily") or {}).items():
            idx_name = idx_info.get("name", ts_code)
            idx_patterns = _fmt_rousu(idx_info.get("patterns", []))
            index_rousu_parts.append(f"{idx_name}（{ts_code}）：\n{idx_patterns}")
        index_rousu_text = "\n\n".join(index_rousu_parts) if index_rousu_parts else "  （暂无数据）"

        rousu_block = f"""
【揉搓线形态分析】
说明：揉搓线 = 小实体（实体占比<40%）+ 双侧长影线（上下影各>20%）。
影线顺序近似规则：日K 以开盘位置判断（开盘偏高→下影接上影，偏低→上影接下影）；30分钟K 以窗口内实际价格序列判断（更精确）。

▌个股日K 揉搓线（近10日）：
{stock_daily_text}

▌个股今日30分钟K 揉搓线：
{stock_intraday_text}

▌大盘指数日K 揉搓线（近5日）：
{index_rousu_text}
"""
    else:
        rousu_block = ""
    # ─────────────────────────────────────────────────────────────────────────

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
        condition_order_instruction = ""  # 盘中模式不输出条件单
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
        # ── 次日条件单专项指令 ──────────────────────────────────────────────
        if holding:
            condition_order_instruction = """
4b. **次日条件单建议**（持仓保护）：
基于上方操作计划，给出明日可挂的条件单参数，格式如下：

【止损单】
- 触发价：___（跌破此价位时卖出，建议参考动态防守价或关键支撑位，止损距离约为 1.5×ATR）
- 触发逻辑：说明为何选此价位（如：跌破8848下轨 / 跌破F618 / 跌破N日低点等）
- 条件单组合建议（请评估该标的的波动率与股性，从以下方案中推荐最合适的一种并给出具体参数）：
  * [方案A：纯价格止损] 适用于趋势彻底破位。设置“定价卖出”条件单，跌破核心防守价时【市价/对手价】卖出。
  * [方案B：时间规避法] 适用于容易早盘诱空的标的。设置“时间条件+定价”单，要求 10:00 之后若依然跌破核心防守价，则触发卖出。
  * [方案C：止损+反手纠错单] 适用于半导体、AI等高弹性/高振幅标的。
    1. 卖出单：跌破防守价时触发“定价卖出”。
    2. 接回单：同时布设“反弹买入”单，触发价设为防守价下方约 __% 处，反弹幅度设为 __%（建议1.5%~2.5%），触发后买回，防止深V洗盘。

【止盈单（第一目标）】
- 触发价：___（到达此价位时减仓约50%，建议参考上轨/F382/近期压力位）
- 触发逻辑：说明为何选此价位
- 条件单建议（二选一）：
  * 若预计该阻力位抛压极重：使用“定价卖出”，触价直接限价或市价卖出50%。
  * 若盘口可能强势突破：使用“回落卖出”，触发价设为目标区域下沿，回落幅度设为 __%（建议1%~1.5%），冲高回落时锁定半仓。

【止盈单（第二目标）】
- 触发价：___（到达此价位时清仓剩余仓位，建议参考更高压力位/历史高点）
- 触发逻辑：说明为何选此价位
- 条件单建议（强烈建议使用动态追踪）：
  * 使用“回落卖出”条件单。触发价设定为 ___，回落幅度设定为 __%（建议根据ATR设定在2%~4%之间）。
  * 执行逻辑：允许利润充分奔跑，只要股价继续上涨就不触发，直到从最高点级次回落达到设定百分比时，系统自动清仓剩余底仓。

注意：止盈距离应至少为止损距离的2倍（风险收益比 ≥ 1:2）；若当前位置风险收益比不足，请明确指出并建议是否值得持有。"""
        else:
            condition_order_instruction = """
4b. **次日条件单建议**（观望入场）：
基于上方操作计划，若明日出现买入机会，给出可挂的条件单参数，格式如下：

【买入条件单】
- 触发价：___（突破/回踩至此价位时买入，说明触发逻辑，如：突破上轨/回踩F618/放量站上均线等）
- 建议仓位：___（如：半仓试探 / 三成仓轻仓介入）
- 执行方式：触价限价买入（注明可接受的滑点范围）

【买入后止损单】
- 触发价：___（买入后跌破此价位离场，止损距离约为 1.5×ATR，参考动态防守价）
- 触发逻辑：说明为何选此价位
- 最大亏损：___元/股（= 买入价 - 止损价，供仓位管理参考）

【买入后止盈单（第一目标）】
- 触发价：___（到达此价位时减仓约50%）
- 触发逻辑：说明为何选此价位

【买入后止盈单（第二目标）】
- 触发价：___（到达此价位时清仓剩余仓位）
- 触发逻辑：说明为何选此价位

注意：止盈距离应至少为止损距离的2倍（风险收益比 ≥ 1:2）；若当前位置风险收益比不足，请明确指出并建议暂不入场。"""
        # ────────────────────────────────────────────────────────────────────

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

    # ── ATR & 动态防守价文本 ──────────────────────────────────────────────────
    atr_val   = result.get("atr")
    ddp_lower = result.get("ddp_lower")
    ddp_f618  = result.get("ddp_f618")
    if atr_val is not None:
        atr_line = f"ATR(14)波动幅度：{atr_val}"
        ddp_parts = []
        if ddp_lower is not None:
            ddp_parts.append(f"基于8848下轨={ddp_lower}")
        if ddp_f618 is not None:
            ddp_parts.append(f"基于F618={ddp_f618}")
        ddp_line = "动态防守价（支撑 − 0.5×ATR）：" + "  ".join(ddp_parts) if ddp_parts else ""
    else:
        atr_line = "ATR(14)波动幅度：暂无（历史数据不足14日）"
        ddp_line = ""
    # ─────────────────────────────────────────────────────────────────────────

    return f"""{mode_context}

【当前股票信息】
股票代码：{result['code']}
股票名称：{result['name']}
当日价格：{result['current_price']}（今日高:{result['high']} 低:{result['low']}）
VWAP均价：{result['avg_price']}
静态8848上轨：{result['upper_line']}
静态8848下轨：{result['lower_line']}
{atr_line}
{ddp_line + chr(10) if ddp_line else ""}持仓状态：{"持仓中，成本价 " + str(result['cost_price']) if holding else "未持仓"}
阶段高点：{result['stage_high'] if result['stage_high'] > 0 else "未设置"}
阶段低点：{result['stage_low'] if result['stage_low'] > 0 else "未设置"}
{fib_text}
20日高点：{result['n20_high']}  20日低点：{result['n20_low']}
60日高点：{result['n60_high']}  60日低点：{result['n60_low']}
静态规则信号参考：{result['signal']}

【近期历史数据（最近60日，按日期倒序）】
{history_text if history_text else "暂无历史数据"}
{rousu_block}
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
{condition_order_instruction}

5. **风险提示**（参考 Skill 07）：当前主要风险点是什么？有哪些需要特别注意的信号？

注意：分析基于当前有限数据，仅供参考，不构成投资建议。
{"" if not user_hint else chr(10) + "【用户补充说明】" + chr(10) + user_hint.strip()}"""



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
        open_ = float(df.loc[0, 'open'])

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
            "price": price, "high": high, "low": low, "avg_price": avg_price, "open": open_
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

        # ── ATR(14) 及动态防守价 ──────────────────────────────────────────
        # ATR = 近14日 TR 的简单移动平均，TR = max(H-L, |H-C_prev|, |L-C_prev|)
        atr_val = calc_atr(code)

        # 精度：ETF（51xxxx/15xxxx/16xxxx/18xxxx）保留3位，A股保留2位
        _short_code = code.split(".")[0] if "." in code else code
        _decimals = 3 if _short_code.startswith(("51", "15", "16", "18")) else 2

        def _ddp(support_price):
            """动态防守价 = 核心支撑位 - 0.5 × ATR；ATR 为 None 或支撑位无效时返回 None"""
            if atr_val is None or support_price is None or support_price <= 0:
                return None
            return round(support_price - 0.5 * atr_val, _decimals)

        ddp_lower = _ddp(lower_line)                                          # 基于 8848 下轨
        ddp_f618  = _ddp(strat["f618"]) if stage_params_set else None         # 基于 F618（仅观望模式有效）
        # ─────────────────────────────────────────────────────────────────

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

        # 分时快照（提前为局部变量，供分时图和K线图共用，避免重复查DB）
        _pts = _get_intraday_points(code)
        # 揉搓线快速信号（日K + 今日分时K，不走AI，直接规则引擎输出）
        _daily_recs = _get_daily_records_for_rousu(code, n=15)

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
            "intraday_points":    _pts,
            "intraday_candles_5":  _build_intraday_candles(_pts, window_minutes=5),
            "intraday_candles_15": _build_intraday_candles(_pts, window_minutes=15),
            "rousu_daily":    analyze_rousu_lines(_daily_recs, n=10, label="日K"),
            "rousu_intraday": analyze_rousu_lines_intraday(code),
            "boll": boll_data,
            "macd": macd_data,
            "stock_pct": stock_pct,
            "yidong": _calc_yidong(code, price),
            # ── ATR & 动态防守价 ──────────────────────────────────────────
            "atr":       atr_val,    # ATR(14) 原始值；None = 历史数据不足
            "ddp_lower": ddp_lower,  # 动态防守价（基于 8848 下轨）
            "ddp_f618":  ddp_f618,   # 动态防守价（基于 F618，仅 stage_params_set 时有值）
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
            "SELECT date, close, high, low, avg_price, COALESCE(open, close) AS open "
            "FROM daily_records "
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
    index_data = get_index_market_data(days=20)  # 已包含 open 字段

    # ── 揉搓线预计算 ──────────────────────────────────────────────────────────
    stock_daily_rousu    = analyze_rousu_lines(history, n=10, label="日K")
    stock_intraday_rousu = analyze_rousu_lines_intraday(stock_code)

    index_daily_rousu = {}
    for ts_code, idx_info in index_data.items():
        idx_records  = idx_info.get("records", [])
        idx_patterns = analyze_rousu_lines(idx_records, n=5, label="日K")
        index_daily_rousu[ts_code] = {
            "name":     idx_info.get("name", ts_code),
            "patterns": idx_patterns,
        }

    rousu_data = {
        "stock_daily":    stock_daily_rousu,
        "stock_intraday": stock_intraday_rousu,
        "index_daily":    index_daily_rousu,
    }
    # ─────────────────────────────────────────────────────────────────────────

    user_prompt = build_ai_prompt(
        result, history,
        mode=ai_mode,
        user_hint=user_hint,
        index_data=index_data,
        rousu_data=rousu_data,
    )

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


@app.get("/ai_stream")
async def ai_stream(request: Request, stock_code: str, ai_mode: str = "intraday", user_hint: str = "", session_id: str = ""):
    """
    SSE 端点：首次 AI 分析，流式推送进度和 token。
    前端通过 EventSource 连接，接收 progress/token/done/error 事件。
    完成后将对话历史存入 ai_conversations。
    """
    import uuid as _uuid

    async def generate():
        loop = asyncio.get_event_loop()

        # 1. 获取股票数据（在线程池中执行阻塞调用）
        yield "event: progress\ndata: 正在获取股票行情…\n\n"
        result = await loop.run_in_executor(None, calculate_8848, stock_code)
        if result.get("error"):
            err_msg = json.dumps({"msg": f"获取股票数据失败：{result.get('error')}"}, ensure_ascii=False)
            yield f"event: error\ndata: {err_msg}\n\n"
            return

        # 2. 历史数据
        yield "event: progress\ndata: 加载历史数据…\n\n"
        history = []
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT date, close, high, low, avg_price, COALESCE(open, close) AS open "
                "FROM daily_records WHERE code = ? ORDER BY date DESC LIMIT 60",
                (stock_code,),
            ).fetchall()
            conn.close()
            history = [dict(r) for r in rows]
        except Exception as e:
            logger.warning("ai_stream: history load failed %s", e)

        # 3. 构建 prompt
        yield "event: progress\ndata: 构建分析 Prompt…\n\n"

        def _build_prompts():
            skills_text = load_skills()
            system_prompt = (
                "你是基于阿狼投资体系的 A 股分析助手。"
                "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
                + skills_text
            )
            index_data = get_index_market_data(days=20)
            stock_daily_rousu    = analyze_rousu_lines(history, n=10, label="日K")
            stock_intraday_rousu = analyze_rousu_lines_intraday(stock_code)
            index_daily_rousu = {}
            for ts_code, idx_info in index_data.items():
                idx_records  = idx_info.get("records", [])
                idx_patterns = analyze_rousu_lines(idx_records, n=5, label="日K")
                index_daily_rousu[ts_code] = {
                    "name":     idx_info.get("name", ts_code),
                    "patterns": idx_patterns,
                }
            rousu_data = {
                "stock_daily":    stock_daily_rousu,
                "stock_intraday": stock_intraday_rousu,
                "index_daily":    index_daily_rousu,
            }
            user_prompt = build_ai_prompt(
                result, history,
                mode=ai_mode,
                user_hint=user_hint,
                index_data=index_data,
                rousu_data=rousu_data,
            )
            return system_prompt, user_prompt

        system_prompt, user_prompt = await loop.run_in_executor(None, _build_prompts)

        # 4. 流式 AI 调用
        yield "event: progress\ndata: AI 分析中…\n\n"
        messages = [{"role": "user", "content": user_prompt}]

        full_text = ""
        try:
            import queue as _queue
            import threading as _threading
            q = _queue.Queue()

            def _stream_thread():
                try:
                    for evt in call_ai_model_streaming(system_prompt, messages):
                        q.put(evt)
                except Exception as ex:
                    q.put(("error", str(ex)))
                finally:
                    q.put(None)  # sentinel

            t = _threading.Thread(target=_stream_thread, daemon=True)
            t.start()

            last_heartbeat = asyncio.get_event_loop().time()
            while True:
                try:
                    item = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.1)
                    now = asyncio.get_event_loop().time()
                    if now - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                    continue

                if item is None:
                    break
                evt_type, evt_data = item
                if evt_type == "progress":
                    payload = json.dumps({"msg": evt_data}, ensure_ascii=False)
                    yield f"event: progress\ndata: {payload}\n\n"
                elif evt_type == "token":
                    payload = json.dumps({"text": evt_data}, ensure_ascii=False)
                    yield f"event: token\ndata: {payload}\n\n"
                elif evt_type == "done":
                    full_text = evt_data
                elif evt_type == "error":
                    payload = json.dumps({"msg": evt_data}, ensure_ascii=False)
                    yield f"event: error\ndata: {payload}\n\n"
                    return
        except Exception as e:
            logger.error("ai_stream: AI call failed %s", e)
            payload = json.dumps({"msg": str(e)}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            return

        # 5. 保存对话历史
        sid = session_id or str(_uuid.uuid4())
        conv_messages = [
            {"role": "user",      "content": user_prompt},
            {"role": "assistant", "content": full_text},
        ]
        await loop.run_in_executor(None, _save_ai_conversation, sid, stock_code, conv_messages)

        done_payload = json.dumps({"session_id": sid, "provider": AI_PROVIDER}, ensure_ascii=False)
        yield f"event: done\ndata: {done_payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/ai_chat")
async def ai_chat(request: Request):
    """
    SSE 端点：多轮对话追问。
    请求体 JSON: {"session_id": "...", "stock_code": "...", "message": "用户追问"}
    流式推送和 /ai_stream 相同格式的事件。
    """
    body = await request.json()
    session_id = body.get("session_id", "")
    stock_code  = body.get("stock_code", "")
    user_message = body.get("message", "").strip()

    if not session_id or not user_message:
        async def _err():
            yield f"event: error\ndata: {json.dumps({'msg': '缺少 session_id 或 message'})}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    async def generate():
        loop = asyncio.get_event_loop()

        # 加载历史对话
        conv_messages = await loop.run_in_executor(None, _load_ai_conversation, session_id)
        if not conv_messages:
            payload = json.dumps({"msg": "会话已过期，请重新发起 AI 分析"}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            return

        # 拼接新用户消息
        conv_messages.append({"role": "user", "content": user_message})

        # 重建 system_prompt（加载 skills）
        def _load_sys():
            skills_text = load_skills()
            return (
                "你是基于阿狼投资体系的 A 股分析助手。"
                "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
                + skills_text
            )
        system_prompt = await loop.run_in_executor(None, _load_sys)

        full_text = ""
        try:
            import queue as _queue
            import threading as _threading
            q = _queue.Queue()

            def _stream_thread():
                try:
                    for evt in call_ai_model_streaming(system_prompt, conv_messages):
                        q.put(evt)
                except Exception as ex:
                    q.put(("error", str(ex)))
                finally:
                    q.put(None)

            t = _threading.Thread(target=_stream_thread, daemon=True)
            t.start()

            last_heartbeat = asyncio.get_event_loop().time()
            while True:
                try:
                    item = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.1)
                    now = asyncio.get_event_loop().time()
                    if now - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = now
                    continue

                if item is None:
                    break
                evt_type, evt_data = item
                if evt_type == "progress":
                    payload = json.dumps({"msg": evt_data}, ensure_ascii=False)
                    yield f"event: progress\ndata: {payload}\n\n"
                elif evt_type == "token":
                    payload = json.dumps({"text": evt_data}, ensure_ascii=False)
                    yield f"event: token\ndata: {payload}\n\n"
                elif evt_type == "done":
                    full_text = evt_data
                elif evt_type == "error":
                    payload = json.dumps({"msg": evt_data}, ensure_ascii=False)
                    yield f"event: error\ndata: {payload}\n\n"
                    return
        except Exception as e:
            logger.error("ai_chat: failed %s", e)
            payload = json.dumps({"msg": str(e)}, ensure_ascii=False)
            yield f"event: error\ndata: {payload}\n\n"
            return

        # 更新对话历史
        conv_messages.append({"role": "assistant", "content": full_text})
        await loop.run_in_executor(None, _save_ai_conversation, session_id, stock_code, conv_messages)

        done_payload = json.dumps({"session_id": session_id, "provider": AI_PROVIDER}, ensure_ascii=False)
        yield f"event: done\ndata: {done_payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
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


@app.post("/update_ai_provider", response_class=HTMLResponse)
async def update_ai_provider(request: Request, provider: str = Form(...)):
    """热切换 AI provider，无需重启。"""
    global AI_PROVIDER
    allowed = {"claude", "openai", "gemini"}
    provider = provider.strip().lower()
    if provider not in allowed:
        return RedirectResponse(url="/", status_code=303)
    AI_PROVIDER = provider
    _cfg.AI_PROVIDER = provider
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    _update_env_key(env_path, "AI_PROVIDER", provider)
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


# ── 注册子路由 ────────────────────────────────────────────────────────────────
import routes_sector
import routes_review

routes_sector.register(app, templates)
routes_review.register(app, templates)
