"""分钟级次日盯盘规则执行与 SSE 事件分发。"""
import datetime as dt
import json
import queue
import sqlite3
import threading

from core.config import DB_PATH, logger
from core.db import get_available_cash
from services.watch_plan_context import render_live_rule_message
from services.watch_features import build_watch_context, evaluate_conditions, shadow_summary
from core.watch_execution import event_details

_subscribers: set[queue.Queue] = set()
_subscribers_lock = threading.Lock()

# 价格必须离开关键位一小段距离才算真正收复/重新跌回。确认分钟负责过滤
# 短促刺穿，滞回区间负责过滤 0.999/1.001 一类贴线抖动。
PRICE_RECOVERY_HYSTERESIS = 0.002


def _action_direction(message: str) -> str:
    text = message or ""
    if any(word in text for word in ("放弃买入", "暂停买入", "停止买入", "停止补仓", "不再买入", "不执行买入")):
        return "cancel"
    if any(word in text for word in ("买入", "建仓", "补仓", "加仓")):
        return "buy"
    if any(word in text for word in ("卖出", "减仓", "止盈", "止损", "离场")):
        return "sell"
    return ""


def _rule_direction(rule) -> str:
    """Use explicit operation intent; parse text only for legacy rows."""
    action = str(rule.get("action") or "")
    if action in {"entry", "add"}:
        return "buy"
    if action in {"reduce", "exit"}:
        return "sell"
    if action == "cancel":
        return "cancel"
    if action == "observe":
        return ""
    return _action_direction(str(rule.get("message") or ""))


def _indicator_label(rule: sqlite3.Row) -> str:
    explicit = str(rule["indicator_label"] or "").strip()
    if explicit:
        return explicit
    message = str(rule["message"] or "")
    known = ("分时均价", "均价", "8848上轨", "8848下轨", "MA5", "MA10", "MA20",
             "Fibonacci 0.382", "Fibonacci 0.618", "Fibonacci 0.786", "前高", "前低")
    return next((name for name in known if name in message), "计划关键线")


def _intraday_avg(conn: sqlite3.Connection, code: str, trade_date: str) -> float | None:
    row = conn.execute(
        "SELECT SUM(vol),SUM(amount) FROM intraday_snapshots WHERE code=? AND date=?",
        (code, trade_date),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    # 快照保存的是分钟增量；当日均价必须累加，不能拿最后一分钟均价代替黄线。
    return float(row[1]) * 1000 / float(row[0])


def _evidence(rule: sqlite3.Row, price: float, avg: float | None) -> str:
    label = _indicator_label(rule)
    line_value = avg if avg and any(x in label for x in ("分时均价", "黄线")) else float(rule["threshold"])
    parts = [f"{label} {line_value:g}", f"当前价 {price:g}"]
    if avg and avg > 0:
        parts.append(f"当前分时均价 {avg:.3f}")
    return "｜".join(parts)


def _confirmed_message(rule: sqlite3.Row, price: float, avg: float | None, minutes: int,
                       available_cash: float | None = None) -> str:
    stored = str(rule["message"] or "").strip()
    original = render_live_rule_message(
        stored, get_available_cash() if available_cash is None else available_cash, price,
    )
    direction = _rule_direction(rule)
    if direction == "buy":
        action = "建议买入/补仓" if any(x in original for x in ("补仓", "加仓")) else "建议买入"
    elif direction == "sell":
        action = "建议卖出/减仓" if any(x in original for x in ("减仓", "止盈")) else "建议卖出/止损"
    elif direction == "cancel":
        action = "建议放弃买入/停止补仓"
    else:
        action = "条件已确认"
    return f"✅ 已连续 {minutes} 分钟确认，{action}。{_evidence(rule, price, avg)}。原计划：{original or '—'}"


def subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=100)
    with _subscribers_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _subscribers_lock:
        _subscribers.discard(q)


def _publish(event: dict) -> None:
    with _subscribers_lock:
        subscribers = list(_subscribers)
    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


def publish_events(events: list[dict]) -> None:
    """向当前在线页面推送已持久化事件；离线页面会通过 API 回补。"""
    for event in events:
        _publish(event)


def _condition(kind: str, price: float, threshold: float, latest_vol: float,
               conn: sqlite3.Connection, code: str, trade_date: str) -> bool:
    if kind == "breakout":
        return price >= threshold
    if kind == "breakdown":
        return price <= threshold
    if kind == "near":
        return abs(price - threshold) / threshold <= 0.005
    if kind == "rapid_move_5m":
        old = conn.execute(
            "SELECT price FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 1 OFFSET 5",
            (code, trade_date),
        ).fetchone()
        return bool(old and old[0] and abs((price - float(old[0])) / float(old[0]) * 100) >= threshold)
    if kind == "volume_spike":
        vols = conn.execute(
            "SELECT vol FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 20 OFFSET 1",
            (code, trade_date),
        ).fetchall()
        baseline = sum(float(v[0] or 0) for v in vols) / len(vols) if vols else 0
        return baseline > 0 and latest_vol / baseline >= threshold
    return False


def _recovered(kind: str, price: float, threshold: float, hit: bool) -> bool:
    if kind == "breakout":
        return price <= threshold * (1 - PRICE_RECOVERY_HYSTERESIS)
    if kind == "breakdown":
        return price >= threshold * (1 + PRICE_RECOVERY_HYSTERESIS)
    # 接近、异动和放量都是瞬态条件；明确离开触发区后即可重新布防。
    if kind == "near":
        return abs(price - threshold) / threshold >= 0.007
    return not hit


def evaluate_watch_rules(trade_date: str | None = None) -> list[dict]:
    """执行规则并持续维护触发/恢复状态；INVALID 才停止跟踪。"""
    trade_date = trade_date or dt.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    events = []
    contexts, shadow_results = {}, {}
    available_cash = get_available_cash()
    try:
        # Serialize with fills/feedback and other evaluators. The snapshot cursor
        # and confirmation counters must commit together.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT r.*,p.code,p.name FROM watch_rules r
               JOIN watch_plans p ON p.id=r.plan_id
               JOIN stock_watchlist w ON w.code=p.code AND w.enabled=1
               WHERE p.trade_date=? AND p.status='active' AND r.state!='invalid' AND COALESCE(r.paused,0)=0
               AND r.execution_status IN ('pending','partial')""",
            (trade_date,),
        ).fetchall()
        for rule in rows:
            rule = dict(rule)
            snap = conn.execute(
                "SELECT price,open,time,vol FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 1",
                (rule["code"], trade_date),
            ).fetchone()
            if not snap:
                continue
            price, open_price, snap_time, latest_vol = float(snap[0]), float(snap[1] or 0), str(snap[2]), float(snap[3] or 0)
            captured_at = dt.datetime.strptime(f"{trade_date} {snap_time}", "%Y-%m-%d %H:%M")
            if trade_date == dt.date.today().isoformat():
                if (dt.datetime.now() - captured_at).total_seconds() > 150:
                    continue
            previous = rule["last_snapshot_time"]
            if previous and snap_time <= previous:
                continue
            if previous and (captured_at - dt.datetime.strptime(f"{trade_date} {previous}", "%Y-%m-%d %H:%M")).total_seconds() != 60:
                rule["consecutive_hits"] = rule["recovery_hits"] = 0
            conn.execute("UPDATE watch_rules SET last_snapshot_time=?,consecutive_hits=?,recovery_hits=? WHERE id=?",
                         (snap_time, rule["consecutive_hits"], rule["recovery_hits"], rule["id"]))
            if rule["snooze_until"] and captured_at.strftime("%Y-%m-%d %H:%M:%S") < rule["snooze_until"]:
                continue
            if rule["snooze_until"]:
                conn.execute("UPDATE watch_rules SET snooze_until=NULL WHERE id=?", (rule["id"],))
            held = conn.execute("SELECT COALESCE(quantity,0) FROM portfolio WHERE code=?", (rule["code"],)).fetchone()
            if held and ((rule["action"] == "entry" and held[0] > 0 and rule["execution_status"] != "partial") or
                         (rule["action"] in {"add", "reduce", "exit"} and not held[0])):
                conn.execute("UPDATE watch_rules SET consecutive_hits=0,recovery_hits=0 WHERE id=?", (rule["id"],))
                continue  # Known position is incompatible; never invent a fill.
            current_avg = _intraday_avg(conn, rule["code"], trade_date)
            threshold = float(rule["threshold"])
            # “分时均价/黄线”会随成交持续变化，不能拿生成计划时的旧均价盯一整天。
            if current_avg and any(x in _indicator_label(rule) for x in ("分时均价", "黄线")):
                threshold = current_avg
            kind = rule["rule_type"]
            hit = _condition(kind, price, threshold, latest_vol, conn, rule["code"], trade_date)
            if rule["ignore_until_recovery"]:
                recovered = int(rule["recovery_hits"] or 0) + 1 if _recovered(kind, price, threshold, hit) else 0
                if recovered >= int(rule["confirmation_minutes"]):
                    conn.execute("""UPDATE watch_rules SET ignore_until_recovery=0,state='waiting',
                        consecutive_hits=0,recovery_hits=0 WHERE id=?""", (rule["id"],))
                else:
                    conn.execute("UPDATE watch_rules SET recovery_hits=? WHERE id=?", (recovered, rule["id"]))
                continue
            # Shadow checks are independent of the original state machine, including
            # risk exits. Keep one audit row per rule/snapshot, even on repeated polls.
            try:
                if rule["code"] not in contexts:
                    contexts[rule["code"]] = build_watch_context(conn, rule["code"], trade_date)
                evaluation = evaluate_conditions(json.loads(rule["conditions_json"]),
                                                 contexts[rule["code"]], price, trade_date)
            except Exception:
                # Technical-data failure must not suppress the original risk alerts.
                logger.exception("watch shadow data unavailable code=%s", rule["code"])
                evaluation = {"mode": "shadow", "status": "unknown", "checks": [
                    {"label": "技术数据读取", "status": "unknown", "actual": None}]}
            evaluation.update({"as_of": f"{trade_date} {snap_time}", "base_hit": hit})
            evaluation["summary"] = shadow_summary(evaluation)
            shadow_results[rule["id"]] = (evaluation, snap_time)
            encoded = json.dumps(evaluation, ensure_ascii=False)
            conn.execute("UPDATE watch_rules SET shadow_result_json=? WHERE id=?", (encoded, rule["id"]))
            conn.execute(
                """INSERT INTO watch_shadow_checks(rule_id,trade_date,snapshot_time,code,price,base_hit,
                   evaluation_json,rule_json) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(rule_id,trade_date,snapshot_time) DO UPDATE SET
                   price=excluded.price,base_hit=excluded.base_hit,evaluation_json=excluded.evaluation_json,
                   rule_json=excluded.rule_json""",
                (rule["id"], trade_date, snap_time, rule["code"], price, int(hit), encoded,
                 json.dumps({"type": kind, "threshold": threshold, "confirmation_minutes": rule["confirmation_minutes"],
                             "conditions": json.loads(rule["conditions_json"]), "message": rule["message"]}, ensure_ascii=False)),
            )
            required_hits = int(rule["confirmation_minutes"])
            if rule["state"] == "observing":
                direction = _rule_direction(rule)
                confirmed = ((direction == "buy" and price >= threshold * (1 + PRICE_RECOVERY_HYSTERESIS)) or
                             (direction in {"sell", "cancel"} and price <= threshold * (1 - PRICE_RECOVERY_HYSTERESIS)))
                cancelled = ((direction == "buy" and price <= threshold * (1 - PRICE_RECOVERY_HYSTERESIS)) or
                             (direction in {"sell", "cancel"} and price >= threshold * (1 + PRICE_RECOVERY_HYSTERESIS)))
                hits = int(rule["consecutive_hits"] or 0) + 1 if confirmed else 0
                recovery_hits = int(rule["recovery_hits"] or 0) + 1 if cancelled else 0
                if hits >= required_hits:
                    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    message = _confirmed_message(rule, price, current_avg, required_hits, available_cash)
                    cur = conn.execute(
                        "INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at) VALUES(?,?,?,?,?,?,?,?)",
                        (rule["id"], rule["code"], rule["name"], "action_confirmed", rule["priority"], price, message, now),
                    )
                    conn.execute("""UPDATE watch_rules SET state='triggered',consecutive_hits=?,recovery_hits=0,
                                 triggered_at=?,state_changed_at=? WHERE id=?""", (hits, now, now, rule["id"]))
                    events.append({"id": cur.lastrowid, "code": rule["code"], "name": rule["name"],
                                   "event_type": "action_confirmed", "priority": rule["priority"], "price": price,
                                   "message": message, "triggered_at": now})
                elif recovery_hits >= required_hits:
                    conn.execute("""UPDATE watch_rules SET state='waiting',consecutive_hits=0,recovery_hits=0,
                                 state_changed_at=? WHERE id=?""",
                                 (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rule["id"]))
                else:
                    conn.execute("UPDATE watch_rules SET consecutive_hits=?,recovery_hits=? WHERE id=?",
                                 (hits, recovery_hits, rule["id"]))
                continue
            if rule["state"] == "triggered":
                recovery_hits = int(rule["recovery_hits"] or 0) + 1 if _recovered(kind, price, threshold, hit) else 0
                if recovery_hits < required_hits:
                    conn.execute("UPDATE watch_rules SET recovery_hits=? WHERE id=?", (recovery_hits, rule["id"]))
                    continue
                now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                labels = {"breakout": "突破", "breakdown": "跌破", "near": "接近",
                          "rapid_move_5m": "5分钟异动", "volume_spike": "分钟放量"}
                detail = (f"价格已离开目标价 {threshold:g} 元附近" if kind == "near"
                          else f"此前的{labels[kind]}提醒条件已解除")
                message = (f"🔄 {rule['name'] or rule['code']} 提醒解除：{detail}。"
                           f"已按 {required_hits} 分钟行情确认，继续监测，条件再次满足时会重新提醒。"
                           f"通知时价格 {price:g} 元。本条仅为状态通知，无需成交操作。")
                cur = conn.execute(
                    "INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at) VALUES(?,?,?,?,?,?,?,?)",
                    (rule["id"], rule["code"], rule["name"], "recovered", "observe", price, message, now),
                )
                conn.execute(
                    """UPDATE watch_rules SET state='waiting',consecutive_hits=0,recovery_hits=0,
                       triggered_at=NULL,state_changed_at=? WHERE id=?""", (now, rule["id"]),
                )
                events.append({"id": cur.lastrowid, "code": rule["code"], "name": rule["name"],
                               "event_type": "recovered", "priority": "observe", "price": price,
                               "message": message, "triggered_at": now})
                continue
            hits = int(rule["consecutive_hits"] or 0) + 1 if hit else 0
            opening_cross = snap_time <= "09:35" and open_price > 0 and (
                (kind == "breakout" and open_price >= threshold) or
                (kind == "breakdown" and open_price <= threshold)
            )
            required_hits = 1 if opening_cross else required_hits
            if hits < required_hits:
                conn.execute("UPDATE watch_rules SET consecutive_hits=? WHERE id=?", (hits, rule["id"]))
                continue
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            labels = {"breakout": "突破", "breakdown": "跌破", "near": "接近",
                      "rapid_move_5m": "5分钟异动", "volume_spike": "分钟放量"}
            if kind == "near":
                live_plan = render_live_rule_message(str(rule["message"] or ""), available_cash, price)
                message = (f"👀 已到达观察区，尚未形成操作确认；将继续检查站稳方向。"
                           f"{_evidence(rule, price, current_avg)}。计划：{live_plan}")
            else:
                message = _confirmed_message(rule, price, current_avg, required_hits, available_cash)
            if opening_cross:
                live_plan = render_live_rule_message(str(rule["message"] or ""), available_cash, price)
                message = f"开盘直接{labels[kind]}，已越级确认。{_evidence(rule, price, current_avg)}。计划：{live_plan}"
            cur = conn.execute(
                "INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at) VALUES(?,?,?,?,?,?,?,?)",
                (rule["id"], rule["code"], rule["name"], kind, rule["priority"], price, message, now),
            )
            next_state = "observing" if kind == "near" and _rule_direction(rule) else "triggered"
            conn.execute("""UPDATE watch_rules SET state=?,consecutive_hits=0,recovery_hits=0,
                         triggered_at=?,state_changed_at=? WHERE id=?""", (next_state, now, now, rule["id"]))
            event = {"id": cur.lastrowid, "code": rule["code"], "name": rule["name"], "event_type": kind,
                     "priority": rule["priority"], "price": price, "message": message, "triggered_at": now}
            events.append(event)
        for event in events:
            # Event dictionaries intentionally do not expose rule_id; resolve through
            # the just-inserted persisted event rather than matching by stock code.
            rule_id = conn.execute("SELECT rule_id FROM watch_events WHERE id=?", (event["id"],)).fetchone()[0]
            evaluation, snap_time = shadow_results[rule_id]
            if event["event_type"] != "recovered":
                event["message"] = event["message"].rstrip("。") + "。" + evaluation["summary"]
            event["shadow_result"] = evaluation
            conn.execute("UPDATE watch_events SET message=? WHERE id=?", (event["message"], event["id"]))
            if event["event_type"] not in {"near", "recovered"}:
                conn.execute("UPDATE watch_shadow_checks SET legacy_confirmed=1 "
                             "WHERE rule_id=? AND trade_date=? AND snapshot_time=?", (rule_id, trade_date, snap_time))
            rule = conn.execute("SELECT active_event_id,priority FROM watch_rules WHERE id=?", (rule_id,)).fetchone()
            prior = conn.execute("SELECT id,event_type FROM watch_events WHERE id=?", (rule[0],)).fetchone() if rule[0] else None
            if event["event_type"] != "recovered":
                if rule[1] != "risk" and prior and prior[1] == event["event_type"]:
                    conn.execute("UPDATE watch_events SET message=?,updated_at=?,repeat_count=repeat_count+1 WHERE id=?",
                                 (event["message"], event["triggered_at"], prior[0]))
                    conn.execute("DELETE FROM watch_events WHERE id=?", (event["id"],))
                    event["id"] = prior[0]
                    event["silent_update"] = True
                conn.execute("UPDATE watch_rules SET active_event_id=? WHERE id=?", (event["id"], rule_id))
            else:
                event["silent_update"] = True
            event.update(event_details(conn, event["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        events.clear()  # Never publish events whose transaction did not commit.
        logger.exception("evaluate_watch_rules failed")
    finally:
        conn.close()
    for event in events:
        _publish(event)
    return events


def get_monitor_health() -> dict:
    """返回盯盘行情的最新快照时间和是否延迟。"""
    today = dt.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT MAX(s.time) FROM intraday_snapshots s
           JOIN stock_watchlist w ON w.code=s.code AND w.enabled=1 WHERE s.date=?""",
        (today,),
    ).fetchone()
    conn.close()
    latest = row[0] if row and row[0] else None
    now = dt.datetime.now()
    trading = now.weekday() < 5 and (
        dt.time(9, 30) <= now.time() <= dt.time(11, 30) or dt.time(13, 0) <= now.time() <= dt.time(15, 0)
    )
    delayed = False
    if latest:
        captured_at = dt.datetime.strptime(f"{today} {latest}", "%Y-%m-%d %H:%M")
        delayed = trading and (now - captured_at).total_seconds() > 150
    return {"date": today, "latest_time": latest, "delayed": delayed,
            "market_state": "trading" if trading else "closed"}


def finalize_watch_event_outcomes(trade_date: str | None = None) -> int:
    """按提醒后余下交易时段的最高/最低价计算最大收益与回撤。"""
    trade_date = trade_date or dt.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id,code,price,substr(triggered_at,12,5) FROM watch_events WHERE date(triggered_at)=? AND evaluated_at IS NULL",
        (trade_date,),
    ).fetchall()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for event_id, code, trigger_price, trigger_time in rows:
        outcome = conn.execute(
            "SELECT MAX(price),MIN(price) FROM intraday_snapshots WHERE code=? AND date=? AND time>=?",
            (code, trade_date, trigger_time),
        ).fetchone()
        if not outcome or outcome[0] is None or not trigger_price:
            continue
        gain = (float(outcome[0]) - float(trigger_price)) / float(trigger_price) * 100
        drawdown = (float(outcome[1]) - float(trigger_price)) / float(trigger_price) * 100
        conn.execute(
            "UPDATE watch_events SET max_gain_pct=?,max_drawdown_pct=?,evaluated_at=? WHERE id=?",
            (round(gain, 2), round(drawdown, 2), now, event_id),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


def sse_payload(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
