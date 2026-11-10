"""Pure portfolio valuation shared by the web UI and read-only MCP."""


def build_portfolio_overview(holdings, available_cash, quotes, prev_closes):
    results = []
    for holding in holdings:
        code = holding["code"]
        cost = holding["cost"]
        name = holding["name"]
        quantity = holding["quantity"]
        short_code = code.split(".")[0] if "." in code else code
        quote = quotes.get(short_code, {})
        price = quote.get("price")
        open_ = quote.get("open")
        if not name:
            name = quote.get("name", "")
        prev_close = quote.get("prev_close") or prev_closes.get(code) or prev_closes.get(short_code)

        if price is not None and cost > 0:
            total_pnl_pct = round((price - cost) / cost * 100, 2)
            total_pnl_abs = round((price - cost) * quantity, 2) if quantity > 0 else None
        else:
            total_pnl_pct = None
            total_pnl_abs = None

        if price is not None and prev_close is not None and prev_close > 0:
            today_pnl_pct = round((price - prev_close) / prev_close * 100, 2)
            today_pnl_abs = round((price - prev_close) * quantity, 2) if quantity > 0 else None
        else:
            today_pnl_pct = None
            today_pnl_abs = None

        market_value = round(price * quantity, 2) if price is not None and quantity > 0 else None

        results.append({
            "code": short_code,
            "name": name or short_code,
            "cost": cost,
            "quantity": quantity,
            "current_price": price,
            "market_value": market_value,
            "total_pnl_pct": total_pnl_pct,
            "total_pnl_abs": total_pnl_abs,
            "today_pnl_pct": today_pnl_pct,
            "today_pnl_abs": today_pnl_abs,
            "quote_source": quote.get("source"),
        })

    valid = [r for r in results if r["total_pnl_pct"] is not None]
    total_avg = round(sum(r["total_pnl_pct"] for r in valid) / len(valid), 2) if valid else None
    today_valid = [r for r in results if r["today_pnl_pct"] is not None]
    today_avg = round(sum(r["today_pnl_pct"] for r in today_valid) / len(today_valid), 2) if today_valid else None

    abs_valid = [r for r in results if r["total_pnl_abs"] is not None]
    total_abs = round(sum(r["total_pnl_abs"] for r in abs_valid), 2) if abs_valid else None
    today_abs_valid = [r for r in results if r["today_pnl_abs"] is not None]
    today_abs = round(sum(r["today_pnl_abs"] for r in today_abs_valid), 2) if today_abs_valid else None
    total_mv = round(sum(r["market_value"] for r in results if r["market_value"] is not None), 2)

    payload = {
        "stocks": results,
        "total_pnl_pct": total_avg,
        "today_avg_pct": today_avg,
        "total_pnl_abs": total_abs,
        "today_pnl_abs": today_abs,
        "total_market_value": total_mv if total_mv else None,
        "available_cash": available_cash,
        "account_equity": round(available_cash + total_mv, 2),
    }
    return payload
