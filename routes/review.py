import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from core.config import logger, DB_PATH
from core.db import (
    get_query_history,
    save_temp_result, load_temp_result,
    get_klines_around_date,
)
from services.ai import call_ai_model_with_tools

TRADING_PROFILE_PATH = Path(__file__).parent / "skills" / "personal" / "trading_profile.md"


def register(app, templates):
    _register_routes(app, templates)


def _register_routes(app, templates):

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, result_id: str = None, type: str = None,
                          imported: int = None, import_error: int = None):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            trades = conn.execute(
                "SELECT * FROM trade_log ORDER BY trade_time DESC"
            ).fetchall()
        finally:
            conn.close()

        ai_result = None
        analyzed_trade_id = None
        stage_result = None
        stage_start = None
        stage_end = None
        stage_count = 0

        if result_id:
            data = load_temp_result(result_id)
            if type == "stage":
                stage_result = data.get("stage_result")
                stage_start  = data.get("stage_start")
                stage_end    = data.get("stage_end")
                stage_count  = data.get("stage_count", 0)
            else:
                ai_result = data.get("review_result")
                analyzed_trade_id = data.get("trade_id")

        raw_history = get_query_history()
        seen_codes = set()
        deduped_history = []
        for item in raw_history:
            if item["code"] not in seen_codes:
                seen_codes.add(item["code"])
                deduped_history.append(item)

        return templates.TemplateResponse("review.html", {
            "request":           request,
            "trades":            [dict(t) for t in trades],
            "ai_result":         ai_result,
            "analyzed_trade_id": analyzed_trade_id,
            "stage_result":      stage_result,
            "stage_start":       stage_start,
            "stage_end":         stage_end,
            "stage_count":       stage_count,
            "query_history":     deduped_history,
            "imported":          imported,
            "import_error":      import_error,
        })

    @app.post("/review/add", response_class=HTMLResponse)
    async def review_add(
        request:    Request,
        code:       str   = Form(...),
        trade_time: str   = Form(...),
        direction:  str   = Form(...),
        price:      float = Form(...),
        volume:     int   = Form(...),
        thought:    str   = Form(""),
        emotion:    str   = Form("冷静"),
    ):
        from core.strategy import to_ts_code
        from core.db import get_cached_name
        trade_time = trade_time.replace("T", " ")
        ts_code = to_ts_code(code)
        name = get_cached_name(ts_code) or ts_code

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT INTO trade_log (code, name, trade_time, direction, price, volume, thought, emotion) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_code, name, trade_time, direction, price, volume, thought, emotion)
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/analyze", response_class=HTMLResponse)
    async def review_analyze(request: Request, trade_id: int = Form(...)):
        from core.strategy import build_review_prompt
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM trade_log WHERE id=?", (trade_id,)).fetchone()
        finally:
            conn.close()

        if not row:
            return RedirectResponse("/review", status_code=303)

        trade = dict(row)
        trade_date = trade["trade_time"][:10]

        stock_klines = get_klines_around_date(trade["code"], trade_date, n=10)
        index_klines = get_klines_around_date("000001.SH", trade_date, n=10)

        system_prompt, user_prompt = build_review_prompt(trade, stock_klines, index_klines)

        logger.info("review_analyze: start trade_id=%s code=%s", trade_id, trade["code"])
        try:
            result = call_ai_model_with_tools(system_prompt, user_prompt)
        except Exception as e:
            logger.error("review_analyze: AI call failed: %s", e)
            result = f"AI 分析失败：{e}"

        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "UPDATE trade_log SET review_result=?, reviewed_at=datetime('now','localtime') WHERE id=?",
                (result, trade_id)
            )
            conn.commit()
        finally:
            conn.close()

        rid = str(uuid.uuid4())
        save_temp_result(rid, {"review_result": result, "trade_id": str(trade_id)})
        return RedirectResponse(f"/review?result_id={rid}", status_code=303)

    @app.post("/review/stage_analyze", response_class=HTMLResponse)
    async def review_stage_analyze(
        request:    Request,
        start_date: str = Form(...),
        end_date:   str = Form(...),
    ):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            trades = conn.execute(
                "SELECT * FROM trade_log WHERE date(trade_time) BETWEEN ? AND ? ORDER BY trade_time ASC",
                (start_date, end_date)
            ).fetchall()
        finally:
            conn.close()

        trades = [dict(t) for t in trades]
        if not trades:
            rid = str(uuid.uuid4())
            save_temp_result(rid, {
                "stage_result": "所选时间范围内没有交易记录，请重新选择日期范围。",
                "stage_start": start_date,
                "stage_end": end_date,
                "stage_count": 0,
            })
            return RedirectResponse(f"/review?result_id={rid}&type=stage", status_code=303)

        klines_map = {}
        for t in trades:
            trade_date = t["trade_time"][:10]
            klines_map[t["id"]] = get_klines_brief(t["code"], trade_date, before_n=5)

        from core.strategy import load_skills
        system_prompt, user_prompt = build_stage_review_prompt(trades, klines_map, load_skills())

        logger.info("stage_analyze: start=%s end=%s count=%d", start_date, end_date, len(trades))
        try:
            result = call_ai_model_with_tools(system_prompt, user_prompt)
        except Exception as e:
            logger.error("stage_analyze: AI call failed: %s", e)
            result = f"AI 分析失败：{e}"

        rid = str(uuid.uuid4())
        save_temp_result(rid, {
            "stage_result": result,
            "stage_start":  start_date,
            "stage_end":    end_date,
            "stage_count":  len(trades),
        })
        return RedirectResponse(f"/review?result_id={rid}&type=stage", status_code=303)

    @app.post("/review/extract_profile", response_class=HTMLResponse)
    async def review_extract_profile(
        request:     Request,
        review_text: str = Form(...),
    ):
        current_profile = TRADING_PROFILE_PATH.read_text(encoding="utf-8") \
            if TRADING_PROFILE_PATH.exists() else "（文件不存在）"

        system_prompt, user_prompt = build_extract_profile_prompt(review_text, current_profile)

        logger.info("extract_profile: calling AI to generate profile update")
        try:
            ai_output = call_ai_model_with_tools(system_prompt, user_prompt)
        except Exception as e:
            logger.error("extract_profile: AI call failed: %s", e)
            ai_output = f"AI 分析失败：{e}"

        separator = "---PROFILE---"
        if separator in ai_output:
            parts = ai_output.split(separator, 1)
            new_profile = parts[0].strip()
            change_summary = parts[1].strip()
        else:
            new_profile = ai_output.strip()
            change_summary = "（AI 未按格式输出变更说明，请人工核对上方内容）"

        rid = str(uuid.uuid4())
        save_temp_result(rid, {
            "current_profile": current_profile,
            "new_profile":     new_profile,
            "change_summary":  change_summary,
        })
        return RedirectResponse(f"/review/profile_preview?result_id={rid}", status_code=303)

    @app.get("/review/profile_preview", response_class=HTMLResponse)
    async def review_profile_preview(request: Request, result_id: str):
        data = load_temp_result(result_id)
        if not data:
            return RedirectResponse("/review", status_code=303)

        rid2 = str(uuid.uuid4())
        save_temp_result(rid2, data)

        return templates.TemplateResponse("profile_preview.html", {
            "request":         request,
            "current_profile": data.get("current_profile", ""),
            "new_profile":     data.get("new_profile", ""),
            "change_summary":  data.get("change_summary", ""),
            "confirm_rid":     rid2,
        })

    @app.post("/review/confirm_profile", response_class=HTMLResponse)
    async def review_confirm_profile(
        request:     Request,
        new_profile: str = Form(...),
    ):
        try:
            TRADING_PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            TRADING_PROFILE_PATH.write_text(new_profile, encoding="utf-8")
            logger.info("confirm_profile: trading_profile.md updated (%d chars)", len(new_profile))
            success = True
            error_msg = ""
        except Exception as e:
            logger.error("confirm_profile: write failed: %s", e)
            success = False
            error_msg = str(e)

        return templates.TemplateResponse("profile_confirm_result.html", {
            "request":   request,
            "success":   success,
            "error_msg": error_msg,
        })

    @app.get("/review/edit", response_class=HTMLResponse)
    async def review_edit_get(request: Request, id: int):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM trade_log WHERE id=?", (id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return RedirectResponse("/review", status_code=303)
        return JSONResponse(dict(row))

    @app.post("/review/edit", response_class=HTMLResponse)
    async def review_edit_post(
        request:    Request,
        trade_id:   int   = Form(...),
        trade_time: str   = Form(...),
        direction:  str   = Form(...),
        price:      float = Form(...),
        volume:     int   = Form(...),
        thought:    str   = Form(""),
        emotion:    str   = Form("冷静"),
    ):
        trade_time = trade_time.replace("T", " ")
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "UPDATE trade_log SET trade_time=?, direction=?, price=?, volume=?, thought=?, emotion=? WHERE id=?",
                (trade_time, direction, price, volume, thought, emotion, trade_id)
            )
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/review", status_code=303)

    @app.post("/review/delete", response_class=HTMLResponse)
    async def review_delete(request: Request, trade_id: int = Form(...)):
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM trade_log WHERE id=?", (trade_id,))
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/review", status_code=303)

    @app.get("/review/export")
    async def review_export():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM trade_log ORDER BY trade_time ASC").fetchall()
        finally:
            conn.close()
        data = [dict(r) for r in rows]
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=trade_log.json"},
        )

    @app.post("/review/import", response_class=HTMLResponse)
    async def review_import(request: Request, file: UploadFile = File(...)):
        try:
            raw = await file.read()
            records = json.loads(raw)
            if not isinstance(records, list):
                raise ValueError("JSON 根节点需为数组")
        except Exception as e:
            logger.error("review_import: parse error %s", e)
            return RedirectResponse("/review?import_error=1", status_code=303)

        required = {"code", "name", "trade_time", "direction", "price", "volume"}
        inserted = 0
        conn = sqlite3.connect(DB_PATH)
        try:
            for r in records:
                if not required.issubset(r.keys()):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO trade_log
                       (id, code, name, trade_time, direction, price, volume, thought, emotion,
                        review_result, reviewed_at, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        r.get("id"),
                        r["code"], r["name"], r["trade_time"],
                        r["direction"], r["price"], r["volume"],
                        r.get("thought", ""), r.get("emotion", "冷静"),
                        r.get("review_result"), r.get("reviewed_at"),
                        r.get("created_at"),
                    ),
                )
                inserted += conn.execute("SELECT changes()").fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        logger.info("review_import: inserted %d records", inserted)
        return RedirectResponse(f"/review?imported={inserted}", status_code=303)


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

def get_klines_brief(code: str, center_date: str, before_n: int = 5) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        today_row = conn.execute(
            "SELECT date, COALESCE(open, close) AS open, high, low, close "
            "FROM daily_records WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, center_date)
        ).fetchone()
        prev_rows = conn.execute(
            "SELECT date, close FROM daily_records WHERE code=? AND date<? ORDER BY date DESC LIMIT ?",
            (code, center_date, before_n)
        ).fetchall()
        return {
            "today": dict(today_row) if today_row else None,
            "prev":  [dict(r) for r in reversed(prev_rows)],
        }
    finally:
        conn.close()


def build_stage_review_prompt(trades: list, klines_map: dict, skills_text: str) -> tuple:
    system_prompt = (
        "你是一位严格的交易复盘导师，使用阿狼交易体系标准进行阶段性复盘分析。\n\n"
        + skills_text
        + "\n\n## 阶段复盘分析框架\n\n"
        "你将收到一段时间内的多笔交易记录（可能涉及多只股票）。\n"
        "你的任务是：\n"
        "1. **逐笔简评**：用一行表格对每笔操作给出简短评价（不超过30字）和评分（A/B/C/D）\n"
        "2. **行为模式归纳**：从整体角度找出该交易者的行为规律，重点关注：\n"
        "   - 是否存在追涨停板行为\n"
        "   - 止损执行率（有没有死扛）\n"
        "   - 大盘弱势时是否仍频繁操作\n"
        "   - 情绪状态（冷静/冲动/纠结）与操作质量的相关性\n"
        "   - 买卖点选择的系统性偏差\n"
        "3. **核心结论**：用3条以内的要点总结该交易者最需要改进的地方\n\n"
        "输出格式：Markdown，先逐笔简评表格，再整体规律总结，语气直接不客套。"
    )

    def fmt_brief_klines(kdata: dict, trade_date: str) -> str:
        if not kdata:
            return "（无K线数据）"
        parts = []
        prev = kdata.get("prev", [])
        if prev:
            closes = "、".join(f"{r['date'][-5:]}收{r['close']}" for r in prev)
            parts.append(f"前{len(prev)}日收盘：{closes}")
        today = kdata.get("today")
        if today:
            parts.append(
                f"操作日（{today['date']}）：开{today['open']} 高{today['high']} "
                f"低{today['low']} 收{today['close']}"
            )
        return "；".join(parts) if parts else "（无K线数据）"

    trade_blocks = []
    for i, t in enumerate(trades, 1):
        trade_date = t["trade_time"][:10]
        kdata = klines_map.get(t["id"], {})
        kline_text = fmt_brief_klines(kdata, trade_date)
        block = (
            f"### 操作 {i}：{t['name']}（{t['code']}）\n"
            f"- 时间：{t['trade_time'][:16]}　方向：{t['direction']}　"
            f"价格：{t['price']} 元　手数：{t['volume']} 手\n"
            f"- 情绪：{t['emotion']}　当时想法：{t['thought'] or '（未记录）'}\n"
            f"- K线参考：{kline_text}"
        )
        trade_blocks.append(block)

    date_range = f"{trades[0]['trade_time'][:10]} ~ {trades[-1]['trade_time'][:10]}" if trades else "未知"
    user_prompt = (
        f"## 阶段复盘请求\n\n"
        f"**时间范围**：{date_range}　**操作笔数**：{len(trades)} 笔\n\n"
        "---\n\n"
        + "\n\n".join(trade_blocks)
        + "\n\n---\n\n请按阶段复盘框架，先给出逐笔简评表格，再做整体行为模式归纳和核心结论。"
    )
    return system_prompt, user_prompt


def build_extract_profile_prompt(review_text: str, current_profile: str) -> tuple:
    system_prompt = (
        "你是一位专业的交易行为分析师，擅长从交易复盘记录中提炼交易者的行为模式，"
        "并将其整理为结构化的个人交易画像文件。\n\n"
        "## 你的任务\n\n"
        "根据本次复盘结果，更新交易者的个人交易画像（Markdown 格式）。\n\n"
        "## 输出要求\n\n"
        "请输出两个部分，用 `---PROFILE---` 分隔符隔开：\n\n"
        "**第一部分**：完整的新版 `trading_profile.md` 内容（直接可写入文件的 Markdown）\n\n"
        "`---PROFILE---`\n\n"
        "**第二部分**：变更说明（相比旧版，新增了哪些条目、修改了哪些条目、删除了哪些条目）\n\n"
        "## 注意事项\n\n"
        "- 保留旧版中已有的有效内容，不要随意删除\n"
        "- 新增条目要有充分的复盘依据，不要过度归纳\n"
        "- 语言简洁，每条不超过50字\n"
        "- 如果本次复盘没有新的值得记录的模式，可以保持原有内容不变，并在变更说明中注明"
    )
    user_prompt = (
        f"## 当前个人交易画像\n\n{current_profile}\n\n"
        "---\n\n"
        f"## 本次复盘结果\n\n{review_text}\n\n"
        "---\n\n"
        "请根据本次复盘结果，生成更新后的个人交易画像，并说明变更内容。"
    )
    return system_prompt, user_prompt
