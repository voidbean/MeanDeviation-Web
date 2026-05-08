# 8848 Stock Strategy Analyzer

This is a simple Python web application based on the "8848" trading strategy theory. Users can input a stock code through a clean web interface, and the backend will fetch real-time data via Tushare to calculate the corresponding resistance level (red line) and support level (green line).

## Strategy Principle

- **High Level (Resistance Line)**: `Intraday Average Price / 0.98848`
- **Low Level (Support Line)**: `Intraday Average Price * 0.98848`

When the stock price exceeds the resistance line, it may be considered a high point. When the price falls below the support line, it may be considered a low point. This provides a simple reference for intraday trading.

## Configuration

### Tushare Token (Optional)

Although a token is not always required for basic real-time quotes, it is recommended to configure a Tushare Token to ensure the stability of the data interface.

First, copy the `.env.example` file to `.env`:

```bash
cp .env.example .env
```

Then, edit the `.env` file and replace `your_tushare_token_here` with your own Tushare Token.

```
# .env
TUSHARE_TOKEN=your_tushare_token_here
```

---

## Method 1: Running with `uv` (Recommended)

This project is configured to use `uv` by default.

### 1. Initialization and Dependency Installation

```bash
uv sync
```

### 2. Starting the Application

Use the provided script to start the service (runs in the background automatically):

```bash
chmod +x start.sh stop.sh
./start.sh
```

Alternatively, run it in the foreground:

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 8848
```

---

## Method 2: Running with traditional Python/Pip

If you do not use `uv`, you can install dependencies with `pip` and run the application directly with `python`.

### 1. Install Dependencies

Ensure your Python version is >= 3.10.

```bash
pip install -r requirements.txt
```

### 2. Start the Application

Start directly using `uvicorn`:

```bash
uvicorn app:app --host 0.0.0.0 --port 8848
```

Or, if you want to run it in the background (similar to the effect of `start.sh`):

```bash
nohup uvicorn app:app --host 0.0.0.0 --port 8848 > app.log 2>&1 &
echo "Application started in the background"
```

---

## Accessing and Stopping

### Accessing the Application

Open your browser and navigate to: [http://localhost:8848](http://localhost:8848)

### Stopping the Application

If you started the application using `start.sh` or in the background, you can use the stop script:

```bash
./stop.sh
```

---

## Historical Data Fetching (`fetch_history.py`)

`fetch_history.py` pulls daily OHLC data from Tushare Pro and stores it in the local SQLite database (`stock_cache.db`). This data powers the 60-day statistics and AI analysis features.

> **Requires** `TUSHARE_TOKEN` to be set in `.env`.

### Usage

```bash
# Normal mode: fetch last 60 trading days for all codes in COMMON_STOCK_CODES
uv run python fetch_history.py

# Backfill mode: fetch last 90 trading days (use on first deployment)
uv run python fetch_history.py --backfill

# Fetch specific stock/ETF codes only (ignores COMMON_STOCK_CODES)
uv run python fetch_history.py --codes 600519,588170,159206

# Combine: specific codes + backfill
uv run python fetch_history.py --codes 600519,588170 --backfill
```

### ETF Support

ETFs are automatically detected and fetched via the `fund_daily` API instead of `daily`:

- **Shanghai ETFs** (code starts with `5`, e.g. `588170`, `510300`) → `fund_daily`
- **Shenzhen ETFs** (code starts with `1`, e.g. `159206`, `159915`) → `fund_daily`
- **Regular stocks** → `daily`

### Crontab (auto-run after market close)

```
35 15 * * 1-5 cd /path/to/MeanDeviation-Web && uv run python fetch_history.py >> fetch_history.log 2>&1
```

### Index Data

The script also automatically fetches the three major market indices (上证指数, 深证成指, 创业板指) on every run, which are used as market trend indicators in AI analysis.
