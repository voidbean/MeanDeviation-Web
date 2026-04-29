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
- **SQLite persistence** (`stock_cache.db`): four tables managed inline:
  - `stock_name_cache` — stock code → name, two-layer cache (in-memory dict + SQLite)
  - `daily_records` — OHLC + avg_price per (date, code), written on every query and by `fetch_history.py`
  - `portfolio` — per-stock cost price, stage high/low, historical max price for dynamic stop-profit
  - `query_history` — last 50 queries, shown in the UI sidebar
- **8848 formula**: `upper_line = avg_price / 0.98848`, `lower_line = avg_price * 0.98848`, where `avg_price = amount / volume` from real-time quote
- **Strategy signals** (`calculate_strategy()`): holding mode uses dynamic stop-profit based on `max_price`; watching mode uses Fibonacci levels (38.2%, 61.8%, 78.6%) derived from user-set stage high/low
- **AI analysis** (`/ai_analyze`): loads all `skills/*.md` files as system prompt, builds a structured user prompt with current quote + 60-day history, then calls the selected LLM

### `fetch_history.py`
Standalone script (not imported by `app.py`) that pulls daily OHLC from Tushare Pro API and upserts into `daily_records`. Run manually or via cron. Requires `TUSHARE_TOKEN`.

### `templates/index.html`
Single Jinja2 template rendering the entire UI. All pages are server-side rendered — no JS framework. Form POSTs trigger page reloads.

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
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — | Gemini config |

## Key Design Decisions

- **Schema migrations** are handled inline in `init_db()` using `try/except` around `ALTER TABLE` — SQLite doesn't support `IF NOT EXISTS` for columns.
- **`COMMON_STOCKS` hot-reload**: `POST /update_common_stocks` writes back to `.env` and calls `load_dotenv(override=True)` to update the global without restarting.
- **`max_price` auto-update**: on every query while `cost_price > 0`, if today's high exceeds the stored `max_price`, it is saved immediately — no manual intervention needed.
- **Stock code normalization**: both `app.py` and `fetch_history.py` contain a `to_ts_code()` helper that converts `600519` / `sh600519` / `600519.SH` to Tushare Pro format. Keep them in sync if modifying.
- **AI timeout**: all LLM calls use a 120-second read timeout and `max_tokens=4096` to avoid truncated analysis reports.
