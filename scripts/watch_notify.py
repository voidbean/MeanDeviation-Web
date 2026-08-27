#!/usr/bin/env python3
"""8848 盯盘本地通知监听器：SSE 实时接收 + API 定时补漏。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

STATE_DIR = Path(os.getenv("WATCH_NOTIFY_STATE_DIR", "~/.local/state/8848-watch")).expanduser()
CURSOR_FILE = STATE_DIR / "last_event_id"
STOP = threading.Event()
EVENT_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue()


def _log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def _apple_script_text(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def system_notification(title: str, message: str, priority: str = "observe") -> None:
    """发送 macOS 系统通知；其他系统保留终端输出，不让监听器因此退出。"""
    if platform.system() != "Darwin":
        return
    sound = ' sound name "Submarine"' if priority == "risk" else ""
    script = (
        f'display notification "{_apple_script_text(message)}" '
        f'with title "{_apple_script_text(title)}"{sound}'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _log(f"系统通知发送失败：{exc}")


def _load_cursor() -> int | None:
    try:
        return int(CURSOR_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _save_cursor(value: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CURSOR_FILE.with_suffix(".tmp")
    temporary.write_text(str(value), encoding="utf-8")
    temporary.replace(CURSOR_FILE)


class EventConsumer:
    def __init__(self, cursor: int) -> None:
        self.cursor = cursor
        self._seen: set[int] = set()
        self._lock = threading.Lock()

    def handle(self, item: dict[str, Any]) -> None:
        try:
            event_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            return
        if event_id <= 0:
            return
        with self._lock:
            if event_id <= self.cursor or event_id in self._seen:
                return
            self._seen.add(event_id)
            self.cursor = max(self.cursor, event_id)
            _save_cursor(self.cursor)
        name = item.get("name") or item.get("code") or "未知标的"
        event_type = "校准" if item.get("event_type") == "calibration" else "规则触发"
        priority = str(item.get("priority") or "observe")
        price = item.get("price")
        title = f"8848 · {name} · {event_type}"
        message = str(item.get("message") or "")
        if price is not None:
            message = f"{message}（{price}）"
        _log(f"[{priority}] {title} {message}")
        system_notification(title, message, priority)


def _fetch_events(session: requests.Session, base_url: str, after_id: int) -> tuple[list[dict], dict]:
    response = session.get(
        f"{base_url}/api/watch_events",
        params={"after_id": after_id, "limit": 100},
        timeout=(5, 15),
    )
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("events") or []), dict(payload.get("health") or {})


def _sse_loop(base_url: str) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": "8848-watch-notify/1.0"})
    delay = 2
    while not STOP.is_set():
        try:
            with session.get(f"{base_url}/monitor_stream", stream=True, timeout=(5, 45)) as response:
                response.raise_for_status()
                delay = 2
                data_lines: list[str] = []
                for raw in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if STOP.is_set():
                        return
                    line = raw or ""
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        try:
                            EVENT_QUEUE.put(json.loads("\n".join(data_lines)))
                        except json.JSONDecodeError:
                            pass
                        data_lines.clear()
        except requests.RequestException as exc:
            _log(f"SSE 断开：{exc}；{delay} 秒后重连")
            STOP.wait(delay)
            delay = min(delay * 2, 60)


def _bootstrap(session: requests.Session, base_url: str, replay: bool) -> EventConsumer:
    saved = _load_cursor()
    if saved is not None:
        return EventConsumer(saved)
    events, _ = _fetch_events(session, base_url, 0)
    latest = max((int(item.get("id") or 0) for item in events), default=0)
    consumer = EventConsumer(0)
    if replay:
        for item in events:
            consumer.handle(item)
    else:
        consumer.cursor = latest
        _save_cursor(latest)
        _log(f"首次启动，从事件 #{latest} 之后开始监听；使用 --replay 可回放现有事件")
    return consumer


def main() -> int:
    parser = argparse.ArgumentParser(description="监听 8848 服务器并发送本地系统通知")
    parser.add_argument("--server", help="覆盖 .env 中的 WATCH_SERVER_URL")
    parser.add_argument("--replay", action="store_true", help="首次运行时通知服务器现有事件")
    parser.add_argument("--test-notification", action="store_true", help="发送测试通知后退出")
    args = parser.parse_args()
    if args.test_notification:
        system_notification("8848 · 测试通知", "本地系统通知工作正常", "risk")
        return 0

    base_url = (args.server or os.getenv("WATCH_SERVER_URL", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        parser.error("请在根目录 .env 设置 WATCH_SERVER_URL，例如 http://1.2.3.4:8848")
    try:
        poll_seconds = max(5, float(os.getenv("WATCH_NOTIFY_POLL_SECONDS", "15")))
    except ValueError:
        poll_seconds = 15

    session = requests.Session()
    session.headers.update({"User-Agent": "8848-watch-notify/1.0"})
    try:
        consumer = _bootstrap(session, base_url, args.replay)
    except (requests.RequestException, ValueError) as exc:
        _log(f"无法连接服务器：{exc}")
        return 2

    def stop_handler(_signum, _frame) -> None:
        STOP.set()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    threading.Thread(target=_sse_loop, args=(base_url,), daemon=True, name="watch-sse").start()
    _log(f"开始监听 {base_url}，补漏间隔 {poll_seconds:g} 秒，当前游标 #{consumer.cursor}")

    failed_since: float | None = None
    outage_notified = False
    last_health_log = 0.0
    next_poll = 0.0
    while not STOP.is_set():
        now = time.monotonic()
        if now >= next_poll:
            try:
                events, health = _fetch_events(session, base_url, consumer.cursor)
                for item in events:
                    consumer.handle(item)
                if failed_since is not None and outage_notified:
                    system_notification("8848 · 连接恢复", "盯盘服务器连接已经恢复", "observe")
                failed_since = None
                outage_notified = False
                if now - last_health_log >= 300:
                    latest = health.get("latest_time") or "—"
                    _log(f"连接正常 · 行情 {health.get('market_state', 'unknown')} · 最新快照 {latest}")
                    last_health_log = now
            except (requests.RequestException, ValueError) as exc:
                failed_since = failed_since or now
                _log(f"补漏请求失败：{exc}")
                if not outage_notified and now - failed_since >= 180:
                    system_notification("8848 · 连接中断", "连续 3 分钟无法连接盯盘服务器", "risk")
                    outage_notified = True
            next_poll = time.monotonic() + poll_seconds
        try:
            # SSE 事件到达后立即唤醒；轮询只负责断线补漏。
            consumer.handle(EVENT_QUEUE.get(timeout=max(0.1, next_poll - time.monotonic())))
        except queue.Empty:
            pass
    _log("监听器已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
