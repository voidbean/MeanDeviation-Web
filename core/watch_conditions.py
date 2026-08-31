"""Versioned, strictly validated shadow conditions (never order gates)."""
import math


CONDITION_LABELS = {
    "volume_ratio_3m_20m": "近3分钟均量/此前20分钟均量",
    "price_vs_vwap": "当前价/当日累计均价",
    "daily_macd_hist": "已收盘日线MACD柱(12,26,9)",
}


def validate_conditions(value):
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("conditions 必须是最多3项的数组")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("条件必须是对象")
        metric, op = item.get("metric"), item.get("op")
        if metric not in CONDITION_LABELS or metric in seen or op not in {"gte", "lte"}:
            raise ValueError("不支持或重复的条件")
        number = item.get("value")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise ValueError("条件阈值必须为有限数值")
        if metric == "volume_ratio_3m_20m" and not 0.1 <= number <= 20:
            raise ValueError("量能倍数超出范围")
        if metric == "price_vs_vwap" and not 0.95 <= number <= 1.05:
            raise ValueError("均价比例超出范围")
        if metric == "daily_macd_hist" and number != 0:
            raise ValueError("MACD辅助条件仅支持与零轴比较")
        result.append({"metric": metric, "op": op, "value": float(number)})
        seen.add(metric)
    return result


def describe_conditions(conditions):
    return "；".join(f"{CONDITION_LABELS[c['metric']]} {'≥' if c['op'] == 'gte' else '≤'} {c['value']:g}"
                    for c in conditions)
