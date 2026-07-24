import time
import sqlite3
import uuid

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import core.config as _cfg
from core.config import logger, DB_PATH, pro
from core.db import (
    get_cached_name,
    save_temp_result, load_temp_result,
    get_index_market_data,
)
from services.ai import call_ai_model_with_tools

# app 和 templates 在注册时注入
_app = None
_templates = None


def _period_return(records: list[dict], days: int) -> float | None:
    """计算按日期倒序的行情记录在给定窗口内的累计涨跌幅。"""
    if len(records) < 2:
        return None
    window = records[:min(days, len(records))]
    latest_close = window[0].get("close", 0) or 0
    base_close = window[-1].get("close", 0) or 0
    if not latest_close or not base_close:
        return None
    return round((latest_close / base_close - 1) * 100, 2)


def _summarize_index_records(records: list[dict]) -> dict:
    """将较长指数历史压缩为可审计的趋势/量能画像，避免模型仅凭 5 日 K 线判浪。"""
    if not records:
        return {}
    latest = records[0]
    recent20 = records[:20]
    highs = [r.get("high", 0) or 0 for r in recent20]
    lows = [r.get("low", 0) or 0 for r in recent20]
    amounts_5 = [r.get("amount_yi", 0) or 0 for r in records[:5]]
    amounts_prev20 = [r.get("amount_yi", 0) or 0 for r in records[5:25]]
    avg5 = sum(amounts_5) / len(amounts_5) if amounts_5 else 0
    avg_prev20 = sum(amounts_prev20) / len(amounts_prev20) if amounts_prev20 else 0
    amount_change = round((avg5 / avg_prev20 - 1) * 100, 1) if avg_prev20 else None
    return {
        "sample_days": len(records),
        "latest_close": latest.get("close", 0),
        "return_5d": _period_return(records, 5),
        "return_20d": _period_return(records, 20),
        "return_60d": _period_return(records, 60),
        "high_20d": round(max(highs), 2) if highs else None,
        "low_20d": round(min(lows), 2) if lows else None,
        "amount_5d_vs_prev20_pct": amount_change,
    }


def register(app, templates):
    global _app, _templates
    _app = app
    _templates = templates
    _register_routes(app, templates)


def _register_routes(app, templates):

    @app.get("/sector", response_class=HTMLResponse)
    async def sector_page(request: Request, result_id: str = None):
        """板块轮动分析页面入口（GET，渲染空页面或从临时结果恢复）。"""
        defaults = {
            "ai_analysis": None,
            "ai_error":    None,
            "sector_data": None,
            "ai_provider": _cfg.AI_PROVIDER,
            "user_hint":   "",
        }
        ctx = load_temp_result(result_id) if result_id else {}
        return templates.TemplateResponse("sector.html", {"request": request, **defaults, **ctx})

    @app.post("/sector_analyze", response_class=HTMLResponse)
    async def sector_analyze(request: Request, user_hint: str = Form("")):
        """触发板块轮动 AI 分析。"""
        logger.info("sector_analyze: start provider=%s", _cfg.AI_PROVIDER)

        sector_data = build_sector_prompt_data()
        logger.info(
            "sector_analyze: data ready sectors=%d hsgt=%d errors=%d",
            len(sector_data["sector_perf"]),
            len(sector_data["hsgt_flow"]),
            len(sector_data["errors"]),
        )

        from core.strategy import load_skills, to_ts_code
        # 板块分析只需 行情阶段/板块轮动/量价/市场参与者 四个技能
        skills_text = load_skills(subset=["01", "04", "05", "08"])
        system_prompt = (
            "你是基于阿狼投资体系的 A 股板块轮动分析助手。\n"
            "【重要规则】A 股实行 T+1 交易制度：当日买入的股票必须等到次日才能卖出；"
            "只有昨日已有持仓的股票，今日才可以做T（高卖低买）。"
            "在给出买卖建议时必须严格遵守此规则，不得建议投资者当日买入后同日卖出。\n"
            "【工具使用】你可以调用可用工具补充以下数据：\n"
            "  - get_sector_flow：获取主要板块资金净流入，判断轮动方向\n"
            "  - get_futures_positions：获取期指多空持仓，辅助判断大盘方向\n"
            "  - get_moneyflow（指定某只风向标个股）：确认龙头资金健康度\n"
            "请在分析前主动调用 get_sector_flow，其余工具按需调用。\n\n"
            "【方法论使用边界】技能库中的具体日期、行业主线、个股、轮动顺序和浪型结论均为历史案例，"
            "不得迁移为当前市场事实。当前结论必须由本次提供的行情、资金和工具数据支持；"
            "数据与历史案例冲突时，以本次数据为准。\n"
            "【浪型纪律】不得默认当前处于 3 浪，更不得在证据不足时编造 3-3/3-5。"
            "先在五浪上行、ABC 调整、箱体震荡和下跌趋势之间比较；"
            "只有一级浪型证据充分时，才继续判断子浪。无法可靠判浪时必须明确写“无法可靠判浪”，"
            "并改用趋势、量能和轮动强度给出条件化策略。\n\n"
            "以下是阿狼投资体系的技能库（已裁剪为板块分析相关章节）：\n\n"
            + skills_text
        )
        user_prompt = build_sector_ai_prompt(sector_data, user_hint=user_hint)

        ai_analysis = None
        ai_error = None
        try:
            ai_analysis = call_ai_model_with_tools(system_prompt, user_prompt)
            logger.info("sector_analyze: done")
        except Exception as e:
            ai_error = str(e)
            logger.error("sector_analyze: failed error=%s", e)

        rid = str(uuid.uuid4())
        save_temp_result(rid, {
            "ai_analysis": ai_analysis,
            "ai_error":    ai_error,
            "sector_data": sector_data,
            "ai_provider": _cfg.AI_PROVIDER,
            "user_hint":   user_hint,
        })
        return RedirectResponse(url=f"/sector?result_id={rid}", status_code=303)


def build_sector_prompt_data() -> dict:
    """
    拉取板块轮动分析所需的原始数据，返回结构化 dict。
    每个 Tushare 接口独立 try/except，失败时 logger.warning 并填充空值，不崩溃。
    """
    from datetime import datetime, timedelta
    from core.strategy import to_ts_code

    result = {
        "sector_perf": [],
        "hsgt_flow": [],
        "sector_stocks": {},
        "errors": [],
    }

    if pro is None:
        result["errors"].append("Tushare Pro 未初始化（缺少 TUSHARE_TOKEN）")
        return result

    today = datetime.today()
    # 一级浪型至少需要覆盖数周的趋势，而不是只看最近 5 个交易日。
    # 90 个自然日通常可取得约 60 个交易日，也为 20/60 日强弱比较留出样本。
    start_date = (today - timedelta(days=90)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    sector_list = []
    try:
        df_idx = pro.ths_index(exchange="A", type="N")
        if df_idx is not None and not df_idx.empty:
            sector_list = df_idx[["ts_code", "name"]].dropna().to_dict("records")
            logger.info("build_sector_prompt_data: got %d ths_index sectors", len(sector_list))
        else:
            result["errors"].append("ths_index: 返回空数据")
    except Exception as e:
        logger.warning("build_sector_prompt_data: ths_index failed: %s", e)
        result["errors"].append(f"ths_index: {e}")

    sector_daily = {}
    if sector_list:
        try:
            df_daily = pro.ths_daily(start_date=start_date, end_date=end_date)
            if df_daily is not None and not df_daily.empty:
                for _, row in df_daily.iterrows():
                    code = str(row.get("ts_code", ""))
                    if not code:
                        continue
                    if code not in sector_daily:
                        sector_daily[code] = []
                    sector_daily[code].append({
                        "trade_date": str(row.get("trade_date", "")),
                        "close":      float(row.get("close", 0) or 0),
                        "pct_change": float(row.get("pct_change", 0) or 0),
                        "amount_rate": float(row.get("turnover_rate", 0) or 0),
                    })
                logger.info("build_sector_prompt_data: got ths_daily for %d sectors", len(sector_daily))
            else:
                result["errors"].append("ths_daily: 返回空数据")
        except Exception as e:
            logger.warning("build_sector_prompt_data: ths_daily failed: %s", e)
            result["errors"].append(f"ths_daily: {e}")

    sector_code_to_name = {s["ts_code"]: s["name"] for s in sector_list}
    perf_list = []
    for ts_code, records in sector_daily.items():
        records_sorted = sorted(records, key=lambda x: x["trade_date"], reverse=True)
        pct_1d = records_sorted[0]["pct_change"] if records_sorted else 0
        pct_5d = _period_return(records_sorted, 5) or 0.0
        perf_list.append({
            "ts_code":       ts_code,
            "name":          sector_code_to_name.get(ts_code, ts_code),
            "pct_change_1d": round(pct_1d, 2),
            "pct_change_5d": pct_5d,
            "pct_change_20d": _period_return(records_sorted, 20),
            "pct_change_60d": _period_return(records_sorted, 60),
            "days_data":     len(records_sorted),
        })

    perf_list.sort(key=lambda x: x["pct_change_5d"], reverse=True)
    # 保留全量用于弱势板块和扩散度判断；页面仍只展示前 20，保持原有 UI 简洁。
    result["sector_perf_all"] = perf_list
    result["sector_perf"] = perf_list[:20]
    logger.info("build_sector_prompt_data: sector_perf built, top=%s",
                result["sector_perf"][0]["name"] if result["sector_perf"] else "N/A")

    try:
        df_hsgt = pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
        if df_hsgt is not None and not df_hsgt.empty:
            df_hsgt = df_hsgt.sort_values("trade_date", ascending=False).head(5)
            for _, row in df_hsgt.iterrows():
                result["hsgt_flow"].append({
                    "trade_date":  str(row.get("trade_date", "")),
                    "north_money": round(float(row.get("north_money", 0) or 0), 2),
                    "south_money": round(float(row.get("south_money", 0) or 0), 2),
                })
            logger.info("build_sector_prompt_data: got %d hsgt records", len(result["hsgt_flow"]))
        else:
            result["errors"].append("moneyflow_hsgt: 返回空数据")
    except Exception as e:
        logger.warning("build_sector_prompt_data: moneyflow_hsgt failed: %s", e)
        result["errors"].append(f"moneyflow_hsgt: {e}")

    top_sectors = result["sector_perf"][:8]
    for sector in top_sectors:
        sector_ts_code = sector["ts_code"]
        sector_name = sector["name"]
        stocks_in_sector = []

        member_codes = []
        try:
            df_members = pro.ths_member(ts_code=sector_ts_code)
            if df_members is not None and not df_members.empty:
                member_codes = df_members["con_code"].dropna().tolist()[:30]
                logger.info("build_sector_prompt_data: sector=%s members=%d", sector_name, len(member_codes))
            else:
                result["errors"].append(f"ths_member({sector_name}): 返回空数据")
        except Exception as e:
            logger.warning("build_sector_prompt_data: ths_member(%s) failed: %s", sector_name, e)
            result["errors"].append(f"ths_member({sector_name}): {e}")

        for raw_code in member_codes[:20]:
            ts_code_stock = to_ts_code(str(raw_code))
            if not ts_code_stock:
                continue
            try:
                df_basic = pro.daily_basic(
                    ts_code=ts_code_stock, limit=1,
                    fields="ts_code,trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate,pct_chg"
                )
                if df_basic is not None and not df_basic.empty:
                    row = df_basic.iloc[0]
                    stock_name = get_cached_name(ts_code_stock) or ts_code_stock
                    total_mv_yi = round(float(row.get("total_mv", 0) or 0) / 10000, 1)
                    stocks_in_sector.append({
                        "ts_code":       ts_code_stock,
                        "name":          stock_name,
                        "turnover_rate": round(float(row.get("turnover_rate", 0) or 0), 2),
                        "pe_ttm":        round(float(row.get("pe_ttm", 0) or 0), 1),
                        "total_mv_yi":   total_mv_yi,
                        "pct_chg":       round(float(row.get("pct_chg", 0) or 0), 2),
                    })
            except Exception as e:
                logger.warning("build_sector_prompt_data: daily_basic(%s) failed: %s", ts_code_stock, e)

        stocks_in_sector.sort(key=lambda x: x["turnover_rate"], reverse=True)
        result["sector_stocks"][sector_name] = stocks_in_sector[:5]

    return result


def build_sector_ai_prompt(data: dict, user_hint: str = "") -> str:
    """将 build_sector_prompt_data() 的结果组装成 AI 分析 prompt。"""
    today_str = time.strftime("%Y-%m-%d")

    # 读 60 日历史用于一级浪型/趋势判断；保留最近 5 日明细以便复核。
    index_data = get_index_market_data(days=60)
    index_sections = []
    for ts_code, idata in index_data.items():
        name = idata.get("name", ts_code)
        records = idata.get("records", [])
        if records:
            summary = _summarize_index_records(records)
            lines = "\n".join(
                f"  {r['date']}: 收{r['close']} 高{r['high']} 低{r['low']} 成交额{r['amount_yi']}亿"
                for r in records[:5]
            )
            index_sections.append(
                f"{name}（{ts_code}，样本{summary['sample_days']}日）："
                f"5日{summary['return_5d'] if summary['return_5d'] is not None else 'N/A'}%，"
                f"20日{summary['return_20d'] if summary['return_20d'] is not None else 'N/A'}%，"
                f"60日{summary['return_60d'] if summary['return_60d'] is not None else 'N/A'}%；"
                f"近20日高/低 {summary['high_20d']}/{summary['low_20d']}；"
                f"近5日成交额相对前20日 {summary['amount_5d_vs_prev20_pct'] if summary['amount_5d_vs_prev20_pct'] is not None else 'N/A'}%\n"
                f"最近5日明细：\n{lines}"
            )
        else:
            index_sections.append(f"{name}（{ts_code}）：暂无数据")
    index_text = "\n\n".join(index_sections) if index_sections else "暂无大盘数据"

    if data["hsgt_flow"]:
        hsgt_text = "\n".join(
            f"  {r['trade_date']}: 北向净流入 {r['north_money']:.1f}亿"
            for r in data["hsgt_flow"]
        )
    else:
        hsgt_text = "暂无数据（接口不可用或无权限）"

    if data["sector_perf"]:
        top5d = data["sector_perf"][:10]
        perf_lines_5d = "\n".join(
            f"  {i+1}. {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}% "
            f"近20日{(s['pct_change_20d'] if s['pct_change_20d'] is not None else 0):+.2f}% "
            f"今日{s['pct_change_1d']:+.2f}%（样本{s['days_data']}日）"
            for i, s in enumerate(top5d)
        )
        # 弱势板块必须从全量行业中选取，不能从“涨幅前 20”里倒排。
        all_sector_perf = data.get("sector_perf_all") or data["sector_perf"]
        bottom5d = sorted(all_sector_perf, key=lambda x: x["pct_change_5d"])[:5]
        perf_lines_bottom = "\n".join(
            f"  {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}% "
            f"近20日{(s['pct_change_20d'] if s['pct_change_20d'] is not None else 0):+.2f}% "
            f"今日{s['pct_change_1d']:+.2f}%（样本{s['days_data']}日）"
            for s in bottom5d
        )
        sector_text = f"近5日涨幅前10：\n{perf_lines_5d}\n\n近5日跌幅前5（弱势板块）：\n{perf_lines_bottom}"
    else:
        sector_text = "暂无板块涨跌幅数据（ths_daily 接口不可用或无权限）"

    stocks_sections = []
    for sector_name, stocks in data["sector_stocks"].items():
        if not stocks:
            continue
        stock_lines = "\n".join(
            f"    {s['ts_code']} {s['name']}: 今日{s['pct_chg']:+.2f}% 换手{s['turnover_rate']:.1f}% "
            f"PE(TTM){s['pe_ttm']:.1f} 市值{s['total_mv_yi']:.0f}亿"
            for s in stocks
        )
        stocks_sections.append(f"  【{sector_name}】活跃个股（按换手率排序）：\n{stock_lines}")
    stocks_text = "\n\n".join(stocks_sections) if stocks_sections else "暂无个股基本面数据"

    if data["errors"]:
        errors_text = "（以下接口数据不可用，分析时请忽略对应部分）：\n" + "\n".join(f"  - {e}" for e in data["errors"])
    else:
        errors_text = "（所有数据接口正常）"

    user_hint_text = f"\n【用户补充说明】\n{user_hint.strip()}" if user_hint and user_hint.strip() else ""

    # 辅助：从活跃个股数据里提取换手率最高的票，用于"发散判断"提示
    dispersion_hints = []
    for sector_name, stocks in data["sector_stocks"].items():
        if not stocks:
            continue
        top = stocks[0]  # 已按换手率排序，第一只换手率最高
        dispersion_hints.append(
            f"  {sector_name}：换手率最高 {top['name']}（{top['ts_code']}）"
            f" {top['turnover_rate']:.1f}% 今日{top['pct_chg']:+.2f}%"
        )
    dispersion_text = "\n".join(dispersion_hints) if dispersion_hints else "（暂无数据）"

    return f"""【分析日期】{today_str}

【大盘风向标（60日趋势摘要 + 最近5日，按日期倒序）】
{index_text}

【北向资金（沪深港通，近5日）】
{hsgt_text}

【同花顺行业板块涨跌幅排名】
{sector_text}

【涨幅前8板块的活跃个股（换手率前5，含基本面）】
{stocks_text}

【各板块换手率最高个股（用于判断是否发散到边缘/不正宗票）】
{dispersion_text}

【数据说明】{errors_text}
{user_hint_text}

【分析要求】
请先调用 get_sector_flow 工具获取主要板块资金净流入数据，再结合以上数据，按以下结构给出板块轮动分析。

---

1. **市场结构与浪型情景**（先判一级，后判子级）
   - 先在“五浪上行（1/2/3/4/5）”“ABC 调整（A/B/C）”“箱体震荡”“下跌趋势”四类中选择主判断；不得预设为 3 浪。
   - 给出一个主判断和最多一个备选判断，各自标注置信度（高/中/低 + 百分比）。每项必须列出至少 2 条本次数据证据和 1 条反证/不确定点。
   - 仅当一级判断为五浪或 ABC 且置信度 ≥60% 时，才继续给出子浪：五浪可写 1-1～5-5（4 浪须说明整理形态），ABC 可写 A 浪、B 反或 C 浪。否则明确写“无法可靠判浪”，禁止用猜测补全浪型。
   - 写清主判断的确认条件与失效条件：分别说明出现什么量能、指数位置、板块扩散或资金流变化后确认/推翻。量能阈值须结合本次实际数据，不可机械套用 skill 的历史数值。
   - 基于以上结论给出仓位倾向（进攻 / 均衡 / 防守 / 等待），但用“当出现 X 做 X，不出现 X 不做 X”的条件句表达。

2. **主线板块确认**（Skill 04）
   - 结合 get_sector_flow 返回的资金净流入、1/5/20 日相对强弱，当前哪 1-2 个板块是真正的主线（持续净流入 + 强度延续 + 换手活跃）？若证据不足，结论应为“暂无可确认主线”。
   - 机构行情 vs 柚子情绪行情的判断：当前哪种主导？依据是什么？
   - 是否已进入"发散到不正宗票"阶段？（参考上方换手率最高个股，判断是否偏离核心）

3. **板块轮动节点**（Skill 04 轮动规则）
   - 当前资金从哪里流出，流向哪里？（注意：钱只去下一个同风偏加速板块，不会跨风偏）
   - 是否有明确的"高标退潮信号"（如龙头出现龙虎榜/换手异常放量）？
   - 风向标龙头健康度：结合涨跌幅和换手数据，各板块龙头是否仍在正常运作？

4. **个股推荐**（每板块 1-2 只，从候选个股中选或根据你的知识补充）
   - 只有主线置信度 ≥60% 时才给出推荐；否则只给“观察池 + 触发条件”，不得硬性推荐。
   - 代码 + 名称
   - 类型（A/B/C/D/E类，参考 Skill 11）
   - 选择理由（≤3句，说明为什么是这只而非其他；换手率高≠推荐理由）
   - 操作建议：买入条件 + 止损位 + 关键风险

5. **黑名单核查**
   - 上方数据中哪些方向命中以下黑名单，需要明确回避：
     风电、战争资源、软件ETF、券商、ST/准ST、X多多概念、红利做T、量子科技、白酒/地产（科技牛市风偏不同）

注意：
- 分析基于有限数据，仅供参考，不构成投资建议
- 数据不可用的部分直接跳过，不要编造
- 策略用"当出现X做X，不出现X不做X"格式书写，避免模糊建议"""
