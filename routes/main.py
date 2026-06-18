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

from dotenv import load_dotenv
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

import core.config as _cfg
from core.config import logger, DB_PATH
from core.db import (
    get_portfolio, save_portfolio,
    save_query_history, get_query_history,
    save_temp_result, load_temp_result,
    get_index_market_data, get_index_trend_chart_data,
)
from core.strategy import (
    calculate_8848, calculate_8848_history, calculate_strategy,
    get_stock_volume_chart_data, build_ai_prompt, load_skills, to_ts_code,
)
from services.tools import (
    analyze_rousu_lines, analyze_rousu_lines_intraday,
    call_ai_model_with_tools, call_ai_model_streaming,
    _save_ai_conversation, _load_ai_conversation,
)

# ── 常用股票管理 ─────────────────────────────────────────────────────────────

def load_common_stocks():
    raw = os.getenv("COMMON_STOCK_CODES", "") or ""
    raw = raw.replace("，", ",")
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return [{"code": code} for code in codes]


COMMON_STOCKS = load_common_stocks()


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
    for item in COMMON_STOCKS:
        code = item.get("code")
        if not code:
            continue
        name = get_cached_name(code)
        if not name:
            try:
                df = ts.get_realtime_quotes(code)
                if df is not None and not df.empty:
                    name = str(df.loc[0, "name"])
                    set_cached_name(code, name)
            except Exception:
                pass
        entries.append({"code": code, "name": name})
    return entries


# ── 路由注册 ─────────────────────────────────────────────────────────────────

def register(app, templates):
    _register_routes(app, templates)


def _register_routes(app, templates):

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
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         "intraday",
            "user_hint":       "",
            "result":          None,
        }
        ctx = load_temp_result(result_id) if result_id else {}
        ctx.pop("query_history", None)
        ctx.pop("common_stocks", None)
        ctx.pop("index_trend", None)
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
            "ai_provider":     _cfg.AI_PROVIDER,
            "ai_mode":         "intraday",
            "user_hint":       "",
        })
        return RedirectResponse(url=f"/?result_id={rid}", status_code=303)

    @app.post("/update_portfolio", response_class=HTMLResponse)
    async def update_portfolio(
        request: Request,
        code:       str   = Form(...),
        cost_price: float = Form(0.0),
        stage_high: float = Form(0.0),
        stage_low:  float = Form(0.0),
        max_price:  float = Form(0.0),
    ):
        current = get_portfolio(code)
        effective_max_price = max_price if max_price > 0 else current["max_price"]
        save_portfolio(code, cost_price, stage_high, stage_low, effective_max_price)

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
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        _update_env_key(env_path, "COMMON_STOCK_CODES", new_val)
        load_dotenv(override=True)
        COMMON_STOCKS = load_common_stocks()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/update_ai_provider", response_class=HTMLResponse)
    async def update_ai_provider(request: Request, provider: str = Form(...)):
        allowed = {"claude", "openai", "gemini"}
        provider = provider.strip().lower()
        if provider not in allowed:
            return RedirectResponse(url="/", status_code=303)
        _cfg.AI_PROVIDER = provider
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        _update_env_key(env_path, "AI_PROVIDER", provider)
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

        skills_text = load_skills()
        system_prompt = (
            "你是基于阿狼投资体系的 A 股分析助手。"
            "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
            + skills_text
        )
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
                skills_text = load_skills()
                system_prompt = (
                    "你是基于阿狼投资体系的 A 股分析助手。"
                    "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
                    + skills_text
                )
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
                skills_text = load_skills()
                return (
                    "你是基于阿狼投资体系的 A 股分析助手。"
                    "以下是阿狼投资体系的技能库，请在分析中主要参考它，但也可以结合你自己的知识库进行补充和对比分析，以提供更全面的见解：\n\n"
                    + skills_text
                )
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
