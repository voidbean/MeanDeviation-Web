# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**8848 股票策略分析器** — A FastAPI web app for A-share (Chinese stock market) analysis based on the "8848" trading strategy. The app fetches real-time quotes via Tushare, calculates pressure/support levels, applies Fibonacci retracement signals, and optionally calls an LLM (Claude/OpenAI/Gemini) for AI-driven trading advice.

The port number 8848 is intentional — it matches the strategy name.

## Commands

### Setup
```bash
cp .env.example .env   # then fill in tokens
uv sync                # install dependencies
```

### Run
```bash
# Foreground
uv run uvicorn app:app --host 0.0.0.0 --port 8848

# Background (via script)
./start.sh
./stop.sh
```

### Fetch historical data (requires TUSHARE_TOKEN)
```bash
uv run python fetch_history.py             # last 60 trading days
uv run python fetch_history.py --backfill  # last 90 days (first-time init)
```

### Crontab for daily data fetch (after market close)
```
35 15 * * 1-5 cd /path/to/MeanDeviation-Web && uv run python fetch_history.py >> fetch_history.log 2>&1
```

## Architecture

### Single-file backend: `app.py`
All application logic lives in `app.py` — no separate modules. Key sections:

- **AI provider config** (top of file): reads `AI_PROVIDER` env var to select Claude / OpenAI / Gemini. `call_ai_model()` dispatches to the correct SDK.
- **SQLite persistence** (`stock_cache.db`): five tables managed inline:
  - `stock_name_cache` — stock code → name, two-layer cache (in-memory dict + SQLite)
  - `daily_records` — OHLC + avg_price per (date, code), written on every query and by `fetch_history.py`
  - `portfolio` — per-stock cost price, stage high/low, historical max price for dynamic stop-profit
  - `query_history` — last 50 queries, shown in the UI sidebar
  - `ai_conversations` — multi-turn chat history keyed by `session_id`; auto-purged after 2 hours
- **8848 formula**: `upper_line = avg_price / 0.98848`, `lower_line = avg_price * 0.98848`, where `avg_price = amount / volume` from real-time quote
- **Strategy signals** (`calculate_strategy()`): holding mode uses dynamic stop-profit based on `max_price`; watching mode uses Fibonacci levels (38.2%, 61.8%, 78.6%) derived from user-set stage high/low
- **AI analysis**: three endpoints:
  - `/ai_analyze` (POST) — legacy synchronous path, still works, redirects to `/?result_id=`
  - `/ai_stream` (GET, SSE) — streaming analysis; frontend connects via `EventSource`, receives `progress` / `token` / `done` / `error` events. On completion, saves conversation to `ai_conversations` and returns `session_id`.
  - `/ai_chat` (POST, SSE) — follow-up chat; loads history by `session_id`, appends user message, streams reply, saves updated history.
- **`call_ai_model_streaming()`**: sync generator that yields `(event_type, data)` tuples. Tool-call rounds emit `progress` events; final text is chunked into `token` events. Runs in a background thread via `queue.Queue` so the async SSE generator can poll without blocking the event loop. A `: heartbeat` SSE comment is sent every 15 s to prevent browser timeout during long AI calls.
- **Tool use layer**: `TOOL_DEFINITIONS` → `_build_claude/openai/gemini_tools()` → `execute_tool()` → `_tool_*()` functions. The LLM can call up to `MAX_TOOL_ROUNDS=5` rounds of tools before the loop exits. `call_ai_model()` (no tools) is kept as a fallback.

### `fetch_history.py`
Standalone script (not imported by `app.py`) that pulls daily OHLC from Tushare Pro API and upserts into `daily_records`. Run manually or via cron. Requires `TUSHARE_TOKEN`.

### `templates/index.html`
Single Jinja2 template rendering the entire UI. Most pages are server-side rendered with Form POSTs triggering page reloads. Exception: the AI analysis button calls `startAiStream()` (global JS function) which opens an `EventSource` to `/ai_stream` and streams results in-place without a page reload. After analysis completes a chat input appears for follow-up questions via `/ai_chat`.

**Important**: `startAiStream()` and `sendAiChat()` must be defined in the **global JS scope** (outside `DOMContentLoaded`), not inside it — they are referenced by `onclick` attributes in the HTML.

### `skills/` directory
Markdown files (`01_*.md` through `11_*.md`) containing the "阿狼投资体系" trading methodology. Loaded at AI analysis time by `load_skills()` and injected as the LLM system prompt. `agent_prompt_template.md` contains the recommended agent invocation pattern.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `TUSHARE_TOKEN` | — | Tushare Pro API token (required for history fetch; optional for real-time quotes) |
| `COMMON_STOCK_CODES` | — | Comma-separated codes for batch analysis, e.g. `600519,000001` |
| `AI_PROVIDER` | `claude` | `claude` / `openai` / `gemini` |
| `CLAUDE_API_KEY` / `CLAUDE_MODEL` / `CLAUDE_BASE_URL` | — | Claude config; `CLAUDE_BASE_URL` enables proxy/compatible endpoints |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | — | OpenAI config; `OPENAI_BASE_URL` supports DeepSeek, local Ollama, etc. |
| `OPENAI_MAX_TOKENS` | `16384` | OpenAI/兼容接口的 max_tokens；DeepSeek-R1 等推理模型会先消耗 reasoning token，建议保持默认或更高 |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — | Gemini config |

### Tool Use Architecture

`/ai_analyze` uses an agentic loop: the LLM receives the base prompt and can call Tushare tools to fetch additional data before producing its final analysis.

**Available tools** (defined in `TOOL_DEFINITIONS` in `app.py`):

| Tool | Tushare API | Returns | 阿狼体系 |
|---|---|---|---|
| `get_intraday_lines` | `stk_mins` 1min | White line (price) + Yellow line (cumulative avg), sampled every 5min | Skill 06 日内做T |
| `get_moneyflow` | `moneyflow` | Last 5 days: super-large/large/net flow amounts | Skill 05/08 量价/资金 |
| `get_top_list` | `top_list` | Dragon-Tiger list entries in last 10 trading days | Skill 08 市场参与者 |
| `get_daily_basic` | `daily_basic` | Latest PE/PB/turnover rate/market cap | Skill 11 股票类型 |

**Provider-specific tool formats** (all generated from the same `TOOL_DEFINITIONS`):
- Claude: `input_schema` format, loop exits on `stop_reason == "end_turn"`
- OpenAI: `{"type": "function"}` format, loop exits on `finish_reason == "stop"`
- Gemini: `FunctionDeclaration` + `start_chat()` pattern, loop exits when no `function_call` parts

**To add a new tool**: add an entry to `TOOL_DEFINITIONS`, implement `_tool_<name>()`, add a branch in `execute_tool()`. The three provider format builders pick it up automatically.

**`stk_mins` rate limit**: Tushare limits `stk_mins` to 2 calls/day on basic accounts. The tool returns an `{"error": "..."}` JSON on failure — the LLM will proceed without intraday data rather than crashing.

## Key Design Decisions

- **Schema migrations** are handled inline in `init_db()` using `try/except` around `ALTER TABLE` — SQLite doesn't support `IF NOT EXISTS` for columns.
- **`COMMON_STOCKS` hot-reload**: `POST /update_common_stocks` writes back to `.env` and calls `load_dotenv(override=True)` to update the global without restarting.
- **`max_price` auto-update**: on every query while `cost_price > 0`, if today's high exceeds the stored `max_price`, it is saved immediately — no manual intervention needed.
- **Stock code normalization**: both `app.py` and `fetch_history.py` contain a `to_ts_code()` helper that converts `600519` / `sh600519` / `600519.SH` to Tushare Pro format. Keep them in sync if modifying.
- **AI timeout**: `call_ai_model_with_tools()` uses 180-second timeout (vs 120s for the no-tool version) to accommodate multi-round tool loops. `MAX_TOOL_ROUNDS=5` prevents infinite loops.
- **SSE streaming pattern**: `call_ai_model_streaming()` is a sync generator run in a `threading.Thread`; events are passed through a `queue.Queue` to the async FastAPI generator. This avoids blocking the uvicorn event loop during long AI calls. Both `/ai_stream` and `/ai_chat` use this pattern.
- **`daily_records.amount` column**: stores trading amount in 千元 (same unit as `pro.daily`). Index data from `pro.index_daily` also uses 千元. Display conversion: `amount / 100000` = 亿元.
