"""09:35 / 10:00 / 13:05 / 14:35 盘中计划校准。"""
import datetime as dt
import json
import sqlite3
import time

from core.config import DB_PATH, logger
from core.db import get_available_cash, get_portfolio
from core.strategy import load_skills
from services.ai import call_ai_model

CALIBRATION_SLOTS = (
    ("09:35", "open"),
    ("10:00", "morning"),
    ("13:05", "midday"),
    ("14:35", "close"),
)


def _now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _claim_run(trade_date: str, slot: str) -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status,started_at FROM watch_calibration_runs WHERE trade_date=? AND slot=?",
            (trade_date, slot),
        ).fetchone()
        if row:
            stale = False
            try:
                stale = (dt.datetime.now() - dt.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")).total_seconds() > 600
            except Exception:
                stale = True
            if row[0] == "completed" or (row[0] == "running" and not stale):
                conn.rollback()
                return False
            conn.execute(
                "UPDATE watch_calibration_runs SET status='running',started_at=?,completed_at=NULL,error='' WHERE trade_date=? AND slot=?",
                (_now_text(), trade_date, slot),
            )
        else:
            conn.execute(
                "INSERT INTO watch_calibration_runs(trade_date,slot,status,started_at) VALUES(?,?,'running',?)",
                (trade_date, slot, _now_text()),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def _finish_run(trade_date: str, slot: str, error: str = "") -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE watch_calibration_runs SET status=?,completed_at=?,error=? WHERE trade_date=? AND slot=?",
        ("failed" if error else "completed", _now_text(), error[:1000], trade_date, slot),
    )
    conn.commit()
    conn.close()


def _active_plans(conn: sqlite3.Connection, trade_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.* FROM watch_plans p JOIN stock_watchlist w ON w.code=p.code AND w.enabled=1
           WHERE p.trade_date=? AND p.status='active' ORDER BY p.code""",
        (trade_date,),
    ).fetchall()


def _rules(conn: sqlite3.Connection, plan_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id,rule_type,threshold,COALESCE(original_threshold,threshold),priority,state,
                  COALESCE(paused,0),message FROM watch_rules WHERE plan_id=? ORDER BY id""",
        (plan_id,),
    ).fetchall()
    keys = ("id", "type", "threshold", "original_threshold", "priority", "state", "paused", "message")
    return [dict(zip(keys, row)) for row in rows]


def _market_snapshot(conn: sqlite3.Connection, code: str, trade_date: str) -> dict | None:
    rows = conn.execute(
        "SELECT time,price,open,high,low,vol FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time",
        (code, trade_date),
    ).fetchall()
    if not rows:
        return None
    prices = [float(r[1]) for r in rows if r[1]]
    vols = [float(r[5] or 0) for r in rows]
    if not prices:
        return None
    return {"first_time": rows[0][0], "last_time": rows[-1][0], "open": float(rows[-1][2] or prices[0]),
            "price": prices[-1], "high": max(prices), "low": min(prices),
            "minutes": len(rows), "total_volume": round(sum(vols), 2)}


def _save_revision(conn, plan, slot, source, decision, reason, original, applied) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO watch_plan_revisions(
           plan_id,trade_date,slot,source,decision,reason,original_rules_json,applied_rules_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (plan["id"], plan["trade_date"], slot, source, decision, reason,
         json.dumps(original, ensure_ascii=False), json.dumps(applied, ensure_ascii=False), _now_text()),
    )


def run_open_calibration(trade_date: str) -> int:
    """09:35：只做确定性暂停，不调用 AI。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    count = 0
    try:
        for plan in _active_plans(conn, trade_date):
            snap = _market_snapshot(conn, plan["code"], trade_date)
            if not snap:
                continue
            original = _rules(conn, plan["id"])
            prev = conn.execute(
                "SELECT close FROM daily_records WHERE code=? AND date<? AND close>0 ORDER BY date DESC LIMIT 1",
                (plan["code"], trade_date),
            ).fetchone()
            gap_pct = ((snap["open"] - float(prev[0])) / float(prev[0]) * 100) if prev and prev[0] else 0
            applied, decision, reason = [], "continue", f"开盘缺口 {gap_pct:.2f}%"
            breakdowns = [r["threshold"] for r in original if r["type"] == "breakdown"]
            if gap_pct >= 4:
                decision, reason = "pause_opportunity", f"高开 {gap_pct:.2f}%，暂停机会规则，避免追涨"
            elif breakdowns and snap["open"] <= max(breakdowns):
                decision, reason = "pause_opportunity", "开盘直接跌破风险线，暂停机会规则"
            if decision == "pause_opportunity":
                for rule in original:
                    if rule["priority"] == "opportunity" and rule["state"] != "triggered":
                        conn.execute("UPDATE watch_rules SET paused=1,revision_reason=? WHERE id=?", (reason, rule["id"]))
                        applied.append({"rule_id": rule["id"], "action": "pause"})
            _save_revision(conn, plan, "09:35", "system", decision, reason, original, applied)
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def _parse_array(text: str) -> list:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = json.loads(text[text.find("["):text.rfind("]") + 1])
    if not isinstance(value, list):
        raise ValueError("AI 校准结果不是 JSON 数组")
    return value


def _apply_ai_decision(conn, plan, proposal: dict, slot: str, market_price: float | None = None) -> dict | None:
    original = _rules(conn, plan["id"])
    by_id = {r["id"]: r for r in original}
    decision = str(proposal.get("decision", "continue"))
    if decision not in {"continue", "pause_opportunity", "invalidate", "tighten_risk"}:
        decision = "continue"
    reason = str(proposal.get("reason", ""))[:500]
    applied = []
    if decision in {"pause_opportunity", "invalidate"}:
        for rule in original:
            should_pause = decision == "invalidate" and rule["priority"] != "risk"
            should_pause = should_pause or (decision == "pause_opportunity" and rule["priority"] == "opportunity")
            if should_pause and rule["state"] != "triggered":
                conn.execute("UPDATE watch_rules SET paused=1,revision_reason=? WHERE id=?", (reason, rule["id"]))
                applied.append({"rule_id": rule["id"], "action": "pause"})
    for change in (proposal.get("adjustments") or [])[:4]:
        try:
            rule_id = int(change.get("rule_id")); rule = by_id[rule_id]
        except (TypeError, ValueError, KeyError):
            continue
        if rule["state"] == "triggered":
            continue
        action = change.get("action")
        if action == "pause":
            conn.execute("UPDATE watch_rules SET paused=1,revision_reason=? WHERE id=?", (reason, rule_id))
            applied.append({"rule_id": rule_id, "action": "pause"})
        elif action == "update" and rule["type"] in {"breakout", "breakdown", "near"}:
            try:
                new_value = float(change.get("threshold")); old = float(rule["threshold"])
            except (TypeError, ValueError):
                continue
            max_delta = old * 0.01
            if abs(new_value - old) > max_delta:
                continue
            if (rule["priority"] == "risk" or rule["type"] == "breakdown") and new_value < old:
                continue  # 风险线只允许收紧（上移）
            conn.execute(
                "UPDATE watch_rules SET threshold=?,revision_reason=?,consecutive_hits=0 WHERE id=?",
                (new_value, reason, rule_id),
            )
            applied.append({"rule_id": rule_id, "action": "update", "from": old, "to": new_value})
    _save_revision(conn, plan, slot, "ai", decision, reason, original, applied)
    if decision == "continue" or market_price is None:
        return None
    # 校准决策和价格规则触发是两类事件。非 continue 决策也必须进入通知中心，
    # 否则页面关闭期间发生的清仓/减仓判断只能埋在审计时间线里。
    representative = next((r for r in original if r["priority"] == "risk"), original[0] if original else None)
    if not representative:
        return None
    priority = "risk" if decision in {"tighten_risk", "invalidate"} else "observe"
    now = _now_text()
    message = f"{slot} 校准：{reason or decision}"
    cur = conn.execute(
        """INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (representative["id"], plan["code"], plan["name"], "calibration", priority,
         float(market_price), message, now),
    )
    return {"id": cur.lastrowid, "code": plan["code"], "name": plan["name"],
            "event_type": "calibration", "priority": priority, "price": float(market_price),
            "message": message, "triggered_at": now, "read_at": None}


def run_ai_calibration(trade_date: str, slot: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    plans = _active_plans(conn, trade_date)
    payload, plan_by_code, market_price_by_code = [], {}, {}
    available_cash = get_available_cash()
    for plan in plans:
        snap = _market_snapshot(conn, plan["code"], trade_date)
        if not snap:
            continue
        rules = _rules(conn, plan["id"])
        portfolio = get_portfolio(plan["code"])
        quantity = int(portfolio.get("quantity") or 0)
        price = float(snap.get("price") or 0)
        payload.append({"code": plan["code"], "name": plan["name"], "bias": plan["bias"],
                        "summary": plan["summary"], "market": snap, "rules": rules,
                        "position": {"holding": quantity > 0, "quantity": quantity,
                                     "cost_price": float(portfolio.get("cost") or 0)},
                        "account": {"available_cash": available_cash,
                                    "max_buy_lots_at_current_price":
                                        int(available_cash // (price * 100)) if price > 0 else 0}})
        plan_by_code[plan["code"]] = plan
        market_price_by_code[plan["code"]] = price
    conn.close()
    if not payload:
        return 0

    skills = load_skills(subset=("05", "06", "07"))
    focus = {
        "10:00": "判断上午主方向、突破质量与量价配合",
        "13:05": "根据上午完整走势决定午后继续、降级或失效",
        "14:35": ("执行尾盘隔夜决策：未持仓判断是否保留买入机会；已持仓判断持有、补仓机会或减仓风控。"
                  "不得临时创造新买点，不得追涨。available_cash是全账户共享现金，不可对多只股票重复分配；"
                  "不足一手（100股）时必须暂停买入/补仓机会。reason必须明确写出买入、补仓、持有、减仓或放弃机会"),
    }.get(slot, "根据当前走势决定继续、降级或失效")
    system_prompt = f"""你是A股盘中计划校准器。以下仅注入量价、分时和风控方法论：\n{skills}\n
只输出JSON数组，不要Markdown。每只股票输出code、decision、reason、adjustments。
decision仅允许continue/pause_opportunity/invalidate/tighten_risk。
adjustments每项仅允许rule_id、action(pause/update)、threshold。不得修改已触发规则；
止损/跌破风险线只能上移，不能下调；关键价最多小幅修正1%；不要追着价格重写计划。
{slot}校准重点：{focus}。
"""
    raw = call_ai_model(system_prompt, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    proposals = _parse_array(raw)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    count = 0
    notification_events = []
    try:
        proposed_codes = set()
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            plan = plan_by_code.get(proposal.get("code"))
            if not plan:
                continue
            event = _apply_ai_decision(
                conn, plan, proposal, slot,
                market_price=market_price_by_code.get(plan["code"]),
            )
            if event:
                notification_events.append(event)
            proposed_codes.add(plan["code"]); count += 1
        for code, plan in plan_by_code.items():
            if code not in proposed_codes:
                _save_revision(conn, plan, slot, "ai", "continue", "AI未返回该股票，保持原计划", _rules(conn, plan["id"]), [])
        conn.commit()
    finally:
        conn.close()
    if notification_events:
        from services.monitor import publish_events
        publish_events(notification_events)
    return count


def run_due_calibrations(now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return []
    trade_date, hhmm = now.date().isoformat(), now.strftime("%H:%M")
    completed = []
    for due, kind in CALIBRATION_SLOTS:
        if hhmm < due or not _claim_run(trade_date, due):
            continue
        try:
            count = run_open_calibration(trade_date) if kind == "open" else run_ai_calibration(trade_date, due)
            _finish_run(trade_date, due)
            logger.info("watch calibration %s completed plans=%d", due, count)
            completed.append(due)
        except Exception as exc:
            logger.exception("watch calibration %s failed", due)
            _finish_run(trade_date, due, str(exc))
    return completed


def calibration_bg_loop() -> None:
    logger.info("calibration_bg_loop: 校准调度线程已启动")
    while True:
        try:
            run_due_calibrations()
        except Exception:
            logger.exception("calibration_bg_loop failed")
        time.sleep(20)
