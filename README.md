# 8848 股票策略分析器

基于"8848"交易体系的 A 股分析 Web 应用。支持实时行情分析、AI 辅助决策、批量自选股管理、持仓管理、技术指标和历史数据回溯。

## 功能

- **8848 压力/支撑线**：基于日内均价计算上压线 (`avg / 0.98848`) 和下支线 (`avg * 0.98848`)
- **策略信号**：持仓模式（动态止盈）+ 观察模式（Fibonacci 38.2% / 61.8% / 78.6% 回撤位）
- **AI 分析**：流式输出，支持 Claude / OpenAI（含 DeepSeek）/ Gemini，可发起多轮追问
- **Tushare 工具调用**：AI 可自主调用13个工具（资金流向、龙虎榜、融资融券、技术指标、筹码分布等）获取数据后再分析
- **批量分析**：一键分析全部自选股，结果保存30分钟可随时查看
- **自选股管理**：添加/删除股票，支持行业 tag 标注和按行业过滤
- **持仓管理**：记录成本价、阶段高低点，自动追踪最高价以辅助止盈
- **历史数据**：60日 OHLC + 技术指标（MACD、BOLL、RSI、移动筹码、揉搓线）
- **交易日志**：记录每笔操作并可事后复盘

## 快速开始

```bash
cp .env.example .env   # 填入 token
uv sync                # 安装依赖
./start.sh             # 后台启动（端口 8848）
```

或前台运行：

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 8848
```

打开浏览器访问：[http://localhost:8848](http://localhost:8848)

停止服务：

```bash
./stop.sh
```

## 环境变量（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TUSHARE_TOKEN` | — | Tushare Pro token，历史数据必填 |
| `COMMON_STOCK_CODES` | — | 自选股代码，逗号分隔，如 `600519,000001` |
| `AI_PROVIDER` | `claude` | `claude` / `openai` / `gemini` |
| `CLAUDE_API_KEY` / `CLAUDE_MODEL` / `CLAUDE_BASE_URL` | — | Claude 配置 |
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | — | OpenAI / DeepSeek / Ollama 配置 |
| `OPENAI_MAX_TOKENS` | `16384` | OpenAI 兼容接口的 max_tokens |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — | Gemini 配置 |

## 历史数据拉取

```bash
uv run python fetch_history.py             # 最近 60 个交易日
uv run python fetch_history.py --backfill  # 最近 90 日（首次初始化）
uv run python fetch_history.py --codes 600519,588170  # 指定股票/ETF
```

ETF 自动识别（沪市5开头、深市1开头）并走 `fund_daily` 接口。脚本同时拉取三大指数（上证、深证、创业板）用于 AI 市场参照。

收盘后自动拉取（crontab）：

```
35 15 * * 1-5 cd /path/to/MeanDeviation-Web && uv run python fetch_history.py >> fetch_history.log 2>&1
```

## 传统 Python/Pip 方式

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8848
```
