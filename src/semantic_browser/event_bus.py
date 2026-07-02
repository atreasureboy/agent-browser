"""T55: 持久化 Event Bus — 跨 SSE 连接的状态共享与 Last-Event-ID 续传.

设计要点 (fable §3.1 简化版):
- SQLite WAL 存事件 (monotonic seq 全序游标, SSE id 字段)
- 单进程内 pub/sub via asyncio.Event + List[asyncio.Queue]
- event_id 用 ULID-ish (timestamp+random), 用于跨重启去重
- LRU + UNIQUE(event_id) 双层去重 (LLM usage 和 SSE 事件都可用)

接口:
  bus = EventBus(db_path)
  await bus.start()
  seq = bus.publish(topic, payload)
  events_iter = bus.subscribe(topics, since_seq=0)  # async generator
  events = bus.replay(since_seq=0, limit=500)       # 同步读 (SSE 重连用)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


def _make_event_id() -> str:
    """T55: event_id — UUID4 hex, 全局唯一, 用于去重."""
    return f"evt_{uuid.uuid4().hex[:24]}"


class EventBus:
    """持久化事件总线 — 单进程 / SQLite."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.expanduser("~/.semantic-browser/event_log.db")
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # 订阅者: dict[topic_pattern → list[Queue]]
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self._dedup_lru: OrderedDict[str, None] = OrderedDict()
        self._dedup_max = 200_000  # 与 fable §3.1 一致
        self._seq_cache: dict[str, int] = {}  # topic → last seq (for SSE id)
        self._seq_max = 0
        self._subs_lock = threading.Lock()

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        # 单连接; SQLite 线程安全, 写串行
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                seq        INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   TEXT UNIQUE NOT NULL,
                topic      TEXT NOT NULL,
                ts         REAL NOT NULL,
                payload    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_topic_seq ON events(topic, seq);
        """)
        conn.commit()
        self._conn = conn
        # 读 max seq, 用于续传游标
        cur = conn.execute("SELECT MAX(seq) FROM events")
        row = cur.fetchone()
        self._seq_max = int(row[0] or 0)
        logger.info("EventBus started (db=%s, max_seq=%d)", self.db_path, self._seq_max)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _is_dup(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._dedup_lru:
                return True
            self._dedup_lru[event_id] = None
            while len(self._dedup_lru) > self._dedup_max:
                self._dedup_lru.popitem(last=False)
            return False

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> int:
        """发布事件 — 同步, 返回新 seq (SSE id 字段). 事件落盘后才返回."""
        if self._conn is None:
            raise RuntimeError("EventBus not started")
        event_id = _make_event_id()
        if self._is_dup(event_id):
            # 极小概率 — 重发
            event_id = _make_event_id()
        ts = time.time()
        body = json.dumps(payload or {}, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(event_id, topic, ts, payload) VALUES (?, ?, ?, ?)",
                (event_id, topic, ts, body),
            )
            self._conn.commit()
            seq = int(cur.lastrowid or 0)
        if seq == 0:
            # UNIQUE 触发, 重发一遍
            return self.publish(topic, payload)
        if seq > self._seq_max:
            self._seq_max = seq
        # 通知订阅者 (跨线程)
        record = {"event_id": event_id, "seq": seq, "topic": topic, "ts": ts, "payload": payload or {}}
        self._fanout(topic, record)
        return seq

    def _fanout(self, topic: str, record: dict[str, Any]) -> None:
        # 同步调用, 异步事件循环里 fanout
        with self._subs_lock:
            matching_lists = []
            for pattern, qs in self._subs.items():
                if _topic_matches(pattern, topic):
                    matching_lists.extend(qs)
        for q in matching_lists:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                # 满则丢弃最旧 (合约: persistent 事件可以靠 replay 找回, page.* 不持久)
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(record)
                except asyncio.QueueFull:
                    pass

    def subscribe(self, topic: str, since_seq: int = 0) -> asyncio.Queue:
        """订阅 — 返回 asyncio.Queue, 调用方 await q.get()."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        with self._subs_lock:
            self._subs.setdefault(topic, []).append(q)
        # 立即注入 last seen seq (让 caller 先 replay 完存量, 再处理 live)
        q._since_seq_hint = since_seq  # type: ignore[attr-defined]
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        with self._subs_lock:
            lst = self._subs.get(topic, [])
            if q in lst:
                lst.remove(q)

    def replay(self, since_seq: int, topic: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """同步读取历史事件 — SSE Last-Event-ID 重连续传用."""
        if self._conn is None:
            return []
        if topic:
            rows = self._conn.execute(
                "SELECT seq, event_id, topic, ts, payload FROM events "
                "WHERE seq > ? AND topic = ? ORDER BY seq ASC LIMIT ?",
                (since_seq, topic, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT seq, event_id, topic, ts, payload FROM events "
                "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (since_seq, limit),
            ).fetchall()
        out = []
        for seq, eid, t, ts, body in rows:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"_raw": body}
            out.append({"event_id": eid, "seq": seq, "topic": t, "ts": ts, "payload": payload})
        return out

    @property
    def max_seq(self) -> int:
        return self._seq_max


def _topic_matches(pattern: str, topic: str) -> bool:
    """topic glob: 'session.*' matches 'session.created', 'foo' matches 'foo'.

    T59: '*' matches everything (wildcard catch-all for /events SSE stream).
    """
    if pattern == "*":
        return True
    if pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False
