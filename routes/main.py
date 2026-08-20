"""
routes_main.py — 主应用路由
包含：股票查询、批量分析、持仓更新、AI 分析（含 SSE 流式）、配置管理等路由。
"""
import asyncio
import json
import os
import queue as _queue
import sqlite3
import threading as _threading
import uuid
import datetime as _dt
import re
import time as _time

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

import core.config as _cfg
from core.config import logger, DB_PATH
from core.db import (
    get_portfolio, save_portfolio, get_available_cash, save_available_cash,
    save_query_history, get_query_history,
    save_temp_result, load_temp_result,
    get_index_market_data, get_index_trend_chart_data,
    get_stock_tag, set_stock_tag, get_all_stock_tags, get_distinct_tags,
    get_all_holdings, get_prev_close,
    get_prev_closes,
    set_watch_enabled, get_watch_enabled_map, save_watch_plans, get_watch_plans,
    activate_watch_plans, get_recent_watch_events,
    update_watch_rule,
    expire_watch_plans, mark_watch_events_read,
    get_watch_plan_revisions, get_recent_watch_plan_dates,
)
from core.strategy import (
    calculate_8848, calculate_8848_history, calculate_strategy,
    get_stock_volume_chart_data, build_ai_prompt, build_ai_system_prompt, to_ts_code,
)
from services.indicators import analyze_rousu_lines, analyze_rousu_lines_intraday
from services.ai import (
    call_ai_model_with_tools, call_ai_model_streaming, call_ai_model,
    _save_ai_conversation, _load_ai_conversation,
)

# ── 常用股票管理 ─────────────────────────────────────────────────────────────

def load_common_stocks():
    raw = os.getenv("COMMON_STOCK_CODES", "") or ""
    raw = raw.replace("，", ",")
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return [{"code": code} for code in codes]


COMMON_STOCKS = load_common_stocks()

_PORTFOLIO_CACHE: dict = {"key": None, "expires_at": 0.0, "payload": None}
_PORTFOLIO_CACHE_LOCK = _threading.Lock()


def _invalidate_portfolio_cache() -> None:
    with _PORTFOLIO_CACHE_LOCK:
        _PORTFOLIO_CACHE.update({"key": None, "expires_at": 0.0, "payload": None})


# 项目根目录的 .env（和 core/config.py 读取的是同一个文件）
_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _next_weekday(today: _dt.date | None = None) -> _dt.date:
    """交易日历不可用时的保底逻辑。"""
    day = (today or _dt.date.today()) + _dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += _dt.timedelta(days=1)
    return day


_TRADE_DATE_CACHE: dict[str, _dt.date] = {}


def _next_trade_date(today: _dt.date | None = None) -> _dt.date:
    """通过 Tushare trade_cal 获取严格晚于 today 的下一个 A 股交易日。"""
    today = today or _dt.date.today()
    cache_key = today.isoformat()
    if cache_key in _TRADE_DATE_CACHE:
        return _TRADE_DATE_CACHE[cache_key]
    if _cfg.pro is not None:
        try:
            start = today + _dt.timedelta(days=1)
            end = today + _dt.timedelta(days=45)
            frame = _cfg.pro.trade_cal(
                exchange="SSE",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields="cal_date,is_open",
            )
            records = frame.to_dict("records") if frame is not None else []
            open_dates = sorted(
                str(row.get("cal_date")) for row in records
                if int(row.get("is_open") or 0) == 1 and row.get("cal_date")
            )
            if open_dates:
                value = _dt.datetime.strptime(open_dates[0], "%Y%m%d").date()
                _TRADE_DATE_CACHE[cache_key] = value
                return value
            logger.warning("trade_cal 未返回未来45天内的交易日，回退到工作日")
        except Exception as exc:
            logger.warning("trade_cal 查询失败，回退到工作日: %s", exc)
    return _next_weekday(today)


def _parse_json_array(text: str) -> list:
    """兼容模型偶尔附带代码围栏或简短说明。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("AI 未返回 JSON 数组")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, list):
        raise ValueError("AI 返回的计划不是数组")
    return value


def _update_env_key(path: str, key: str, value: str) -> None:
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
    import tushare as ts
    from core.db import get_cached_name, set_cached_name
    entries = []
    codes = [item.get("code") for item in COMMON_STOCKS if item.get("code")]
    tag_map = get_all_stock_tags(codes)
    watch_map = get_watch_enabled_map(codes)
    for code in codes:
        name = get_cached_name(code)
        if not name:
            try:
                df = ts.get_realtime_quotes(code)
                if df is not None and not df.empty:
                    name = str(df.loc[0, "name"])
                    set_cached_name(code, name)
            except Exception:
                pass
        entries.append({"code": code, "name": name, "tag": tag_map.get(code, ""),
                        "watch_enabled": watch_map.get(code, False)})
    return entries


# ── 路由注册 ─────────────────────────────────────────────────────────────────

def register(app, templates):
    _register_routes(app, templates)


def _register_routes(app, templates):

    @app.get("/", response_class=HTMLResponse)
    async def read_root(request: Request, result_id: str = None):
        defaults = {
            "common_stocks":   build_common_stocks_with_name(),
            "tag_suggestions": get_distinct_tags(),
            "batch_results":   None,
            "history_results": None,
            "stock_volume":    None,
            "index_trend":    get_index_trend_chart_data(days=20),
            "last_code":       "",
            "query_history":   get_query_history(),
            "ai_analysis":     None,
            "ai_error":        None,
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         "intraday",
            "user_hint":       "",
            "result":          None,
        }
        ctx = load_temp_result(result_id) if result_id else {}
        ctx.pop("query_history", None)
        ctx.pop("common_stocks", None)
        ctx.pop("index_trend", None)
        ctx.pop("tag_suggestions", None)
        return templates.TemplateResponse("index.html", {"request": request, **defaults, **ctx})

    @app.post("/analyze", response_class=HTMLResponse)
    async def analyze_stock(request: Request, stock_code: str = Form(...)):
        logger.info("analyze_stock: start code=%s", stock_code)
        result = calculate_8848(stock_code)
        if isinstance(result, dict) and result.get("status") == "success":
            save_query_history(result["code"], result["name"])

        history_results = calculate_8848_history(stock_code, days=20)
        stock_volume = get_stock_volume_chart_data(history_results)
        index_trend = get_index_trend_chart_data(days=20)

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
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         "intraday",
            "user_hint":       "",
        })
        return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

    @app.post("/analyze_batch", response_class=HTMLResponse)
    async def analyze_batch(request: Request):
        import concurrent.futures
        import time

        items = [item for item in COMMON_STOCKS if item.get("code")]

        def _fetch(item):
            res = calculate_8848(item["code"])
            return res if res.get("status") == "success" else None

        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            raw = await loop.run_in_executor(None, lambda: list(pool.map(_fetch, items)))
        results = [r for r in raw if r is not None]

        save_temp_result("batch_latest", {
            "batch_results": results,
            "batch_time":    time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return RedirectResponse(url="/batch", status_code=303)

    @app.get("/batch", response_class=HTMLResponse)
    async def batch_page(request: Request):
        data = load_temp_result("batch_latest", keep=True)
        today = _dt.date.today().isoformat()
        expire_watch_plans(today)
        plan_date = _next_trade_date().isoformat()
        history_dates = get_recent_watch_plan_dates(today)
        available_cash = get_available_cash()
        history_plans = []
        for history_date in history_dates:
            history_plans.append({
                "trade_date": history_date,
                "plans": get_watch_plans(history_date),
            })
        return templates.TemplateResponse("batch.html", {
            "request":       request,
            "batch_results": data.get("batch_results", []),
            "batch_time":    data.get("batch_time", ""),
            "watch_map":     get_watch_enabled_map(),
            "today_plans":   get_watch_plans(today),
            "watch_plans":   get_watch_plans(plan_date),
            "watch_events":  get_recent_watch_events(),
            "watch_revisions": get_watch_plan_revisions(today),
            "history_plan_groups": history_plans,
            "available_cash": available_cash,
            "plan_date":     plan_date,
            "plan_message":  data.get("plan_message", ""),
            "plan_error":    data.get("plan_error", ""),
        })

    @app.post("/api/watch_toggle", response_class=JSONResponse)
    async def watch_toggle(request: Request):
        body = await request.json()
        code = str(body.get("code", "")).strip()
        if not code:
            return JSONResponse({"ok": False, "error": "缺少股票代码"}, status_code=400)
        enabled = bool(body.get("enabled"))
        set_watch_enabled(code, enabled)
        return {"ok": True, "code": code, "enabled": enabled}

    @app.post("/watch_plans/generate", response_class=HTMLResponse)
    async def generate_watch_plans():
        data = load_temp_result("batch_latest", keep=True)
        results = data.get("batch_results", [])
        watch_map = get_watch_enabled_map()
        selected = [r for r in results if watch_map.get(r.get("code"), False)]
        if not selected:
            data.update({"plan_error": "请先打开至少一只股票的盯盘开关。", "plan_message": ""})
            save_temp_result("batch_latest", data)
            return RedirectResponse(url="/batch", status_code=303)

        trade_date = _next_trade_date().isoformat()
        compact_fields = (
            "code", "name", "current_price", "avg_price", "upper_line", "lower_line", "signal",
            "f382", "f618", "f786", "n20_high", "n20_low", "n40_high", "n40_low",
            "cost_price", "quantity", "stage_high", "stage_low", "atr", "ddp_lower", "ddp_f618",
        )
        compact = []
        available_cash = get_available_cash()
        for result in selected:
            item = {k: result.get(k) for k in compact_fields if result.get(k) is not None}
            portfolio = get_portfolio(str(result.get("code", "")))
            quantity = int(portfolio.get("quantity") or 0)
            cost = float(portfolio.get("cost") or result.get("cost_price") or 0)
            item.update({
                # 兼容旧数据：早期只录入了成本价、尚未填写持仓数量。
                "holding": quantity > 0 or cost > 0,
                "quantity": quantity,
                "cost_price": cost,
                "available_cash": available_cash,
            })
            current_price = float(result.get("current_price") or 0)
            item["max_buy_lots_at_current_price"] = (
                int(available_cash // (current_price * 100)) if current_price > 0 else 0
            )
            compact.append(item)
        # 该任务只需要把已计算的关键位整理成规则。不要加载完整 skills 提示，
        # 否则推理模型可能把输出额度耗在长篇分析上，来不及生成最终 JSON。
        system_prompt = """你是 A 股次日盯盘计划生成器。输入数据已由系统计算完成，无需重新推导指标。
一次处理所有股票，不调用工具，不展示推理过程，不写长篇分析。
只输出一个 JSON 数组，禁止 Markdown 代码块。每项必须包含 code、name、bias、summary、rules。
rules 仅允许 type=breakout/breakdown/near/rapid_move_5m/volume_spike；每条包含 price、confirmation_minutes(1-5)、
priority(risk/opportunity/observe)、message。每只股票最多4条，价格必须来自输入关键位或有清晰依据。
breakout默认确认3分钟，breakdown默认确认2分钟，near默认1分钟。避免相互矛盾或过密价位。
可额外使用 rapid_move_5m（price字段表示5分钟涨跌幅绝对值阈值，建议1.5）或
volume_spike（price字段表示当前分钟量/此前20分钟均量倍数，建议3.0），每只股票最多选一种异动规则。
必须按 holding 区分计划：未持仓只寻找入场机会与放弃买入/风险条件；已持仓同时考虑回落补仓机会、
冲高减仓/止盈点和跌破风控点。message 必须写清动作（观察买入、补仓、减仓、止盈或止损），
不能把未持仓股票写成卖出，也不能在没有风险退出规则时只给已持仓股票补仓规则。
available_cash 是整个账户共享的可用现金，不得对每只股票重复分配；资金不足一手时只允许观察，
message 中不得建议实际买入。涉及买入或补仓时，应结合候选价格说明最多可买手数（1手=100股），并保留手续费余量。
"""
        user_prompt = (
            f"为交易日 {trade_date} 生成盯盘计划。以下是已由本系统批量计算的数据：\n" +
            json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            raw = await asyncio.to_thread(call_ai_model, system_prompt, user_prompt)
            logger.info(
                "watch plan AI response provider=%s raw_type=%s raw_len=%d raw_preview=%r",
                _cfg.AI_PROVIDER,
                type(raw).__name__,
                len(raw or ""),
                (raw or "")[:2000],
            )
            plans = _parse_json_array(raw)
            allowed_codes = {r.get("code") for r in selected}
            plans = [p for p in plans if isinstance(p, dict) and p.get("code") in allowed_codes]
            current_by_code = {r.get("code"): r.get("current_price") for r in selected}
            for plan in plans:
                plan["_current_price"] = current_by_code.get(plan.get("code"))
            saved = save_watch_plans(plans, trade_date)
            if not saved:
                raise ValueError("AI 返回内容中没有可用规则")
            missing = len(selected) - saved
            suffix = f"；另有 {missing} 只未通过规则校验" if missing else ""
            data.update({"plan_message": f"已生成 {saved} 只股票的 {trade_date} 盯盘草稿{suffix}，请检查后启用。", "plan_error": ""})
        except Exception as exc:
            logger.exception("generate_watch_plans failed")
            data.update({"plan_error": f"生成计划失败：{exc}", "plan_message": ""})
        save_temp_result("batch_latest", data)
        return RedirectResponse(url="/batch", status_code=303)

    @app.post("/watch_plans/activate", response_class=HTMLResponse)
    async def activate_plans():
        trade_date = _next_trade_date().isoformat()
        count = activate_watch_plans(trade_date)
        data = load_temp_result("batch_latest", keep=True)
        data.update({"plan_message": f"已启用 {count} 份 {trade_date} 盯盘计划。", "plan_error": ""})
        save_temp_result("batch_latest", data)
        return RedirectResponse(url="/batch", status_code=303)

    @app.post("/api/watch_rules/{rule_id}", response_class=JSONResponse)
    async def edit_watch_rule(rule_id: int, request: Request):
        body = await request.json()
        try:
            threshold = float(body.get("price"))
            confirmation = int(body.get("confirmation_minutes", 1))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "价格或确认分钟无效"}, status_code=400)
        ok = update_watch_rule(rule_id, threshold, confirmation)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.get("/monitor_stream")
    async def monitor_stream(request: Request):
        from services.monitor import subscribe, unsubscribe, sse_payload
        q = subscribe()

        async def generate():
            try:
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.to_thread(q.get, True, 15)
                        yield sse_payload(event)
                    except _queue.Empty:
                        yield ": heartbeat\n\n"
            finally:
                unsubscribe(q)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/watch_events", response_class=JSONResponse)
    async def watch_events(after_id: int = 0, limit: int = 50):
        from services.monitor import get_monitor_health
        events = get_recent_watch_events(max(1, min(limit, 100)), max(0, after_id))
        events.reverse()
        return {"ok": True, "events": events, "health": get_monitor_health()}

    @app.post("/api/watch_events/read", response_class=JSONResponse)
    async def read_watch_events(request: Request):
        body = await request.json()
        up_to_id = body.get("up_to_id")
        count = mark_watch_events_read(int(up_to_id) if up_to_id is not None else None)
        return {"ok": True, "count": count}

    @app.get("/api/common_stocks_status")
    async def common_stocks_status():
        """返回所有自选股的实时信号和关键价位，用于自选股列表异步刷新。"""
        import concurrent.futures

        def _fetch(item):
            code = item.get("code")
            if not code:
                return None
            try:
                res = calculate_8848(code)
                if res.get("status") != "success":
                    return {"code": code, "error": True}
                # 止损价：有持仓用 cost*0.93，无持仓用 stage_low（若有），否则 None
                stop_loss = None
                cost = res.get("cost_price", 0)
                stage_low = res.get("stage_low", 0)
                stage_high = res.get("stage_high", 0)
                if cost > 0:
                    stop_loss = round(cost * 0.93, 2)
                elif stage_low > 0:
                    stop_loss = round(stage_low, 2)

                tooltip_parts = []
                if res.get("stage_params_set"):
                    tooltip_parts.append(f"区间 {stage_low}~{stage_high}")
                    tooltip_parts.append(f"F382={res['f382']} F618={res['f618']} F786={res['f786']}")
                if stop_loss is not None:
                    tooltip_parts.append(f"止损 {stop_loss}")
                if cost > 0:
                    tooltip_parts.append(f"成本 {cost}")

                return {
                    "code": code,
                    "signal": res["signal"],
                    "advice_class": res["advice_class"],
                    "current_price": res["current_price"],
                    "stop_loss": stop_loss,
                    "tooltip": " | ".join(tooltip_parts),
                    "error": False,
                }
            except Exception:
                return {"code": code, "error": True}

        loop = asyncio.get_event_loop()
        items = [item for item in COMMON_STOCKS if item.get("code")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = await loop.run_in_executor(
                None,
                lambda: list(pool.map(_fetch, items)),
            )
        results = [r for r in results if r is not None]
        return JSONResponse({"stocks": results})

    @app.post("/update_portfolio", response_class=HTMLResponse)
    async def update_portfolio(
        request: Request,
        code:       str   = Form(...),
        cost_price: float = Form(0.0),
        stage_high: float = Form(0.0),
        stage_low:  float = Form(0.0),
        max_price:  float = Form(0.0),
        quantity:   int   = Form(0),
    ):
        current = get_portfolio(code)
        effective_max_price = max_price if max_price > 0 else current["max_price"]
        save_portfolio(code, cost_price, stage_high, stage_low, effective_max_price, quantity)
        _invalidate_portfolio_cache()

        result = calculate_8848(code)
        if isinstance(result, dict) and result.get("status") == "success":
            save_query_history(result["code"], result["name"])

        history_results = calculate_8848_history(code, days=20)
        stock_volume = get_stock_volume_chart_data(history_results)
        index_trend = get_index_trend_chart_data(days=20)

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
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         "intraday",
            "user_hint":       "",
        })
        return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

    @app.post("/update_common_stocks", response_class=HTMLResponse)
    async def update_common_stocks(request: Request, codes: str = Form(...)):
        global COMMON_STOCKS
        code_list = [c.strip() for c in codes.replace("，", ",").split(",") if c.strip()]
        new_val = ",".join(code_list)
        # 先更新进程内环境变量与内存列表，再写 .env。
        # 不能只靠 load_dotenv()：无路径时可能因 cwd 找不到文件，导致 UI 仍读旧值。
        os.environ["COMMON_STOCK_CODES"] = new_val
        COMMON_STOCKS = [{"code": c} for c in code_list]
        _cfg.COMMON_STOCKS = COMMON_STOCKS
        _update_env_key(_ENV_PATH, "COMMON_STOCK_CODES", new_val)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/portfolio_overview", response_class=JSONResponse)
    async def portfolio_overview():
        holdings = get_all_holdings()
        available_cash = get_available_cash()
        if not holdings:
            return JSONResponse({"stocks": [], "total_pnl_pct": None, "today_avg_pct": None,
                                 "available_cash": available_cash, "account_equity": available_cash})

        cache_key = (available_cash, tuple((h["code"], h["cost"], h["quantity"]) for h in holdings))
        with _PORTFOLIO_CACHE_LOCK:
            if (_PORTFOLIO_CACHE["key"] == cache_key and _PORTFOLIO_CACHE["payload"] is not None
                    and _PORTFOLIO_CACHE["expires_at"] > _time.monotonic()):
                return JSONResponse(_PORTFOLIO_CACHE["payload"])

        def _load_market_data() -> tuple[dict, dict]:
            """实时行情只请求一次；失败或缺票时使用后台分钟快照。"""
            import tushare as ts
            short_codes = [h["code"].split(".")[0] for h in holdings]
            quotes: dict[str, dict] = {}
            started = _time.monotonic()
            try:
                frame = ts.get_realtime_quotes(short_codes)
                if frame is not None and not frame.empty:
                    for _, row in frame.iterrows():
                        code = str(row.get("code", "")).strip()
                        price = float(row.get("price") or 0)
                        if code and price > 0:
                            quotes[code] = {"price": price, "open": float(row.get("open") or 0),
                                            "prev_close": float(row.get("pre_close") or 0),
                                            "name": str(row.get("name") or ""), "source": "realtime"}
            except Exception as exc:
                logger.warning("portfolio_overview: batch realtime quote failed: %s", exc)
            logger.info("portfolio_overview: batch quote codes=%d found=%d elapsed=%.3fs",
                        len(short_codes), len(quotes), _time.monotonic() - started)

            missing = [code for code in short_codes if code not in quotes]
            if missing:
                conn = sqlite3.connect(DB_PATH)
                marks = ",".join("?" for _ in missing)
                today = _dt.date.today().isoformat()
                rows = conn.execute(
                    f"""SELECT s.code,s.price,s.open FROM intraday_snapshots s
                        JOIN (SELECT code,MAX(time) AS max_time FROM intraday_snapshots
                              WHERE date=? AND substr(code,1,6) IN ({marks}) GROUP BY code) x
                        ON x.code=s.code AND x.max_time=s.time WHERE s.date=?""",
                    [today, *missing, today],
                ).fetchall()
                conn.close()
                for code, price, open_ in rows:
                    if price:
                        quotes[str(code).split(".")[0]] = {
                            "price": float(price), "open": float(open_ or 0), "name": "", "source": "snapshot"
                        }
            prev_codes = list(dict.fromkeys([h["code"] for h in holdings] + short_codes))
            prev = get_prev_closes(prev_codes, before_date=_dt.date.today().isoformat())
            return quotes, prev

        quotes, prev_closes = await asyncio.to_thread(_load_market_data)
        results = []
        for holding in holdings:
            code = holding["code"]
            cost = holding["cost"]
            name = holding["name"]
            quantity = holding["quantity"]
            short_code = code.split(".")[0] if "." in code else code
            quote = quotes.get(short_code, {})
            price = quote.get("price")
            open_ = quote.get("open")
            if not name:
                name = quote.get("name", "")
            prev_close = quote.get("prev_close") or prev_closes.get(code) or prev_closes.get(short_code)

            if price is not None and cost > 0:
                total_pnl_pct = round((price - cost) / cost * 100, 2)
                total_pnl_abs = round((price - cost) * quantity, 2) if quantity > 0 else None
            else:
                total_pnl_pct = None
                total_pnl_abs = None

            if price is not None and prev_close is not None and prev_close > 0:
                today_pnl_pct = round((price - prev_close) / prev_close * 100, 2)
                today_pnl_abs = round((price - prev_close) * quantity, 2) if quantity > 0 else None
            else:
                today_pnl_pct = None
                today_pnl_abs = None

            market_value = round(price * quantity, 2) if price is not None and quantity > 0 else None

            results.append({
                "code": short_code,
                "name": name or short_code,
                "cost": cost,
                "quantity": quantity,
                "current_price": price,
                "market_value": market_value,
                "total_pnl_pct": total_pnl_pct,
                "total_pnl_abs": total_pnl_abs,
                "today_pnl_pct": today_pnl_pct,
                "today_pnl_abs": today_pnl_abs,
                "quote_source": quote.get("source"),
            })

        valid = [r for r in results if r["total_pnl_pct"] is not None]
        total_avg = round(sum(r["total_pnl_pct"] for r in valid) / len(valid), 2) if valid else None
        today_valid = [r for r in results if r["today_pnl_pct"] is not None]
        today_avg = round(sum(r["today_pnl_pct"] for r in today_valid) / len(today_valid), 2) if today_valid else None

        abs_valid = [r for r in results if r["total_pnl_abs"] is not None]
        total_abs = round(sum(r["total_pnl_abs"] for r in abs_valid), 2) if abs_valid else None
        today_abs_valid = [r for r in results if r["today_pnl_abs"] is not None]
        today_abs = round(sum(r["today_pnl_abs"] for r in today_abs_valid), 2) if today_abs_valid else None
        total_mv = round(sum(r["market_value"] for r in results if r["market_value"] is not None), 2)

        payload = {
            "stocks": results,
            "total_pnl_pct": total_avg,
            "today_avg_pct": today_avg,
            "total_pnl_abs": total_abs,
            "today_pnl_abs": today_abs,
            "total_market_value": total_mv if total_mv else None,
            "available_cash": available_cash,
            "account_equity": round(available_cash + total_mv, 2),
        }
        with _PORTFOLIO_CACHE_LOCK:
            _PORTFOLIO_CACHE.update({"key": cache_key, "expires_at": _time.monotonic() + 20, "payload": payload})
        return JSONResponse(payload)

    @app.post("/api/available_cash", response_class=JSONResponse)
    async def update_available_cash(request: Request):
        try:
            body = await request.json()
            amount = float(body.get("amount"))
        except (TypeError, ValueError, AttributeError):
            return JSONResponse({"ok": False, "error": "可用现金格式无效"}, status_code=400)
        if amount < 0 or amount > 1_000_000_000_000:
            return JSONResponse({"ok": False, "error": "可用现金必须为非负数"}, status_code=400)
        save_available_cash(round(amount, 2))
        _invalidate_portfolio_cache()
        return JSONResponse({"ok": True, "available_cash": round(amount, 2)})

    @app.post("/api/holdings_batch_save", response_class=JSONResponse)
    async def holdings_batch_save(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        items = body if isinstance(body, list) else []
        for item in items:
            code = str(item.get("code", "")).strip()
            if not code:
                continue
            cost = float(item.get("cost", 0) or 0)
            quantity = int(item.get("quantity", 0) or 0)
            current = get_portfolio(code)
            save_portfolio(
                code, cost,
                current["stage_high"], current["stage_low"],
                current["max_price"], quantity,
            )
        _invalidate_portfolio_cache()
        return JSONResponse({"ok": True, "saved": len(items)})

    @app.post("/update_stock_tag", response_class=HTMLResponse)
    async def update_stock_tag(request: Request, code: str = Form(...), tag: str = Form("")):
        set_stock_tag(code.strip(), tag.strip())
        return RedirectResponse(url="/", status_code=303)

    @app.post("/update_ai_provider", response_class=HTMLResponse)
    async def update_ai_provider(request: Request, provider: str = Form(...)):
        allowed = {"claude", "openai", "gemini"}
        provider = provider.strip().lower()
        if provider not in allowed:
            return RedirectResponse(url="/", status_code=303)
        _cfg.AI_PROVIDER = provider
        _update_env_key(_ENV_PATH, "AI_PROVIDER", provider)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/clear_history", response_class=HTMLResponse)
    async def clear_history(request: Request):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("DELETE FROM query_history")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to clear query history: {e}")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/ai_analyze", response_class=HTMLResponse)
    async def ai_analyze(request: Request, stock_code: str = Form(...), ai_mode: str = Form("intraday"), user_hint: str = Form("")):
        logger.info("ai_analyze: start code=%s provider=%s mode=%s", stock_code, _cfg.AI_PROVIDER, ai_mode)

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
                "ai_provider":     _cfg.AI_PROVIDER,
                "ai_mode":         ai_mode,
                "user_hint":       user_hint,
            })
            return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

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
            logger.warning("ai_analyze: failed to load history for %s: %s", stock_code, e)

        holding = result["cost_price"] > 0
        system_prompt = build_ai_system_prompt(ai_mode, holding)
        index_data = get_index_market_data(days=20)

        stock_daily_rousu = analyze_rousu_lines(history, n=10, label="日K")
        stock_intraday_rousu = analyze_rousu_lines_intraday(stock_code)
        index_daily_rousu = {}
        for ts_code, idx_info in index_data.items():
            idx_patterns = analyze_rousu_lines(idx_info.get("records", []), n=5, label="日K")
            index_daily_rousu[ts_code] = {"name": idx_info.get("name", ts_code), "patterns": idx_patterns}

        rousu_data = {
            "stock_daily": stock_daily_rousu,
            "stock_intraday": stock_intraday_rousu,
            "index_daily": index_daily_rousu,
        }
        user_prompt = build_ai_prompt(result, history, mode=ai_mode, user_hint=user_hint, index_data=index_data, rousu_data=rousu_data)

        ai_analysis = None
        ai_error = None
        try:
            ai_analysis = call_ai_model_with_tools(system_prompt, user_prompt)
        except Exception as e:
            ai_error = str(e)
            logger.error("ai_analyze: failed code=%s error=%s", stock_code, e)

        hist_for_chart = calculate_8848_history(stock_code, days=20)
        stock_volume = get_stock_volume_chart_data(hist_for_chart)
        index_trend = get_index_trend_chart_data(days=20)

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
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         ai_mode,
            "user_hint":       user_hint,
        })
        return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

    @app.get("/ai_stream")
    async def ai_stream(request: Request, stock_code: str, ai_mode: str = "intraday", user_hint: str = "", session_id: str = ""):
        async def generate():
            loop = asyncio.get_event_loop()

            yield "event: progress\ndata: 正在获取股票行情…\n\n"
            result = await loop.run_in_executor(None, calculate_8848, stock_code)
            if result.get("error"):
                err_msg = json.dumps({"msg": f"获取股票数据失败：{result.get('error')}"}, ensure_ascii=False)
                yield f"event: error\ndata: {err_msg}\n\n"
                return

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

            yield "event: progress\ndata: 构建分析 Prompt…\n\n"

            def _build_prompts():
                holding = result["cost_price"] > 0
                system_prompt = build_ai_system_prompt(ai_mode, holding)
                index_data = get_index_market_data(days=20)
                stock_daily_rousu = analyze_rousu_lines(history, n=10, label="日K")
                stock_intraday_rousu = analyze_rousu_lines_intraday(stock_code)
                index_daily_rousu = {}
                for ts_code, idx_info in index_data.items():
                    idx_patterns = analyze_rousu_lines(idx_info.get("records", []), n=5, label="日K")
                    index_daily_rousu[ts_code] = {"name": idx_info.get("name", ts_code), "patterns": idx_patterns}
                rousu_data = {
                    "stock_daily": stock_daily_rousu,
                    "stock_intraday": stock_intraday_rousu,
                    "index_daily": index_daily_rousu,
                }
                user_prompt = build_ai_prompt(result, history, mode=ai_mode, user_hint=user_hint, index_data=index_data, rousu_data=rousu_data)
                return system_prompt, user_prompt

            system_prompt, user_prompt = await loop.run_in_executor(None, _build_prompts)

            yield "event: progress\ndata: AI 分析中…\n\n"
            messages = [{"role": "user", "content": user_prompt}]

            full_text = ""
            try:
                q = _queue.Queue()

                def _stream_thread():
                    try:
                        for evt in call_ai_model_streaming(system_prompt, messages):
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
                        yield f"event: progress\ndata: {json.dumps({'msg': evt_data}, ensure_ascii=False)}\n\n"
                    elif evt_type == "token":
                        yield f"event: token\ndata: {json.dumps({'text': evt_data}, ensure_ascii=False)}\n\n"
                    elif evt_type == "done":
                        full_text = evt_data
                    elif evt_type == "error":
                        yield f"event: error\ndata: {json.dumps({'msg': evt_data}, ensure_ascii=False)}\n\n"
                        return
            except Exception as e:
                logger.error("ai_stream: AI call failed %s", e)
                yield f"event: error\ndata: {json.dumps({'msg': str(e)}, ensure_ascii=False)}\n\n"
                return

            sid = session_id or str(uuid.uuid4())
            conv_messages = [
                {"role": "user",      "content": user_prompt},
                {"role": "assistant", "content": full_text},
            ]
            await loop.run_in_executor(None, _save_ai_conversation, sid, stock_code, conv_messages)
            yield f"event: done\ndata: {json.dumps({'session_id': sid, 'provider': _cfg.AI_PROVIDER}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/ai_chat")
    async def ai_chat(request: Request):
        body = await request.json()
        session_id = body.get("session_id", "")
        stock_code = body.get("stock_code", "")
        user_message = body.get("message", "").strip()

        if not session_id or not user_message:
            async def _err():
                yield f"event: error\ndata: {json.dumps({'msg': '缺少 session_id 或 message'})}\n\n"
            return StreamingResponse(_err(), media_type="text/event-stream")

        async def generate():
            loop = asyncio.get_event_loop()

            conv_messages = await loop.run_in_executor(None, _load_ai_conversation, session_id)
            if not conv_messages:
                yield f"event: error\ndata: {json.dumps({'msg': '会话已过期，请重新发起 AI 分析'}, ensure_ascii=False)}\n\n"
                return

            conv_messages.append({"role": "user", "content": user_message})

            def _load_sys():
                portfolio = get_portfolio(stock_code) if stock_code else {}
                holding = (portfolio.get("cost_price") or 0) > 0
                return build_ai_system_prompt("intraday", holding)
            system_prompt = await loop.run_in_executor(None, _load_sys)

            full_text = ""
            try:
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
                        yield f"event: progress\ndata: {json.dumps({'msg': evt_data}, ensure_ascii=False)}\n\n"
                    elif evt_type == "token":
                        yield f"event: token\ndata: {json.dumps({'text': evt_data}, ensure_ascii=False)}\n\n"
                    elif evt_type == "done":
                        full_text = evt_data
                    elif evt_type == "error":
                        yield f"event: error\ndata: {json.dumps({'msg': evt_data}, ensure_ascii=False)}\n\n"
                        return
            except Exception as e:
                logger.error("ai_chat: failed %s", e)
                yield f"event: error\ndata: {json.dumps({'msg': str(e)}, ensure_ascii=False)}\n\n"
                return

            conv_messages.append({"role": "assistant", "content": full_text})
            await loop.run_in_executor(None, _save_ai_conversation, session_id, stock_code, conv_messages)
            yield f"event: done\ndata: {json.dumps({'session_id': session_id, 'provider': _cfg.AI_PROVIDER}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
