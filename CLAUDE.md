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

The codebase has been refactored from a single `app.py` into a modular structure. The entry point is still `app.py` but it is now a thin launcher.

### Entry point: `app.py`
Initializes FastAPI, runs `init_db()` on startup, starts the background intraday snapshot thread, then registers all route modules:
```
routes.main.register(app, templates)
routes.sector.register(app, templates)
routes.review.register(app, templates)
```

### `core/` — shared config and persistence
- **`core/config.py`**: loads `.env` (root-level only), initializes Tushare `pro` API, exposes `AI_PROVIDER`, `DB_PATH`, `SKILLS_DIR`, `COMMON_STOCKS`, etc. `load_dotenv()` is called here — **all `.env` writes must target the root `.env`, not any subdirectory `.env`**.
- **`core/db.py`**: all SQLite access. Tables: `stock_name_cache`, `daily_records`, `portfolio`, `query_history`, `temp_results`, `intraday_snapshots`, `trade_log`, `ai_conversations`, `stock_tags`. Schema migrations use `try/except` around `ALTER TABLE`.
- **`core/strategy.py`**: `calculate_8848()`, `calculate_strategy()`, `calculate_8848_history()`, `to_ts_code()`, `load_skills()`, `build_ai_prompt()`, `get_stock_volume_chart_data()`.

### `services/` — business logic
- **`services/tushare_tools.py`**: `TOOL_DEFINITIONS` list, all `_tool_*()` functions (13 tools), `execute_tool()`, provider-specific tool format builders (`_build_claude/openai/gemini_tools()`).
- **`services/ai.py`**: `call_ai_model_with_tools()`, `call_ai_model_streaming()`, `call_ai_model()`, `_save_ai_conversation()`, `_load_ai_conversation()`.
- **`services/indicators.py`**: 揉搓线分析 (`analyze_rousu_lines`, `analyze_rousu_lines_intraday`), intraday snapshot fetch/background loop, volatility stats, MACD, BOLL, 移动筹码 etc.
- **`services/tools.py`**: re-exports everything from `tushare_tools.py` and `ai.py` for backwards-compatible imports.

### `routes/` — FastAPI route registration
Each module exports `register(app, templates)`. Do NOT import routes at module level — they are registered in `app.py`.
- **`routes/main.py`**: homepage, stock analysis, batch analysis, portfolio, AI stream/chat, common stocks management, AI provider config.
- **`routes/sector.py`**: sector analysis endpoints.
- **`routes/review.py`**: trade log review endpoints.

**Critical**: `routes/main.py` manages `COMMON_STOCKS` as a module-level global and writes `.env` changes via `_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")` — this resolves to the root `.env`, which is the same file `core/config.py` reads at startup. Never use `os.path.dirname(__file__)` alone as the `.env` path inside `routes/`.

### `templates/index.html`
Single Jinja2 template for the entire UI. Server-side rendered with Form POSTs. Exceptions:
- AI analysis uses `startAiStream()` → `EventSource` to `/ai_stream` (SSE, no page reload)
- Follow-up chat uses `sendAiChat()` → `/ai_chat` (SSE)
- Tag edits use `openTagModal()` / `saveTagModal()` → POST `/update_stock_tag`

**Important**: `startAiStream()`, `sendAiChat()`, `openTagModal()`, `saveTagModal()`, `filterByTag()`, `addStock()`, `removeStock()` must all be in **global JS scope** (outside `DOMContentLoaded`), as they are called from `onclick` attributes.

### `fetch_history.py`
Standalone script (not imported by `app.py`) — pulls daily OHLC from Tushare Pro and upserts into `daily_records`. Run manually or via cron.

### `skills/` directory
Markdown files (`01_*.md` through `11_*.md`) — "阿狼投资体系" methodology. Loaded by `load_skills()` and injected as LLM system prompt.

## SQLite Tables (`stock_cache.db`)

| Table | Purpose |
|---|---|
| `stock_name_cache` | code → name, two-layer (in-memory dict + SQLite) |
| `daily_records` | OHLC + avg_price + amount + open per (date, code) |
| `portfolio` | cost_price, stage_high/low, max_price per code |
| `query_history` | last 50 queries shown in UI sidebar |
| `temp_results` | short-lived query results keyed by UUID, auto-purged after 30 min |
| `intraday_snapshots` | per-minute price snapshots, used for 揉搓线 and volatility |
| `trade_log` | manual trade records with review/emotion fields |
| `ai_conversations` | multi-turn chat history keyed by session_id, auto-purged after 2 hours |
| `stock_tags` | code → industry tag (e.g. "白酒", "银行"), user-editable |

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `TUSHARE_TOKEN` | — | Tushare Pro API token |
| `COMMON_STOCK_CODES` | — | Comma-separated codes for batch analysis |
| `AI_PROVIDER` | `claude` | `claude` / `openai` / `gemini` |
| `CLAUDE_API_KEY` / `CLAUDE_MODEL` / `CLAUDE_BASE_URL` | — | Claude config |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | — | OpenAI / DeepSeek / Ollama config |
| `OPENAI_MAX_TOKENS` | `16384` | max_tokens for OpenAI-compatible APIs |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — | Gemini config |

## Tool Use Architecture

`/ai_stream` and `/ai_analyze` run an agentic loop where the LLM can call Tushare tools before producing final analysis.

**Available tools** (defined in `TOOL_DEFINITIONS` in `services/tushare_tools.py`):

| Tool | Tushare API | Returns |
|---|---|---|
| `get_intraday_lines` | `stk_mins` 1min | White/Yellow line (price + cumulative avg) |
| `get_moneyflow` | `moneyflow` | Last 5 days super-large/large/net flows |
| `get_top_list` | `top_list` | Dragon-Tiger entries in last 10 days |
| `get_daily_basic` | `daily_basic` | PE/PB/turnover/market cap |
| `get_technical_indicators` | local DB | MACD, BOLL, 移动筹码, RSI, 阿狼移动 |
| `get_margin_data` | `margin_detail` | Latest margin trading data |
| `get_sector_flow` | `moneyflow_ind_dc` | Sector money flow |
| `get_futures_positions` | `fut_holding` | Top futures positions |
| `get_disclosure_calendar` | `disclosure_date` | Earnings disclosure date |
| `get_share_reduction` | `share_float` | Shareholder reduction plans |
| `get_etf_flow` | `fund_flow_ind` | ETF fund flow |
| `get_chip_distribution` | local DB | Chip distribution (移动筹码) |
| `get_technical_factors` | local DB | Combined technical factors |

**To add a new tool**: add entry to `TOOL_DEFINITIONS`, implement `_tool_<name>()` in `tushare_tools.py`, add branch in `execute_tool()`. The three provider format builders pick it up automatically.

**Provider-specific formats**:
- Claude: `input_schema`, loop exits on `stop_reason == "end_turn"`
- OpenAI: `{"type": "function"}`, loop exits on `finish_reason == "stop"`
- Gemini: `FunctionDeclaration` + `start_chat()`, loop exits when no `function_call` parts

## Key Design Decisions

- **`.env` path**: always the root-level `.env`. `core/config.py` loads it at import time. `routes/main.py` writes to it via `_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")`. Never write to `routes/.env` or any subdirectory `.env`.
- **`COMMON_STOCKS` hot-reload**: `POST /update_common_stocks` writes root `.env` then calls `load_dotenv(override=True)` to update in-process globals without restart.
- **Stock code normalization**: `to_ts_code()` in `core/strategy.py` converts `600519` / `sh600519` / `600519.SH` → Tushare Pro format. `fetch_history.py` has its own copy — keep in sync.
- **SSE streaming**: `call_ai_model_streaming()` is a sync generator run in a `threading.Thread`; events pass through `queue.Queue` to the async FastAPI generator. Both `/ai_stream` and `/ai_chat` use this pattern. Heartbeat comment sent every 15s.
- **AI timeout**: `call_ai_model_with_tools()` uses 180s; `MAX_TOOL_ROUNDS=5` prevents infinite loops.
- **Schema migrations**: inline in `init_db()` via `try/except` around `ALTER TABLE` (SQLite has no `ADD COLUMN IF NOT EXISTS`).
- **`daily_records.amount`**: stored in 千元. Display as 亿元: `amount / 100000`.
- **Stock tags**: stored in `stock_tags` SQLite table (not in `.env`). `get_distinct_tags()` returns all in-use tags for datalist suggestions. Tag colors assigned by JS `_tagColor()` — fixed map for common industries, hash-based fallback for custom tags.
