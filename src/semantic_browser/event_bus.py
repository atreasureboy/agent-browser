"""T55 + T65.8: 持久化 Event Bus — 跨 SSE 连接的状态共享与 Last-Event-ID 续传.

设计要点 (fable §3.1):
- SQLite WAL 存事件 (monotonic seq 全序游标, SSE id 字段)
- 单进程内 pub/sub via asyncio.Event + List[asyncio.Queue]
- event_id 用 ULID (T65.8 升级自 UUID), 跨重启去重
- LRU + UNIQUE(event_id) 双层去重 (LLM usage 和 SSE 事件都可用)
- T65.8 加 schema: scope/scope_id/tenant_id/producer/provenance/dedup_key/persistent/expires_at
- dedup_key UNIQUE 兜底 + INSERT OR IGNORE 去重 (D18)

接口:
  bus = EventBus(db_path)
  await bus.start()
  seq = bus.publish(topic, payload, *, scope, scope_id, tenant_id, producer, dedup_key, persistent)
  events_iter = bus.subscribe(topics, since_seq=0, tenant_id=...)  # async generator
  events = bus.replay(since_seq=0, limit=500, tenant_id=...)       # 同步读 (SSE 重连用)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any, AsyncIterator

from semantic_browser.daemon.ulid import ulid_new

logger = logging.getLogger(__name__)


def _make_event_id() -> str:
    """T65.8: event_id 用 ULID (26 字符 time-ordered), 替代 UUID."""
    return f"evt_{ulid_new()}"


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
                seq            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id       TEXT UNIQUE NOT NULL,
                ts             REAL NOT NULL,
                topic          TEXT NOT NULL,
                scope          TEXT NOT NULL DEFAULT 'global',
                scope_id       TEXT,
                tenant_id      TEXT NOT NULL DEFAULT 'anonymous',
                producer_kind  TEXT NOT NULL DEFAULT 'system',
                producer_id    TEXT,
                provenance     TEXT NOT NULL DEFAULT 'trusted',
                dedup_key      TEXT,
                persistent     INTEGER NOT NULL DEFAULT 1,
                payload_json   TEXT NOT NULL,
                expires_at     REAL
            );
            CREATE INDEX IF NOT EXISTS idx_topic_seq ON events(topic, seq);
            CREATE INDEX IF NOT EXISTS idx_tenant_seq ON events(tenant_id, seq);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_key ON events(dedup_key)
                WHERE dedup_key IS NOT NULL;
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

    def publish(
        self, topic: str, payload: dict[str, Any] | None = None,
        *,
        scope: str = "global",
        scope_id: str | None = None,
        tenant_id: str = "anonymous",
        producer_kind: str = "system",
        producer_id: str | None = None,
        provenance: str = "trusted",
        dedup_key: str | None = None,
        persistent: bool = True,
        ttl_s: float | None = None,
    ) -> int:
        """发布事件 — 同步, 返回新 seq (SSE id 字段). 事件落盘后才返回.

        T65.8 加 schema: scope/scope_id/tenant_id/producer/provenance/dedup_key/persistent.
        """
        if self._conn is None:
            raise RuntimeError("EventBus not started")
        event_id = _make_event_id()
        if self._is_dup(event_id):
            # 极小概率 — 重发
            event_id = _make_event_id()
        ts = time.time()
        body = json.dumps(payload or {}, ensure_ascii=False)
        expires_at = ts + ttl_s if (ttl_s and persistent) else None
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO events(event_id, ts, topic, scope, scope_id, "
                "tenant_id, producer_kind, producer_id, provenance, dedup_key, "
                "persistent, payload_json, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, ts, topic, scope, scope_id, tenant_id,
                 producer_kind, producer_id, provenance, dedup_key,
                 1 if persistent else 0, body, expires_at),
            )
            self._conn.commit()
            seq = int(cur.lastrowid or 0)
        if seq == 0:
            # UNIQUE 触发 (event_id 或 dedup_key) — 不重发, 返 0 表示 dedup hit
            # 调用方可用 return value 判断是否是新事件
            if seq > self._seq_max:
                self._seq_max = seq
            return 0
        if seq > self._seq_max:
            self._seq_max = seq
        # 通知订阅者 (跨线程)
        record = {
            "event_id": event_id, "seq": seq, "topic": topic, "ts": ts,
            "scope": scope, "scope_id": scope_id, "tenant_id": tenant_id,
            "producer": {"kind": producer_kind, "id": producer_id},
            "provenance": provenance, "dedup_key": dedup_key, "persistent": persistent,
            "payload": payload or {},
        }
        self._fanout(topic, record)
        return seq

    def _fanout(self, topic: str, record: dict[str, Any]) -> None:
        # 同步调用, 异步事件循环里 fanout
        event_tenant = record.get("tenant_id", "anonymous")
        with self._subs_lock:
            matching_lists = []
            for sub_key, qs in self._subs.items():
                # sub_key 格式: "{tenant_id or *}::{topic_pattern}"
                # 同时支持老的无 tenant 前缀 key (向下兼容)
                if "::" in sub_key:
                    sub_tenant, pattern = sub_key.split("::", 1)
                else:
                    sub_tenant, pattern = "*", sub_key
                # tenant 过滤: * 或匹配 event.tenant_id
                if sub_tenant not in ("*", event_tenant):
                    continue
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

    def subscribe(self, topic: str, since_seq: int = 0,
                 *, tenant_id: str | None = None) -> asyncio.Queue:
        """订阅 — 返回 asyncio.Queue, 调用方 await q.get().

        T65.8: tenant_id 过滤 — 不同 tenant 的事件不会交叉投递.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        # tenant_id 存在时, 拼成 key 隔离
        sub_key = f"{tenant_id or '*'}::{topic}"
        with self._subs_lock:
            self._subs.setdefault(sub_key, []).append(q)
        # 立即注入 last seen seq (让 caller 先 replay 完存量, 再处理 live)
        q._since_seq_hint = since_seq  # type: ignore[attr-defined]
        q._sub_key = sub_key  # type: ignore[attr-defined]
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        # T65.8: 用 _sub_key (含 tenant_id 前缀) 定位 — 兼容老 caller
        sub_key = getattr(q, "_sub_key", f"*::{topic}")
        with self._subs_lock:
            lst = self._subs.get(sub_key) or self._subs.get(topic, [])
            if q in lst:
                lst.remove(q)

    def replay(self, since_seq: int, topic: str | None = None, limit: int = 500,
               *, tenant_id: str | None = None, persistent_only: bool = False) -> list[dict[str, Any]]:
        """同步读取历史事件 — SSE Last-Event-ID 重连续传用.

        T65.8: tenant_id 过滤 (跨租户不串); persistent_only=True 跳过 page.*
        """
        if self._conn is None:
            return []
        # Build dynamic WHERE
        where = ["seq > ?"]
        params: list[Any] = [since_seq]
        if topic:
            where.append("topic = ?")
            params.append(topic)
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        if persistent_only:
            where.append("persistent = 1")
        sql = (
            "SELECT seq, event_id, ts, topic, scope, scope_id, tenant_id, "
            "producer_kind, producer_id, provenance, dedup_key, persistent, "
            "payload_json, expires_at FROM events WHERE " + " AND ".join(where) +
            " ORDER BY seq ASC LIMIT ?"
        )
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for (seq, eid, ts, topic_, scope, scope_id, tenant_id_,
             pk, pid, prov, ddk, persistent, body, exp_at) in rows:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"_raw": body}
            out.append({
                "event_id": eid, "seq": seq, "ts": ts, "topic": topic_,
                "scope": scope, "scope_id": scope_id, "tenant_id": tenant_id_,
                "producer": {"kind": pk, "id": pid},
                "provenance": prov, "dedup_key": ddk, "persistent": bool(persistent),
                "payload": payload, "expires_at": exp_at,
            })
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
