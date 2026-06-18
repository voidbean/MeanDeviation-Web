import time
import sqlite3
import uuid

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config as _cfg
from config import logger, DB_PATH, pro
from db import (
    get_cached_name,
    save_temp_result, load_temp_result,
    get_index_market_data,
)
from tools import call_ai_model

# app 和 templates 在注册时注入
_app = None
_templates = None


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

        from app import load_skills, to_ts_code
        skills_text = load_skills()
        system_prompt = (
            "你是基于阿狼投资体系的 A 股板块轮动分析助手。"
            "以下是阿狼投资体系的技能库，请在分析中参考它，但也要结合你自己的知识进行独立判断，"
            "不要过度依赖框架导致分析僵化：\n\n"
            + skills_text
        )
        user_prompt = build_sector_ai_prompt(sector_data, user_hint=user_hint)

        ai_analysis = None
        ai_error = None
        try:
            ai_analysis = call_ai_model(system_prompt, user_prompt)
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
    from app import to_ts_code

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
    start_date = (today - timedelta(days=15)).strftime("%Y%m%d")
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
                        "amount":     float(row.get("turnover_rate", 0) or 0),
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
        pct_5d = 0.0
        if len(records_sorted) >= 2:
            cum = 1.0
            for r in records_sorted[:5]:
                cum *= (1 + r["pct_change"] / 100)
            pct_5d = round((cum - 1) * 100, 2)
        perf_list.append({
            "ts_code":       ts_code,
            "name":          sector_code_to_name.get(ts_code, ts_code),
            "pct_change_1d": round(pct_1d, 2),
            "pct_change_5d": pct_5d,
            "days_data":     len(records_sorted),
        })

    perf_list.sort(key=lambda x: x["pct_change_5d"], reverse=True)
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

    index_data = get_index_market_data(days=10)
    index_sections = []
    for ts_code, idata in index_data.items():
        name = idata.get("name", ts_code)
        records = idata.get("records", [])
        if records:
            lines = "\n".join(
                f"  {r['date']}: 收{r['close']} 高{r['high']} 低{r['low']} 成交额{r['amount_yi']}亿"
                for r in records[:5]
            )
            index_sections.append(f"{name}（{ts_code}）近5日：\n{lines}")
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
            f"  {i+1}. {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}%  今日{s['pct_change_1d']:+.2f}%"
            for i, s in enumerate(top5d)
        )
        bottom5d = sorted(data["sector_perf"], key=lambda x: x["pct_change_5d"])[:5]
        perf_lines_bottom = "\n".join(
            f"  {s['name']}（{s['ts_code']}）: 近5日{s['pct_change_5d']:+.2f}%  今日{s['pct_change_1d']:+.2f}%"
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

    return f"""【分析日期】{today_str}

【大盘风向标（近5日，按日期倒序）】
{index_text}

【北向资金（沪深港通，近5日）】
{hsgt_text}

【同花顺行业板块涨跌幅排名】
{sector_text}

【涨幅前8板块的活跃个股（换手率前5，含基本面）】
{stocks_text}

【数据说明】{errors_text}
{user_hint_text}

【分析要求】
你是一位基于阿狼投资体系的 A 股分析师。请根据以上数据，给出板块轮动分析和个股推荐。

以下是分析框架（仅供参考，请结合你自己的判断，不要机械套用）：

1. **大盘阶段判断**（参考 Skill 01 的 3-X 框架）：根据三大指数的量能和价格走势，当前大盘处于哪个阶段？对操作有何影响？

2. **当前主线板块**：根据近期涨跌幅和资金流向，哪 2-3 个板块处于主升或启动阶段？请说明判断依据（量能持续性、资金来源）。

3. **板块轮动方向**（参考 Skill 04 的轮动逻辑）：资金从哪里流出，往哪里流入？当前处于哪个轮动节点？

4. **个股推荐**（每个推荐板块 1-2 只，从上方候选个股中选择或根据你的知识补充）：
   - 股票代码 + 名称
   - 类型判断（参考 Skill 11：A/B/C/D/E 类）
   - 推荐理由（不超过 3 句，重点说明为什么是这只而不是其他）
   - 操作建议（买入条件 / 关键风险提示）

5. **不建议参与的方向**：当前哪些板块或个股应该回避？原因是什么？

注意：
- 以上分析基于有限的量化数据，仅供参考，不构成投资建议
- 如果某类数据不可用，请跳过依赖该数据的分析，不要编造数据
- 个股推荐要有明确的选择理由，不要仅因为涨幅高就推荐"""
