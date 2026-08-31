"""Explicit operation intent, separate from market-trigger state."""
ACTIONS = {"entry", "add", "reduce", "exit", "cancel", "observe"}


def infer_action(message):
    text = message or ""
    if any(w in text for w in ("放弃买入", "暂停买入", "停止买入", "停止补仓", "不再买入", "不执行买入")):
        return "cancel"
    if any(w in text for w in ("补仓", "加仓")):
        return "add"
    if any(w in text for w in ("买入", "建仓")):
        return "entry"
    if any(w in text for w in ("清仓", "止损", "离场", "全部卖出")):
        return "exit"
    if any(w in text for w in ("卖出", "减仓", "止盈")):
        return "reduce"
    return "observe"
