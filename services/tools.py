"""
services/tools.py — 兼容 shim，从新模块 re-export 所有公共符号。
保留此文件只是为了不破坏任何直接从这里导入的旧代码。
"""
# indicators
from services.indicators import (
    _today_str,
    _is_trading_time,
    _save_intraday_snapshot,
    _fetch_and_save_intraday_snapshots,
    _intraday_bg_loop,
    _get_intraday_points,
    _build_intraday_candles,
    analyze_rousu_lines,
    analyze_rousu_lines_intraday,
    _get_daily_records_for_rousu,
    _calc_macd,
    _get_benchmark_index,
    _calc_yidong,
    _calc_boll,
)

# tushare_tools
from services.tushare_tools import (
    MAX_TOOL_ROUNDS,
    TOOL_DEFINITIONS,
    SECTOR_INDEX_CODES,
    KEY_ETF_CODES,
    execute_tool,
    _build_claude_tools,
    _build_openai_tools,
    _build_gemini_tools,
    _tool_get_intraday_lines,
    _tool_get_index_intraday,
    _tool_get_moneyflow,
    _tool_get_top_list,
    _tool_get_daily_basic,
    _tool_get_technical_indicators,
    _tool_get_margin_data,
    _tool_get_sector_flow,
    _tool_get_futures_positions,
    _tool_get_disclosure_calendar,
    _tool_get_share_reduction,
    _tool_get_etf_flow,
    _tool_get_chip_distribution,
    _tool_get_technical_factors,
)

# ai
from services.ai import (
    call_ai_model,
    call_ai_model_with_tools,
    call_ai_model_streaming,
    _save_ai_conversation,
    _load_ai_conversation,
)
