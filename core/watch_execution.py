"""Manual execution feedback. One SQLite transaction owns ledger, cash and position.

This records actual fills, not orders. Undo is deliberately conservative: only
the latest active fill and only while its affected account state is unchanged.
"""
import datetime as dt
import json
import math
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 100_000_000:
        raise ValueError("股数必须为正整数")
    return value


def _number(value, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("价格/费用必须为有效数字")
    if value < 0 or (not zero and value == 0) or value > 1_000_000_000:
        raise ValueError("价格/费用超出范围")
    return value


def _position(conn, code):
    row = conn.execute("SELECT * FROM portfolio WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def _snapshot(conn, code, rule_ids):
    row = conn.execute("SELECT * FROM account_settings WHERE setting_key='available_cash'").fetchone()
    rules = []
    for rule_id in rule_ids:
        item = conn.execute("SELECT id,execution_status,target_quantity FROM watch_rules WHERE id=?", (rule_id,)).fetchone()
        if item:
            rules.append(dict(item))
    return {"position": _position(conn, code), "cash": dict(row) if row else None, "rules": rules}


def submit_feedback(db_path, event_id, body):
    if not isinstance(body, dict):
        raise ValueError("反馈格式无效")
    action = body.get("action")
    if action not in {"fill", "snooze", "ignore", "disable"}:
        raise ValueError("不支持的反馈动作")
    now = dt.datetime.now()
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        payload = json.dumps({**body, "event_id": event_id}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if action == "fill":
            key = body.get("request_id")
            if not isinstance(key, str) or not 16 <= len(key) <= 100:
                raise ValueError("缺少有效的幂等请求编号")
            existing = conn.execute("SELECT * FROM watch_executions WHERE request_id=?", (key,)).fetchone()
            if existing:
                if existing["payload_json"] != payload:
                    raise ValueError("相同请求编号不能用于不同成交")
                return {"ok": True, "execution_id": existing["id"], "duplicate": True,
                        "voided": existing["voided_at"] is not None}
        row = conn.execute("""SELECT r.*,p.code,p.name,p.trade_date,e.event_type FROM watch_events e
            JOIN watch_rules r ON r.id=e.rule_id JOIN watch_plans p ON p.id=r.plan_id WHERE e.id=?""", (event_id,)).fetchone()
        if not row:
            raise ValueError("提醒对应规则已不存在")
        if row["execution_status"] in {"completed", "disabled"}:
            raise ValueError("该动作已完成或停用；不要重复记账")
        if action != "fill":
            if row["trade_date"] != now.date().isoformat():
                raise ValueError("只能调整今日提醒")
            if action in {"snooze", "ignore"} and row["priority"] == "risk":
                raise ValueError("风险提醒不能被普通稍后/忽略操作屏蔽；如确需停用请明确选择今日停用")
            if action == "disable":
                conn.execute("UPDATE watch_rules SET execution_status='disabled' WHERE id=?", (row["id"],))
            elif action == "snooze":
                until = (now + dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("""UPDATE watch_rules SET snooze_until=?,state='waiting',consecutive_hits=0,
                    recovery_hits=0,ignore_until_recovery=0,active_event_id=NULL WHERE id=?""", (until, row["id"]))
            else:
                conn.execute("""UPDATE watch_rules SET ignore_until_recovery=1,snooze_until=NULL,
                    recovery_hits=0,active_event_id=NULL WHERE id=?""", (row["id"],))
            conn.commit()
            return {"ok": True}

        if row["action"] not in {"entry", "add", "reduce", "exit"} or row["event_type"] in {"near", "recovered", "calibration"}:
            raise ValueError("该提醒不是已确认的买卖动作，请使用交易复盘记录其他成交")
        direction = "买入" if row["action"] in {"entry", "add"} else "卖出"
        if body.get("direction") != direction:
            raise ValueError("成交方向与计划动作不一致")
        quantity = _positive_int(body.get("quantity"))
        target = _positive_int(body.get("target_quantity"))
        price = _number(body.get("price"))
        fee = _number(body.get("fee", 0), zero=True)
        trade_time = dt.datetime.fromisoformat(str(body.get("trade_time", "")))
        if trade_time.tzinfo is not None or trade_time > now or trade_time.date().isoformat() != row["trade_date"]:
            raise ValueError("成交时间须为计划当日的本地时间，且不能在未来")
        filled = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM watch_executions WHERE rule_id=? AND voided_at IS NULL",
                              (row["id"],)).fetchone()[0]
        if row["target_quantity"] is not None and target != row["target_quantity"]:
            raise ValueError("部分成交后不能改变本动作总股数；更正请先撤销最近成交")
        if filled + quantity > target:
            raise ValueError("累计成交不能超过本动作总股数")
        position = _position(conn, row["code"])
        held = int(position["quantity"] or 0) if position else 0
        cost = float(position["cost_price"] or 0) if position and held > 0 else 0
        cash_row = conn.execute("SELECT value FROM account_settings WHERE setting_key='available_cash'").fetchone()
        cash = float(cash_row[0]) if cash_row else 0
        gross = Decimal(str(price)) * quantity
        cash_delta = _money(-(gross + Decimal(str(fee))) if direction == "买入" else gross - Decimal(str(fee)))
        if direction == "卖出" and quantity > held:
            raise ValueError("卖出股数超过账面持仓，请先核对持仓")
        if cash + cash_delta < -0.001:
            raise ValueError("账面现金不足，请先核对可用现金；不要重复记录已手动入账的成交")
        if direction == "买入" and row["action"] == "entry" and held > 0 and filled == 0:
            raise ValueError("已有持仓，首次建仓动作不适用；请核对是否已手动记账")
        if row["action"] == "add" and held <= 0:
            raise ValueError("没有持仓，补仓动作不适用")
        affected = [row["id"]]
        new_quantity = held + quantity if direction == "买入" else held - quantity
        if new_quantity == 0:
            affected += [r[0] for r in conn.execute("""SELECT r.id FROM watch_rules r JOIN watch_plans p ON p.id=r.plan_id
                WHERE p.code=? AND p.trade_date=? AND r.id!=? AND r.action IN ('add','reduce','exit')
                AND r.execution_status IN ('pending','partial')""", (row["code"], row["trade_date"], row["id"]))]
        elif direction == "买入":
            affected += [r[0] for r in conn.execute("""SELECT r.id FROM watch_rules r JOIN watch_plans p ON p.id=r.plan_id
                WHERE p.code=? AND p.trade_date=? AND r.id!=? AND r.action='entry'
                AND r.execution_status='pending'""", (row["code"], row["trade_date"], row["id"]))]
        before = _snapshot(conn, row["code"], affected)
        new_cost = round((cost * held + float(gross) + fee) / new_quantity, 4) if direction == "买入" else cost if new_quantity else 0
        conn.execute("""INSERT INTO portfolio(code,cost_price,quantity,stage_high,stage_low,max_price,updated_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET cost_price=excluded.cost_price,
            quantity=excluded.quantity,max_price=excluded.max_price,updated_at=excluded.updated_at""",
            (row["code"], new_cost, new_quantity, position["stage_high"] if position else 0,
             position["stage_low"] if position else 0,
             max(float(position["max_price"] or 0) if position and held else 0, price) if new_quantity else 0, int(time.time())))
        conn.execute("""INSERT INTO account_settings(setting_key,value,updated_at) VALUES('available_cash',?,?)
            ON CONFLICT(setting_key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (_money(cash + cash_delta), int(time.time())))
        conn.execute("UPDATE watch_rules SET execution_status=?,target_quantity=? WHERE id=?",
                     ("completed" if filled + quantity == target or new_quantity == 0 else "partial", target, row["id"]))
        for rule_id in affected[1:]:
            conn.execute("UPDATE watch_rules SET execution_status='disabled' WHERE id=?", (rule_id,))
        from core.strategy import to_ts_code
        log_id = conn.execute("""INSERT INTO trade_log(code,name,trade_time,direction,price,volume,thought,emotion)
            VALUES(?,?,?,?,?,?,?,'冷静')""", (to_ts_code(row["code"]), row["name"], trade_time.strftime("%Y-%m-%d %H:%M:%S"),
            direction, price, quantity, f"盯盘成交反馈 #{event_id}；费用 {fee:.2f}；请勿再次手动入账")).lastrowid
        after = _snapshot(conn, row["code"], affected)
        execution_id = conn.execute("""INSERT INTO watch_executions(request_id,payload_json,event_id,rule_id,code,
            trade_log_id,quantity,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (key, payload, event_id, row["id"], row["code"], log_id, quantity,
             json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True), now.strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
        conn.commit()
        return {"ok": True, "execution_id": execution_id, "execution_status": "completed" if filled + quantity == target or new_quantity == 0 else "partial"}
    finally:
        conn.close()


def undo_execution(db_path, execution_id):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM watch_executions WHERE id=?", (execution_id,)).fetchone()
        if not row:
            raise ValueError("成交记录不存在")
        if row["voided_at"]:
            return {"ok": True, "duplicate": True}
        latest = conn.execute("SELECT MAX(id) FROM watch_executions WHERE voided_at IS NULL").fetchone()[0]
        if latest != execution_id:
            raise ValueError("为避免覆盖后续账目，只能按逆序撤销最近成交")
        before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
        rule_ids = [r["id"] for r in after["rules"]]
        if _snapshot(conn, row["code"], rule_ids) != after:
            raise ValueError("成交后持仓、现金或执行状态已被修改，不能自动回滚；请先核对账目")
        for table, key, value, saved in (("portfolio", "code", row["code"], before["position"]),
                                        ("account_settings", "setting_key", "available_cash", before["cash"])):
            conn.execute(f"DELETE FROM {table} WHERE {key}=?", (value,))
            if saved:
                conn.execute(f"INSERT INTO {table}({','.join(saved)}) VALUES({','.join('?' for _ in saved)})", tuple(saved.values()))
        for rule in before["rules"]:
            conn.execute("UPDATE watch_rules SET execution_status=?,target_quantity=? WHERE id=?",
                         (rule["execution_status"], rule["target_quantity"], rule["id"]))
        conn.execute("DELETE FROM trade_log WHERE id=?", (row["trade_log_id"],))
        conn.execute("UPDATE watch_executions SET voided_at=? WHERE id=?", (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), execution_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def event_details(conn, event_id):
    cur = conn.execute("""SELECT e.*,r.action,r.execution_status,r.target_quantity,r.snooze_until,
        r.ignore_until_recovery,p.trade_date,
        (SELECT COALESCE(SUM(quantity),0) FROM watch_executions x WHERE x.rule_id=e.rule_id AND x.voided_at IS NULL) AS filled_quantity,
        (SELECT MAX(id) FROM watch_executions x WHERE x.rule_id=e.rule_id AND x.voided_at IS NULL) AS last_execution_id
        FROM watch_events e LEFT JOIN watch_rules r ON r.id=e.rule_id
        LEFT JOIN watch_plans p ON p.id=r.plan_id WHERE e.id=?""", (event_id,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None
