"""Render watch-plan messages with live account context.

AI plans are generated ahead of the trading day.  Price levels and intended
actions belong to the plan, while affordability does not: cash can change
after any intraday trade.  Keep the stored text for audit purposes and replace
only its stale cash/lot wording when it is shown or triggered.
"""
import re


_BUY_WORDS = ("买入", "建仓", "补仓", "加仓")
_CANCEL_WORDS = ("放弃买入", "暂停买入", "停止买入", "停止补仓", "不再买入", "不执行买入")


def is_buy_action(message: str) -> bool:
    text = message or ""
    return any(word in text for word in _BUY_WORDS) and not any(word in text for word in _CANCEL_WORDS)


def _remove_stale_affordability(message: str) -> str:
    """Remove AI-authored cash/lot clauses without altering the planned action."""
    text = (message or "").strip()
    patterns = (
        r"(?:当前)?(?:账户)?可用现金[^，。；]*",
        r"(?:当前)?资金(?:不足|不够)[^，。；]*",
        r"不足\s*(?:买|购买)?\s*一手[^，。；]*",
        r"最多(?:可)?(?:买入|购买|补仓|加仓|买)?\s*\d+\s*手[^，。；]*",
        r"仅(?:能|可)\s*(?:买入|购买|补仓|加仓|买)?\s*\d+\s*手[^，。；]*",
        r"只允许观察(?:，?不(?:建议|执行)?(?:实际)?买入)?",
    )
    for pattern in patterns:
        text = re.sub(pattern, "", text)
    text = re.sub(r"[，；]\s*[，；]", "，", text)
    text = re.sub(r"^[，。；\s]+|[，；\s]+$", "", text)
    return text


def render_live_rule_message(message: str, available_cash: float, price: float) -> str:
    """Append affordability calculated from current cash and the relevant price."""
    original = (message or "").strip()
    if not is_buy_action(original) or price <= 0:
        return original

    base = _remove_stale_affordability(original)
    cash = max(0.0, float(available_cash or 0))
    lots = int(cash // (float(price) * 100))
    if lots <= 0:
        live = f"实时可用现金 ¥{cash:,.2f}，按当前参考价不足1手，仅观察、不执行买入"
    else:
        live = (f"实时可用现金 ¥{cash:,.2f}，按当前参考价最多可买{lots}手"
                "（全账户共享，实际下单须预留手续费）")
    return f"{base}；{live}" if base else live


def render_live_plan_messages(plans: list[dict], available_cash: float) -> list[dict]:
    """Decorate loaded plans in place for template display."""
    for plan in plans:
        for rule in plan.get("rules", []):
            rule["message"] = render_live_rule_message(
                str(rule.get("message") or ""), available_cash, float(rule.get("price") or 0),
            )
    return plans
