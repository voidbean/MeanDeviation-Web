"""
services/ai.py — AI 模型调用层（支持 Claude / OpenAI / Gemini）
包含：带工具的同步调用、流式生成、无工具调用、会话持久化
"""
import json
import sqlite3
import time

import core.config as _cfg
from core.config import DB_PATH, logger
from services.tushare_tools import (
    MAX_TOOL_ROUNDS,
    execute_tool,
    _build_claude_tools,
    _build_openai_tools,
    _build_gemini_tools,
)

_MAX_TOKENS = 4096


# ── 会话持久化 ────────────────────────────────────────────────────────────────

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


# ── 带工具的同步调用 ──────────────────────────────────────────────────────────

def call_ai_model_with_tools(system_prompt: str, user_prompt: str) -> str:
    provider = _cfg.AI_PROVIDER

    if provider == "claude":
        import anthropic
        kwargs: dict = {"api_key": _cfg.CLAUDE_API_KEY, "timeout": 180.0, "default_headers": {"api-key": _cfg.CLAUDE_API_KEY}}
        if _cfg.CLAUDE_BASE_URL:
            kwargs["base_url"] = _cfg.CLAUDE_BASE_URL
        client = anthropic.Anthropic(**kwargs)
        claude_tools = _build_claude_tools()
        messages = [{"role": "user", "content": user_prompt}]
        for _round in range(MAX_TOOL_ROUNDS):
            resp = client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system_prompt,
                tools=claude_tools,
                messages=messages,
            )
            logger.info("claude tool_use round=%d stop_reason=%s", _round, resp.stop_reason)
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
                if not response_content:
                    return "AI model returned no content."
                return "".join(b.text for b in response_content if hasattr(b, "text"))
        logger.warning("claude tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        return "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))

    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": _cfg.OPENAI_API_KEY, "timeout": 180.0}
        if _cfg.OPENAI_BASE_URL:
            kwargs["base_url"] = _cfg.OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        openai_tools = _build_openai_tools()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ]
        for _round in range(MAX_TOOL_ROUNDS):
            resp = client.chat.completions.create(
                model=_cfg.OPENAI_MODEL,
                max_tokens=_cfg.OPENAI_MAX_TOKENS,
                tools=openai_tools,
                messages=messages,
            )
            choice = resp.choices[0]
            logger.info("openai tool_use round=%d finish_reason=%s", _round, choice.finish_reason)
            if choice.finish_reason == "length":
                logger.warning("openai tool_use truncated by max_tokens=%d", _cfg.OPENAI_MAX_TOKENS)
                return choice.message.content or ""
            if choice.finish_reason == "stop":
                return choice.message.content or ""
            if choice.finish_reason == "tool_calls":
                msg = choice.message
                messages.append(msg.model_dump())
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result_str = execute_tool(tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
            else:
                return choice.message.content or ""
        logger.warning("openai tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        return resp.choices[0].message.content or ""

    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=_cfg.GEMINI_API_KEY)
        gemini_tool = _build_gemini_tools()
        model = genai.GenerativeModel(
            model_name=_cfg.GEMINI_MODEL,
            system_instruction=system_prompt,
            tools=[gemini_tool],
        )
        chat = model.start_chat()
        resp = chat.send_message(
            user_prompt,
            generation_config=genai_types.GenerationConfig(max_output_tokens=_MAX_TOKENS),
            request_options={"timeout": 180},
        )
        for _round in range(MAX_TOOL_ROUNDS):
            fc_parts = [p for p in resp.parts if p.function_call.name]
            logger.info("gemini tool_use round=%d fc_count=%d", _round, len(fc_parts))
            if not fc_parts:
                return resp.text
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
                generation_config=genai_types.GenerationConfig(max_output_tokens=_MAX_TOKENS),
                request_options={"timeout": 180},
            )
        logger.warning("gemini tool_use exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        return resp.text

    else:
        raise ValueError(f"不支持的 AI_PROVIDER: {provider}，请设置为 claude / openai / gemini")


# ── 流式生成（带工具） ────────────────────────────────────────────────────────

def call_ai_model_streaming(system_prompt: str, messages: list):
    """
    带工具调用的流式生成器。
    yield ("progress", msg) / ("token", chunk) / ("done", full_text) / ("error", msg)
    messages: OpenAI 格式消息列表（不含 system），支持多轮对话。
    """
    provider = _cfg.AI_PROVIDER

    if provider == "claude":
        import anthropic
        kwargs: dict = {"api_key": _cfg.CLAUDE_API_KEY, "timeout": 180.0, "default_headers": {"api-key": _cfg.CLAUDE_API_KEY}}
        if _cfg.CLAUDE_BASE_URL:
            kwargs["base_url"] = _cfg.CLAUDE_BASE_URL
        client = anthropic.Anthropic(**kwargs)
        claude_tools = _build_claude_tools()

        claude_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] in ("user", "assistant")
        ]

        for _round in range(MAX_TOOL_ROUNDS):
            yield ("progress", f"AI 第 {_round + 1} 轮推理中，请稍候…")
            resp = client.messages.create(
                model=_cfg.CLAUDE_MODEL,
                max_tokens=_MAX_TOKENS,
                system=system_prompt,
                tools=claude_tools,
                messages=claude_messages,
            )
            logger.info("claude streaming round=%d stop_reason=%s", _round, resp.stop_reason)
            response_content = resp.content or []

            if resp.stop_reason == "end_turn":
                full_text = "".join(b.text for b in response_content if hasattr(b, "text"))
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
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
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
                yield ("done", full_text)
                return

        logger.warning("claude streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        full_text = "".join(b.text for b in (resp.content or []) if hasattr(b, "text"))
        yield ("done", full_text)

    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": _cfg.OPENAI_API_KEY, "timeout": 180.0}
        if _cfg.OPENAI_BASE_URL:
            kwargs["base_url"] = _cfg.OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        openai_tools = _build_openai_tools()
        oai_messages = [{"role": "system", "content": system_prompt}] + list(messages)

        for _round in range(MAX_TOOL_ROUNDS):
            yield ("progress", f"AI 第 {_round + 1} 轮推理中，请稍候…")
            resp = client.chat.completions.create(
                model=_cfg.OPENAI_MODEL,
                max_tokens=_cfg.OPENAI_MAX_TOKENS,
                tools=openai_tools,
                messages=oai_messages,
            )
            choice = resp.choices[0]
            logger.info("openai streaming round=%d finish_reason=%s", _round, choice.finish_reason)

            if choice.finish_reason == "length":
                logger.warning("openai streaming truncated by max_tokens=%d", _cfg.OPENAI_MAX_TOKENS)
                full_text = choice.message.content or ""
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
                yield ("done", full_text)
                return

            if choice.finish_reason == "stop":
                full_text = choice.message.content or ""
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
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
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
                yield ("done", full_text)
                return

        logger.warning("openai streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        full_text = resp.choices[0].message.content or ""
        yield ("done", full_text)

    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=_cfg.GEMINI_API_KEY)
        gemini_tool = _build_gemini_tools()
        model_obj = genai.GenerativeModel(
            model_name=_cfg.GEMINI_MODEL,
            system_instruction=system_prompt,
            tools=[gemini_tool],
        )
        chat = model_obj.start_chat()

        history_msgs = list(messages)
        last_user = None
        for i in range(len(history_msgs) - 1, -1, -1):
            if history_msgs[i]["role"] == "user":
                last_user = history_msgs.pop(i)
                break
        for m in history_msgs:
            try:
                chat.send_message(m["content"], generation_config=genai_types.GenerationConfig(max_output_tokens=1))
            except Exception:
                pass

        user_content = last_user["content"] if last_user else ""
        yield ("progress", "AI 第 1 轮推理中，请稍候…")
        resp = chat.send_message(
            user_content,
            generation_config=genai_types.GenerationConfig(max_output_tokens=_MAX_TOKENS),
            request_options={"timeout": 180},
        )

        for _round in range(MAX_TOOL_ROUNDS):
            fc_parts = [p for p in resp.parts if p.function_call.name]
            logger.info("gemini streaming round=%d fc_count=%d", _round, len(fc_parts))
            if not fc_parts:
                full_text = resp.text
                for i in range(0, len(full_text), 4):
                    yield ("token", full_text[i:i+4])
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
                generation_config=genai_types.GenerationConfig(max_output_tokens=_MAX_TOKENS),
                request_options={"timeout": 180},
            )

        logger.warning("gemini streaming exceeded MAX_TOOL_ROUNDS=%d", MAX_TOOL_ROUNDS)
        yield ("done", resp.text)

    else:
        yield ("error", f"不支持的 AI_PROVIDER: {provider}")
        yield ("done", "")


# ── 无工具的简单调用 ──────────────────────────────────────────────────────────

def call_ai_model(system_prompt: str, user_prompt: str) -> str:
    provider = _cfg.AI_PROVIDER

    if provider == "claude":
        import anthropic
        kwargs: dict = {"api_key": _cfg.CLAUDE_API_KEY, "timeout": 120.0, "default_headers": {"api-key": _cfg.CLAUDE_API_KEY}}
        if _cfg.CLAUDE_BASE_URL:
            kwargs["base_url"] = _cfg.CLAUDE_BASE_URL
        client = anthropic.Anthropic(**kwargs)
        msg = client.messages.create(
            model=_cfg.CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(b.text for b in (msg.content or []) if hasattr(b, "text"))

    elif provider == "openai":
        from openai import OpenAI
        kwargs = {"api_key": _cfg.OPENAI_API_KEY, "timeout": 120.0}
        if _cfg.OPENAI_BASE_URL:
            kwargs["base_url"] = _cfg.OPENAI_BASE_URL
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=_cfg.OPENAI_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        return resp.choices[0].message.content

    elif provider == "gemini":
        import google.generativeai as genai
        from google.generativeai import types as genai_types
        genai.configure(api_key=_cfg.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=_cfg.GEMINI_MODEL,
            system_instruction=system_prompt,
        )
        resp = model.generate_content(
            user_prompt,
            generation_config=genai_types.GenerationConfig(max_output_tokens=_MAX_TOKENS),
            request_options={"timeout": 120},
        )
        return resp.text

    else:
        raise ValueError(f"不支持的 AI_PROVIDER: {provider}，请设置为 claude / openai / gemini")
