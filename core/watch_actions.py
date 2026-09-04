"""Explicit operation intent, separate from market-trigger state."""
ACTIONS = {"entry", "add", "reduce", "exit", "cancel", "observe"}

# Observation-only wording can still contain a buy keyword.  It must be
# checked before positive keywords (for example, “不主动补仓” contains “补仓”).
OBSERVE_ONLY_PHRASES = (
    "仅观察", "只观察", "不主动买入", "不主动补仓", "不建议买入", "不建议补仓",
)


def infer_action(message):
    text = message or ""
    if any(w in text for w in ("放弃买入", "暂停买入", "停止买入", "停止补仓", "不再买入", "不执行买入")):
        return "cancel"
    if any(w in text for w in OBSERVE_ONLY_PHRASES):
        return "observe"
    if any(w in text for w in ("补仓", "加仓")):
        return "add"
    if any(w in text for w in ("买入", "建仓")):
        return "entry"
    if any(w in text for w in ("清仓", "止损", "离场", "全部卖出")):
        return "exit"
    if any(w in text for w in ("卖出", "减仓", "止盈")):
        return "reduce"
    return "observe"
