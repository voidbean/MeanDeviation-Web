# 8848 股票策略分析器

这是一个基于 "8848" 交易策略理论的简单 Python Web 应用。用户可以通过一个简洁的网页界面输入股票代码，后端会通过 Tushare 实时获取数据，并计算出对应的压力位（红线）和支撑位（绿线）。

## 策略原理

- **高位判断（压力线）**: `当日均价 / 0.98848`
- **低位判断（支撑线）**: `当日均价 * 0.98848`

当股价超过压力线时，可能被视为高位；当股价低于支撑线时，可能被视为低位。这为日内交易提供了一个简单的参考。

## 配置

### Tushare Token (可选)

虽然基础的实时行情获取通常不需要 Token，但为了保证数据接口的稳定性，建议配置 Tushare 的 Token。

首先，复制 `.env.example` 文件为 `.env`：

```bash
cp .env.example .env
```

然后，编辑 `.env` 文件，将 `your_tushare_token_here` 替换为您自己的 Tushare Token。

```
# .env
TUSHARE_TOKEN=your_tushare_token_here
```

---

## 方式一：使用 uv 运行 (推荐)

本项目默认配置为使用 `uv` 管理。

### 1. 初始化与依赖安装

```bash
uv sync
```

### 2. 启动应用

使用提供的脚本启动服务（自动后台运行）：

```bash
chmod +x start.sh stop.sh
./start.sh
```

或者直接前台运行：

```bash
uv run uvicorn app:app --host 0.0.0.0 --port 8848
```

---

## 方式二：使用传统 Python/Pip 运行

如果您不使用 `uv`，可以通过 `pip` 安装依赖并使用 `python` 直接运行。

### 1. 安装依赖

确保您的 Python 版本 >= 3.10。

```bash
pip install -r requirements.txt
```

### 2. 启动应用

直接使用 `uvicorn` 启动：

```bash
uvicorn app:app --host 0.0.0.0 --port 8848
```

或者，如果您希望在后台运行（类似 `start.sh` 的效果）：

```bash
nohup uvicorn app:app --host 0.0.0.0 --port 8848 > app.log 2>&1 &
echo "应用已在后台启动"
```

---

## 访问与停止

### 访问应用

打开浏览器访问：[http://localhost:8848](http://localhost:8848)

### 停止应用

如果您使用 `start.sh` 或后台方式启动，可以使用停止脚本：

```bash
./stop.sh
```
