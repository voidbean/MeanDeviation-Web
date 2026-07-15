import os
import logging
from pathlib import Path

import tushare as ts
from dotenv import load_dotenv

# 项目根目录（core/ 的上一级）
_ROOT = Path(__file__).parent.parent

# 必须指定根目录 .env，避免 cwd 变化时 find_dotenv 找不到文件
load_dotenv(_ROOT / ".env", override=True)

logging.basicConfig(
    filename=str(_ROOT / "app.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── AI 分析配置 ──────────────────────────────────────────────
AI_PROVIDER       = os.getenv("AI_PROVIDER", "claude").lower()
CLAUDE_API_KEY    = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
CLAUDE_BASE_URL   = os.getenv("CLAUDE_BASE_URL", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_BASE_URL   = os.getenv("OPENAI_BASE_URL", "")
OPENAI_PROXY      = os.getenv("OPENAI_PROXY", "")  # 仅 OpenAI 兼容请求走此代理（如 OpenRouter 走日本出口）
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "16384"))
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

SKILLS_DIR = _ROOT / "skills"

# Tushare
TS_TOKEN = os.getenv("TUSHARE_TOKEN", "")
pro = None
if TS_TOKEN:
    ts.set_token(TS_TOKEN)
    pro = ts.pro_api()

# SQLite
STOCK_NAME_CACHE: dict = {}
DB_PATH = str(_ROOT / "stock_cache.db")


def load_common_stocks() -> list:
    raw = os.getenv("COMMON_STOCK_CODES", "") or ""
    raw = raw.replace("，", ",")
    codes = [c.strip() for c in raw.split(",") if c.strip()]
    return [{"code": code} for code in codes]


COMMON_STOCKS: list = load_common_stocks()
