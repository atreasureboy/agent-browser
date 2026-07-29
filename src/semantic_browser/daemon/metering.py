"""Metering framework for LLM token and browser resource cost tracking.

§4: 计量系统 — LLM token + 浏览器资源成本归因到 run_id/session/agent/tenant.

两种接入模式:
- LLM Proxy (默认): 透明转发 + 从响应 usage 计量
- 上报模式: agent 直连 provider, 事后 POST /v1/usage-events

存储: SQLite metering.db (独立文件, 避免与 session 库锁竞争)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class UsageEvent:
    """单个计量事件."""
    event_id: str
    kind: str  # llm_usage | browser_seconds | bandwidth_bytes
    source: str  # proxy | reported | internal
    ts: float  # epoch seconds
    tenant_id: str = ""
    agent_id: str = ""
    run_id: str = ""
    session_id: str = ""
    # LLM 字段
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # 资源字段
    metric: str = ""
    quantity: float = 0.0
    # 成本
    cost_micro_usd: int = 0  # 微美元 (整数, 避免浮点)
    estimated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "source": self.source,
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "metric": self.metric,
            "quantity": self.quantity,
            "cost_micro_usd": self.cost_micro_usd,
            "estimated": self.estimated,
        }


class MeteringStore:
    """SQLite 存储后端."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ts REAL NOT NULL,
                    tenant_id TEXT,
                    agent_id TEXT,
                    run_id TEXT,
                    session_id TEXT,
                    provider TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    metric TEXT,
                    quantity REAL,
                    cost_micro_usd INTEGER,
                    estimated INTEGER,
                    ingested_at REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts
                ON usage_events(tenant_id, ts)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_run
                ON usage_events(run_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rollups (
                    scope TEXT NOT NULL,  -- run | agent | tenant
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    window_start INTEGER NOT NULL,  -- epoch seconds (hourly)
                    total_tokens INTEGER,
                    total_cost_micro_usd INTEGER,
                    event_count INTEGER,
                    PRIMARY KEY (scope, scope_id, kind, window_start)
                )
            """)

    def ingest(self, event: UsageEvent) -> None:
        """写入单个事件."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO usage_events (
                        event_id, kind, source, ts, tenant_id, agent_id, run_id, session_id,
                        provider, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                        metric, quantity, cost_micro_usd, estimated, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.event_id, event.kind, event.source, event.ts,
                    event.tenant_id, event.agent_id, event.run_id, event.session_id,
                    event.provider, event.model,
                    event.input_tokens, event.output_tokens,
                    event.cache_read_tokens, event.cache_write_tokens,
                    event.metric, event.quantity, event.cost_micro_usd,
                    1 if event.estimated else 0,
                    time.time(),
                ))

    def ingest_batch(self, events: list[UsageEvent]) -> int:
        """批量写入, 返回成功写入数量 (去重后)."""
        if not events:
            return 0
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                rows = [
                    (e.event_id, e.kind, e.source, e.ts,
                     e.tenant_id, e.agent_id, e.run_id, e.session_id,
                     e.provider, e.model,
                     e.input_tokens, e.output_tokens,
                     e.cache_read_tokens, e.cache_write_tokens,
                     e.metric, e.quantity, e.cost_micro_usd,
                     1 if e.estimated else 0,
                     time.time())
                    for e in events
                ]
                conn.executemany("""
                    INSERT OR IGNORE INTO usage_events (
                        event_id, kind, source, ts, tenant_id, agent_id, run_id, session_id,
                        provider, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                        metric, quantity, cost_micro_usd, estimated, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
                return conn.total_changes

    def get_usage_by_run(self, run_id: str) -> dict[str, Any]:
        """查询某 run 的累计用量."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT kind, SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost_micro_usd) as total_cost_micro_usd,
                       COUNT(*) as event_count
                FROM usage_events
                WHERE run_id = ?
                GROUP BY kind
            """, (run_id,)).fetchall()
            return {row["kind"]: {
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "total_cost_micro_usd": row["total_cost_micro_usd"] or 0,
                "event_count": row["event_count"],
            } for row in rows}

    def get_usage_by_tenant(self, tenant_id: str, since_ts: float = 0) -> dict[str, Any]:
        """查询某 tenant 的累计用量."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT kind, SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost_micro_usd) as total_cost_micro_usd,
                       COUNT(*) as event_count
                FROM usage_events
                WHERE tenant_id = ? AND ts >= ?
                GROUP BY kind
            """, (tenant_id, since_ts)).fetchall()
            return {row["kind"]: {
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "total_cost_micro_usd": row["total_cost_micro_usd"] or 0,
                "event_count": row["event_count"],
            } for row in rows}


class PriceTable:
    """价格表 — 根据 model 计算成本."""

    # 默认价格 (微美元 / 1K tokens)
    DEFAULT_PRICES = {
        "gpt-4": {"input": 30000, "output": 60000},
        "gpt-4-turbo": {"input": 10000, "output": 30000},
        "gpt-3.5-turbo": {"input": 500, "output": 1500},
        "claude-3-opus": {"input": 15000, "output": 75000},
        "claude-3-sonnet": {"input": 3000, "output": 15000},
        "claude-3-haiku": {"input": 250, "output": 1250},
        "deepseek-chat": {"input": 140, "output": 280},
        "MiniMax-M3": {"input": 100, "output": 500},
        "qwen-plus": {"input": 400, "output": 1200},
        "gemini-pro": {"input": 500, "output": 1500},
    }

    def __init__(self, custom_prices: dict[str, dict[str, int]] | None = None):
        self.prices = {**self.DEFAULT_PRICES, **(custom_prices or {})}

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int,
                       cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> tuple[int, bool]:
        """计算成本 (微美元). 返回 (cost_micro_usd, estimated)."""
        if model in self.prices:
            price = self.prices[model]
            input_cost = (input_tokens * price["input"]) // 1000
            output_cost = (output_tokens * price["output"]) // 1000
            # Cache reads 通常 1/10 价格
            cache_cost = (cache_read_tokens * price["input"]) // 10000
            return input_cost + output_cost + cache_cost, False
        else:
            # 未知 model: 用最贵价格 * 1.25, 标记 estimated
            max_input = max(p["input"] for p in self.prices.values())
            max_output = max(p["output"] for p in self.prices.values())
            cost = int((input_tokens * max_input + output_tokens * max_output) * 1.25) // 1000
            logger.warning("Unknown model %s, using estimated cost (max * 1.25)", model)
            return cost, True
