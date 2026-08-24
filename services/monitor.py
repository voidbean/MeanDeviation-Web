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


def publish_events(events: list[dict]) -> None:
    """向当前在线页面推送已持久化事件；离线页面会通过 API 回补。"""
    for event in events:
        _publish(event)


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
               WHERE p.trade_date=? AND p.status='active' AND r.state!='triggered' AND COALESCE(r.paused,0)=0""",
            (trade_date,),
        ).fetchall()
        for rule in rows:
            snap = conn.execute(
                "SELECT price,open,time,vol FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 1",
                (rule["code"], trade_date),
            ).fetchone()
            if not snap:
                continue
            price, open_price, snap_time, latest_vol = float(snap[0]), float(snap[1] or 0), str(snap[2]), float(snap[3] or 0)
            if trade_date == dt.date.today().isoformat():
                captured_at = dt.datetime.strptime(f"{trade_date} {snap_time}", "%Y-%m-%d %H:%M")
                if (dt.datetime.now() - captured_at).total_seconds() > 150:
                    continue
            threshold = float(rule["threshold"])
            kind = rule["rule_type"]
            hit = False
            if kind == "breakout":
                hit = price >= threshold
            elif kind == "breakdown":
                hit = price <= threshold
            elif kind == "near":
                hit = abs(price - threshold) / threshold <= 0.005
            elif kind == "rapid_move_5m":
                old = conn.execute(
                    "SELECT price FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 1 OFFSET 5",
                    (rule["code"], trade_date),
                ).fetchone()
                hit = bool(old and old[0] and abs((price - float(old[0])) / float(old[0]) * 100) >= threshold)
            elif kind == "volume_spike":
                vols = conn.execute(
                    "SELECT vol FROM intraday_snapshots WHERE code=? AND date=? ORDER BY time DESC LIMIT 20 OFFSET 1",
                    (rule["code"], trade_date),
                ).fetchall()
                baseline = sum(float(v[0] or 0) for v in vols) / len(vols) if vols else 0
                hit = baseline > 0 and latest_vol / baseline >= threshold
            hits = int(rule["consecutive_hits"] or 0) + 1 if hit else 0
            opening_cross = snap_time <= "09:35" and open_price > 0 and (
                (kind == "breakout" and open_price >= threshold) or
                (kind == "breakdown" and open_price <= threshold)
            )
            required_hits = 1 if opening_cross else int(rule["confirmation_minutes"])
            if hits < required_hits:
                conn.execute("UPDATE watch_rules SET consecutive_hits=? WHERE id=?", (hits, rule["id"]))
                continue
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            labels = {"breakout": "突破", "breakdown": "跌破", "near": "接近",
                      "rapid_move_5m": "5分钟异动", "volume_spike": "分钟放量"}
            message = rule["message"] or f"{rule['name'] or rule['code']} {labels[kind]}关键价 {threshold:g}"
            if opening_cross:
                message = f"开盘直接{labels[kind]}关键价 {threshold:g}；原普通确认规则已越级触发。"
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
