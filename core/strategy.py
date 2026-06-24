"""
core/strategy.py — 核心业务逻辑
包含：8848 策略计算、斐波那契信号、AI prompt 构建、技能库加载等。
"""
import sqlite3

import tushare as ts

import core.config as _cfg
from core.config import logger, DB_PATH, SKILLS_DIR, pro
from core.db import (
    get_cached_name, set_cached_name,
    save_daily_record, get_portfolio, save_portfolio,
    get_n_day_stats, calc_atr,
)
from services.indicators import (
    analyze_rousu_lines, analyze_rousu_lines_intraday,
    _calc_macd, _calc_boll, _calc_yidong,
    _get_intraday_points, _build_intraday_candles,
    _get_daily_records_for_rousu,
    get_volatility_stats,
)


def to_ts_code(code: str) -> str:
    """将各种格式的股票代码规范化为 Tushare Pro ts_code 格式。"""
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


def load_skills() -> str:
    """读取 skills/*.md 和 skills/personal/*.md，拼接为字符串。"""
    if not SKILLS_DIR.exists():
        return ""
    parts = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        parts.append(f"## {f.name}\n\n" + f.read_text(encoding="utf-8"))
    personal_dir = SKILLS_DIR / "personal"
    if personal_dir.exists():
        personal_files = sorted(personal_dir.glob("*.md"))
        if personal_files:
            parts.append("## 个人交易画像（优先参考）")
            for f in personal_files:
                parts.append(f.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def calculate_strategy(now, cost, st_high, stage_high, stage_low, stage_params_set: bool = False):
    signal = "观望"
    advice_class = "secondary"

    if stage_params_set:
        diff = stage_high - stage_low
        f382 = stage_high - diff * 0.382
        f618 = stage_high - diff * 0.618
        f786 = stage_high - diff * 0.786
    else:
        diff = f382 = f618 = f786 = 0.0

    is_break_low = False

    if cost > 0:
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
        if not stage_params_set:
            if stage_high > 0 and now > stage_high:
                signal = "突破跟进"
                advice_class = "danger"
            else:
                signal = "观望"
                advice_class = "secondary"
        else:
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


def calculate_8848(code: str):
    try:
        df = ts.get_realtime_quotes(code)
        if df is None or df.empty:
            return {"error": "Stock code not found or data unavailable."}

        name = str(df.loc[0, "name"])
        set_cached_name(code, name)
        price = float(df.loc[0, "price"])
        high = float(df.loc[0, "high"])
        low = float(df.loc[0, "low"])
        open_ = float(df.loc[0, "open"])
        volume = float(df.loc[0, "volume"])
        amount = float(df.loc[0, "amount"])

        if volume == 0:
            return {"error": "Volume is 0, cannot calculate average price (Market might be closed or just opened)."}
        if price == 0:
            return {"error": "Current price is 0, cannot calculate (Stock might be suspended)."}

        avg_price = amount / volume
        if abs(avg_price - price) / price > 0.5:
            if abs((avg_price * 100) - price) / price < 0.5:
                avg_price *= 100

        save_daily_record(code, name, {
            "price": price, "high": high, "low": low, "avg_price": avg_price, "open": open_
        })

        portfolio = get_portfolio(code)
        cost_price = portfolio["cost"]
        stage_high = portfolio["stage_high"]
        stage_low = portfolio["stage_low"]
        max_price = portfolio["max_price"]
        quantity = portfolio["quantity"]

        if cost_price > 0 and high > max_price:
            max_price = high
            save_portfolio(code, cost_price, stage_high, stage_low, max_price, quantity)

        st_high = max_price if max_price > 0 else high
        stage_params_set = (
            stage_high > 0 and stage_low > 0 and stage_high > stage_low
        )

        strat = calculate_strategy(price, cost_price, st_high, stage_high, stage_low, stage_params_set)

        upper_line = avg_price / 0.98848
        lower_line = avg_price * 0.98848

        n_day = get_n_day_stats(code)
        atr_val = calc_atr(code)

        _short_code = code.split(".")[0] if "." in code else code
        _decimals = 3 if _short_code.startswith(("51", "15", "16", "18")) else 2

        def _ddp(support_price):
            if atr_val is None or support_price is None or support_price <= 0:
                return None
            return round(support_price - 0.5 * atr_val, _decimals)

        ddp_lower = _ddp(lower_line)
        ddp_f618 = _ddp(strat["f618"]) if stage_params_set else None

        boll_data = _calc_boll(code)
        macd_data = _calc_macd(code)

        stock_pct = None
        if boll_data and boll_data.get("recent_closes"):
            rc = boll_data["recent_closes"]
            if rc and rc[0]["close"]:
                base = rc[0]["close"]
                stock_pct = [
                    round((r["close"] - base) / base * 100, 2) if r["close"] else None
                    for r in rc
                ]

        _pts = _get_intraday_points(code)
        _daily_recs = _get_daily_records_for_rousu(code, n=15)
        _vol_stats = get_volatility_stats(code)

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
            "quantity": quantity,
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
            "n20_low": n_day["n20_low"],
            "n60_high": n_day["n60_high"],
            "n60_low": n_day["n60_low"],
            "intraday_points": _pts,
            "intraday_candles_5": _build_intraday_candles(_pts, window_minutes=5),
            "intraday_candles_15": _build_intraday_candles(_pts, window_minutes=15),
            "rousu_daily": analyze_rousu_lines(_daily_recs, n=10, label="日K"),
            "rousu_intraday": analyze_rousu_lines_intraday(code),
            "boll": boll_data,
            "macd": macd_data,
            "stock_pct": stock_pct,
            "yidong": _calc_yidong(code, price),
            "atr": atr_val,
            "ddp_lower": ddp_lower,
            "ddp_f618": ddp_f618,
            "vol_stats": _vol_stats,
        }
    except Exception as e:
        return {"error": str(e)}


def calculate_8848_history(code: str, days: int = 20):
    """计算最近 days 个交易日的 8848 上下轨信息。"""
    if pro is None:
        logger.warning("calculate_8848_history: pro client is None, skip. code=%s", code)
        return []

    try:
        ts_code = to_ts_code(code)
        if not ts_code:
            logger.warning("calculate_8848_history: unrecognized code format code=%s", code)
            return []
        df = pro.daily(ts_code=ts_code, limit=days * 5)
    except Exception:
        logger.exception("Failed to fetch history for code=%s", code)
        return []

    if df is None or df.empty:
        return []

    records = []
    for _, row in df.iterrows():
        try:
            close_price = float(row["close"])
        except Exception:
            continue

        amount = float(row.get("amount", 0))
        volume = float(row.get("vol", 0))

        if volume > 0 and amount > 0:
            avg_price = (amount * 1000) / (volume * 100)
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

        records.append({
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
        })

    return sorted(records, key=lambda x: x["date"], reverse=True)[:days]


def get_stock_volume_chart_data(history_results: list) -> dict | None:
    """将 calculate_8848_history() 返回的 records 转换为 ECharts 格式。"""
    if not history_results:
        return None

    records = list(reversed(history_results))
    dates = [r["date"] for r in records]
    amounts = [r.get("amount_yi", 0) for r in records]
    closes = [r.get("close", None) for r in records]
    colors: list[str] = []
    labels: list[str] = []

    for i, amt in enumerate(amounts):
        if i == 0 or amounts[i - 1] == 0:
            colors.append("#9e9e9e")
            labels.append("—")
        else:
            if amt > amounts[i - 1]:
                colors.append("#ef5350")
                labels.append("放量")
            else:
                colors.append("#9e9e9e")
                labels.append("缩量")

    return {
        "dates": dates,
        "amounts": amounts,
        "colors": colors,
        "labels": labels,
        "closes": closes,
        "opens": [r.get("open", None) for r in records],
        "highs": [r.get("high", None) for r in records],
        "lows": [r.get("low", None) for r in records],
        "upper_lines": [r.get("upper_line", None) for r in records],
        "lower_lines": [r.get("lower_line", None) for r in records],
        "avg_prices": [r.get("avg_price", None) for r in records],
    }


def build_review_prompt(trade: dict, stock_klines: list, index_klines: list) -> tuple:
    """构建单笔复盘的 system_prompt 和 user_prompt。"""
    skills_text = load_skills()

    system_prompt = (
        "你是一位严格的交易复盘导师，使用阿狼交易体系标准进行复盘分析。\n"
        "【重要规则】A 股实行 T+1 交易制度：当日买入的股票必须等到次日才能卖出；"
        "只有昨日已有持仓的股票，今日才可以做T（高卖低买）。"
        "在复盘分析和评价操作时必须以此规则为前提，评估买卖时序是否合规。\n\n"
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
    """将股票数据 + 持仓参数 + 历史数据 + 大盘指数数据组装成分析 prompt。"""
    history_text = "\n".join(
        f"  {r['date']}: 开{r.get('open', r['close'])} 收{r['close']} 高{r['high']} 低{r['low']} 均价{r['avg_price']}"
        for r in history
    )
    holding = result["cost_price"] > 0

    def _fmt_rousu(patterns: list) -> str:
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
        stock_daily_text = _fmt_rousu(rousu_data.get("stock_daily", []))
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
影线顺序近似规则：日K 以开盘位置判断；30分钟K 以窗口内实际价格序列判断（更精确）。

▌个股日K 揉搓线（近10日）：
{stock_daily_text}

▌个股今日30分钟K 揉搓线：
{stock_intraday_text}

▌大盘指数日K 揉搓线（近5日）：
{index_rousu_text}
"""
    else:
        rousu_block = ""

    fib_text = (
        f"斐波那契 38.2%: {result['f382']}  61.8%: {result['f618']}  78.6%: {result['f786']}"
        if result.get("stage_params_set")
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
        condition_order_instruction = ""
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
        if holding:
            condition_order_instruction = """
4b. **次日条件单建议**（持仓保护）：

券商条件单支持两种动态触发方式，请优先使用：
- **冲高回落型止盈**：价格先涨到目标高点，再回落一定幅度才触发卖出（适合止盈，不在高位踏空）
- **下跌反弹型纠错**：价格先跌破止损价，再反弹一定幅度才触发买回（适合防深V洗盘）
- 所有价位同时给出绝对价格和相对当前收盘价的百分比偏移，方便快速挂单

请按明日三种开盘情形分别给出方案：

**【方案一：高开 ≥1.5%】**（开盘即在压力区，警惕假突破后回落）
- 止盈单：使用"冲高回落"型，先涨至___（收盘价×1.0X），再回落___点或___%触发
- 止损单：因高开已接近目标位，止损线相应上移至___（约 +0.5×ATR），防止利润回吐
- 建议：止盈优先，不急于止损；若开盘即超预期强势，可将止损移至今日收盘价附近保本

**【方案二：平开（±1.5% 以内）】**（正常情形，用静态计算价位）
- 止盈单（第一目标）：冲高回落型，涨至___（参考8848上轨/F382/近期压力位），回落___%触发，减仓约50%
- 止盈单（第二目标）：涨至___（参考更高压力位/历史高点），回落___%触发，清仓剩余
- 止损单：跌破___（动态防守价，约 1.5×ATR 止损距离）时触发；若担心深V可用"下跌反弹"型：跌破止损价后反弹1%再卖
- 时间规避选项：若开盘前30分钟（9:25-10:00）内出现大幅震荡，可推迟至10:00后再判断是否触发

**【方案三：低开 ≥1.5%】**（低开跳空，持仓压力大，需区分洗盘与趋势破坏）
- 止损单：将防守价下移约 0.5×ATR 至___，给低开留出喘息空间
- 纠错单（防深V）：若触发止损后，价格反弹回___（止损价+1%~2%），触发反手买回
- 止盈单：低开后若快速拉回，冲高回落型止盈触发价上调至___

**价位汇总表**（方便快速对照挂单）：
| 情形 | 止损触发价 | 第一止盈触发价 | 第二止盈触发价 |
|------|------------|---------------|---------------|
| 高开≥1.5% | ___ | ___ | ___ |
| 平开 | ___ | ___ | ___ |
| 低开≥1.5% | ___ | ___ | ___ |

注意：止盈距离应至少为止损距离的2倍（风险收益比 ≥ 1:2）。若ATR数据缺失，止损距离建议用收盘价×1.5%代替。"""
        else:
            condition_order_instruction = """
4b. **次日条件单建议**（观望入场）：

券商条件单支持两种动态触发方式，请优先使用：
- **冲高回落型**：价格先涨到确认突破价，再回落一定幅度才触发买入（避免追高）
- **下跌反弹型**：价格先跌到支撑位，再反弹一定幅度才触发买入（量价确认，防止越跌越买）
- 所有价位同时给出绝对价格和相对当前收盘价的百分比偏移

请按明日三种开盘情形分别给出方案：

**【方案一：高开 ≥1.5%】**（高开追高风险大，不建议开盘直接追）
- 买入策略：改用"冲高回落确认型"——等待价格先涨至___，再回落___%（约0.5×ATR）时触发
- 若高开幅度超过2%且量能不足，建议放弃当日入场，等下一个低吸机会
- 建议仓位：___（高开追入降至三成仓，控制风险）

**【方案二：平开（±1.5% 以内）】**（正常情形，首选方案）
- 突破型买入：价格突破___（近期压力位/8848上轨），确认站稳后买入
- 回踩型买入：价格回踩至___（关键支撑/F618/F786），触发买入；或用"下跌反弹型"——跌至___后反弹___%再触发
- 建议仓位：___（如：半仓试探，确认后补仓至满仓）
- 买入后止损：___（止损距离约 1.5×ATR，最大亏损___元/股）
- 买入后止盈（第一目标）：___（至少为止损距离2倍）
- 买入后止盈（第二目标）：___

**【方案三：低开 ≥1.5%】**（低开跳空，需先观察，不要第一时间抄底）
- 观察窗口：等待开盘后10~15分钟，看量能和方向是否稳定（避免急杀阶段入场）
- 若低开后缩量止跌：使用"下跌反弹型"——跌至___后反弹___%触发买入
- 若低开后继续放量下杀：放弃当日入场，等待新的支撑确认
- 建议仓位：___（低开入场降至三成仓试探）

**价位汇总表**（方便快速对照挂单）：
| 情形 | 买入触发价 | 触发方式 | 止损价 | 第一止盈价 |
|------|------------|---------|--------|-----------|
| 高开≥1.5% | ___ | 冲高回落 | ___ | ___ |
| 平开 | ___ | 突破/回踩 | ___ | ___ |
| 低开≥1.5% | ___ | 下跌反弹 | ___ | ___ |

注意：止盈距离应至少为止损距离的2倍（风险收益比 ≥ 1:2）。若ATR数据缺失，止损距离建议用收盘价×1.5%代替。"""

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

    atr_val = result.get("atr")
    ddp_lower = result.get("ddp_lower")
    ddp_f618 = result.get("ddp_f618")
    vol_stats = result.get("vol_stats")

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

    if vol_stats:
        _src_label = f"近{vol_stats['days_used']}日分时快照" if vol_stats["source"] == "intraday" else f"近{vol_stats['days_used']}日K线影线"
        vol_stats_line = (
            f"日内波动特征（{_src_label}）：\n"
            f"  冲高后典型回落幅度（上影线均值）：{vol_stats['avg_upper_shadow_pct']}%\n"
            f"  低点后典型反弹幅度（下影线均值）：{vol_stats['avg_lower_shadow_pct']}%\n"
            f"  日内平均总振幅：{vol_stats['avg_daily_range_pct']}%\n"
            f"  → 条件单参考：冲高回落止盈回落幅度建议约 {vol_stats['avg_upper_shadow_pct']}%，"
            f"下跌反弹买入/纠错单反弹幅度建议约 {vol_stats['avg_lower_shadow_pct']}%，"
            f"止损安全距离建议至少 {round(vol_stats['avg_daily_range_pct'] * 0.4, 2)}%（振幅×40%）以避免误触发。"
        )
    else:
        vol_stats_line = "日内波动特征：暂无数据（COMMON_STOCKS 列表外的票需积累分时快照后可用）"

    return f"""{mode_context}

【当前股票信息】
股票代码：{result['code']}
股票名称：{result['name']}
当日价格：{result['current_price']}（今日高:{result['high']} 低:{result['low']}）
VWAP均价：{result['avg_price']}
静态8848上轨：{result['upper_line']}
静态8848下轨：{result['lower_line']}
{atr_line}
{ddp_line + chr(10) if ddp_line else ""}{vol_stats_line}
持仓状态：{"持仓中，成本价 " + str(result['cost_price']) if holding else "未持仓"}
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

0. **对静态信号的批判性评估**：结合量价关系、波动性等因素，判断静态8848上下轨和斐波那契信号的可靠性。

1. **大盘阶段判断**（参考 Skill 01）：当前大盘处于哪个阶段（3-1/3-2/3-3/3-4/3-5）？对个股操作有何影响？

2. **股票类型判断**（参考 Skill 11）：属于哪种类型（A/B/C/D/E类及子类）？

3. **量价状态**（参考 Skill 05）：近期量能趋势如何？资金动向？

4. **{op3_label}**（参考 Skill 03 + 对应类型操作规则）：结合静态规则信号参考（{result['signal']}），{op3_focus}
{extra_instruction}
{condition_order_instruction}

5. **风险提示**（参考 Skill 07）：当前主要风险点和需要特别注意的信号。

注意：分析基于当前有限数据，仅供参考，不构成投资建议。
{"" if not user_hint else chr(10) + "【用户补充说明】" + chr(10) + user_hint.strip()}"""