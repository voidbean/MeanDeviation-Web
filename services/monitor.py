"""分钟级次日盯盘规则执行与 SSE 事件分发。"""
import datetime as dt
import json
import queue
import sqlite3
import threading

from core.config import DB_PATH, logger

_subscribers: set[queue.Queue] = set()
_subscribers_lock = threading.Lock()


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


def evaluate_watch_rules(trade_date: str | None = None) -> list[dict]:
    """使用当分钟已保存的快照执行规则；相同规则每天最多触发一次。"""
    trade_date = trade_date or dt.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    events = []
    try:
        rows = conn.execute(
            """SELECT r.*,p.code,p.name FROM watch_rules r
               JOIN watch_plans p ON p.id=r.plan_id
               JOIN stock_watchlist w ON w.code=p.code AND w.enabled=1
               WHERE p.trade_date=? AND p.status='active' AND r.state!='triggered'""",
            (trade_date,),
        ).fetchall()
        for rule in rows:
            snap = conn.execute(
                "SELECT price FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 1",
                (rule["code"], trade_date),
            ).fetchone()
            if not snap:
                continue
            price, threshold = float(snap[0]), float(rule["threshold"])
            kind = rule["rule_type"]
            hit = ((kind == "breakout" and price >= threshold) or
                   (kind == "breakdown" and price <= threshold) or
                   (kind == "near" and abs(price - threshold) / threshold <= 0.005))
            hits = int(rule["consecutive_hits"] or 0) + 1 if hit else 0
            if hits < int(rule["confirmation_minutes"]):
                conn.execute("UPDATE watch_rules SET consecutive_hits=? WHERE id=?", (hits, rule["id"]))
                continue
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            labels = {"breakout": "突破", "breakdown": "跌破", "near": "接近"}
            message = rule["message"] or f"{rule['name'] or rule['code']} {labels[kind]}关键价 {threshold:g}"
            cur = conn.execute(
                "INSERT INTO watch_events(rule_id,code,name,event_type,priority,price,message,triggered_at) VALUES(?,?,?,?,?,?,?,?)",
                (rule["id"], rule["code"], rule["name"], kind, rule["priority"], price, message, now),
            )
            conn.execute("UPDATE watch_rules SET state='triggered',consecutive_hits=?,triggered_at=? WHERE id=?",
                         (hits, now, rule["id"]))
            event = {"id": cur.lastrowid, "code": rule["code"], "name": rule["name"], "event_type": kind,
                     "priority": rule["priority"], "price": price, "message": message, "triggered_at": now}
            events.append(event)
        conn.commit()
    except Exception:
        logger.exception("evaluate_watch_rules failed")
    finally:
        conn.close()
    for event in events:
        _publish(event)
    return events


def sse_payload(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
