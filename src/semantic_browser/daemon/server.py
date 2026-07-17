"""Local Transparent Browser daemon.

A small stdlib HTTP daemon that owns one persistent browser instance. CLI/MCP
adapters should talk to this daemon instead of launching their own browsers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from semantic_browser.result import classify_exception, err, ok
from urllib.parse import parse_qs, urlparse

from semantic_browser.engine import SemanticBrowser
from semantic_browser.snapshot.engine import SnapshotEngine
from semantic_browser.browser.controller import BrowserConfig, BrowserController
from semantic_browser.browser.pool import ControllerPool
from semantic_browser.memory.store import MemoryStore
from semantic_browser.graph.builder import GraphBuilder
from semantic_browser.daemon.snapshots import SnapshotStore
from semantic_browser.event_bus import EventBus

logger = logging.getLogger(__name__)

def _pid_path(port: int) -> Path:
    """每个 daemon 端口一个 PID 文件. 每次读 HOME env (而不是模块级常量),
    让测试能改 HOME 隔离状态."""
    return Path.home() / ".semantic-browser" / f"daemon-{port}.pid"


def _pid_alive(pid: int) -> bool:
    """进程是否还活着. signal 0 不发信号, 只检查权限/存在."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在, 但属于其他用户 (例如 root 起的 daemon)
        return True


def _read_pid_file(path: Path) -> tuple[int, str] | None:
    """读 PID 文件, 解析 'pid\\nhost\\n' 格式. 失败返回 None."""
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, OSError):
        return None
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    host = lines[1].strip() if len(lines) > 1 else ""
    return (pid, host)


class _AsyncOwner:
    """Runs one asyncio loop in a background thread for browser operations.

    T54: 持有 ControllerPool (共享 chromium + 多 BrowserContext). 每个 session
    是一个独立的 BrowserController, 通过 name 区分.
    T65.6: 加 tenant/agent 元数据 — 多 agent 共享 daemon 时按 tenant 隔离 session,
    每个 session 记 tenant_id + agent_id (默认 "anonymous").
    T66.6.1: _session_meta 跨重启保留 — 从 lease_manager.sessions_index 预热;
            set_session_meta 镜像写回 sessions_index.
    """

    DEFAULT_SESSION = "default"
    DEFAULT_TENANT = "anonymous"
    DEFAULT_AGENT = "anonymous"

    class _BrowserShim:
        """Backward-compat: 让 owner.browser.controller.X() 还能工作."""

        def __init__(self, controller: BrowserController) -> None:
            self.controller = controller

    def __init__(self, headless: bool = True, storage_state_path: str | None = None,
                 k_contexts: int = 16) -> None:
        import threading as _threading
        self.loop = asyncio.new_event_loop()
        self.config = BrowserConfig(
            headless=headless,
            storage_state_path=os.path.expanduser(storage_state_path) if storage_state_path else None,
        )
        # T54: 共享 chromium 进程 + 多 BrowserContext
        # T65.5: 用 daemon 的 k_contexts (默认 16) — 之前硬编码 20 是 K=20 时代的遗留.
        self.pool = ControllerPool(self.config, max_contexts=k_contexts)
        self.thread = threading.Thread(target=self._run_loop, name="tb-daemon-loop", daemon=True)
        self.thread.start()
        # T51: 浏览器操作串行化锁 (放在 owner 上, _acquire_op_lock_or_503 直接拿)
        self.op_lock = _threading.Lock()
        # T65.1: per-session last_used 跟踪 — idle recycle 用
        self._session_last_used: dict[str, float] = {}
        # T65.6: tenant/agent 元数据 — 每 session 记归属, 按 tenant 过滤 session 列表.
        # T66.6.1: 跨重启保留 — daemon init 时从 lease_manager.sessions_index 预热,
        #          set_session_meta 镜像写回 (sessions_index 是 source of truth).
        # owner.lease_manager 由 daemon init 后置 (顺序: owner → lease_manager.start() → 预热)
        self.lease_manager = None
        self._session_meta: dict[str, dict[str, Any]] = {}
        self.run(self.pool.start())
        # 预创建 default session — 保留 .browser 兼容旧代码
        default_ctrl = self.run(self.pool.acquire(self.DEFAULT_SESSION))
        self._session_last_used[self.DEFAULT_SESSION] = time.time()  # T65.1
        # T65.6: default session 归 default tenant + agent
        self._session_meta[self.DEFAULT_SESSION] = {
            "tenant_id": self.DEFAULT_TENANT,
            "agent_id": self.DEFAULT_AGENT,
            "created_at": time.time(),
        }
        self.browser = self._BrowserShim(default_ctrl)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=120)

    def close(self) -> None:
        try:
            self.run(self.pool.close())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

    def get_controller(self, name: str | None = None) -> BrowserController:
        """T54: 拿指定 session 的 controller, 懒创建 (同步 — 给 HTTP handler thread 用).

        注意: 已经在 event loop 上的 coroutine 不能调这个, 会 deadlock.
        那种情况直接 await self.pool.acquire(name).
        T65.1: touch session (更新 last_used_at) 用于 idle 回收.
        """
        name = name or self.DEFAULT_SESSION
        ctrl = self.run(self.pool.acquire(name))
        self._session_last_used[name] = time.time()
        return ctrl

    async def aget_controller(self, name: str | None = None) -> BrowserController:
        """T54: async 版本 — 给已经在 event loop 上的 coroutine 用 (不会 deadlock).
        T65.1: touch session."""
        name = name or self.DEFAULT_SESSION
        ctrl = await self.pool.acquire(name)
        self._session_last_used[name] = time.time()
        return ctrl

    def get_idle_sessions(self, idle_timeout_s: float) -> list[str]:
        """T65.1: 返回所有 idle 超过 idle_timeout_s 秒的 session 列表
        (排除 default session — 不能被回收)."""
        now = time.time()
        return [
            n for n, ts in self._session_last_used.items()
            if n != self.DEFAULT_SESSION and (now - ts) >= idle_timeout_s
        ]

    def touch_session(self, name: str) -> None:
        """T65.1: 显式 touch — 路由 handler 在每个 op 开始/结束时调, 不依赖 aget."""
        self._session_last_used[name] = time.time()

    def run_coro(self, coro):
        """T55: 在 daemon event loop 上跑一个 coroutine (从 daemon 主线程调用)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=60)

    def list_sessions(self) -> list[str]:
        """T54: 列出所有活跃 session 名."""
        return self.pool.list_active()

    def list_sessions_for_tenant(self, tenant_id: str) -> list[str]:
        """T65.6: 按 tenant_id 过滤 — 返回该 tenant 下的所有 session 名."""
        return [
            n for n, meta in self._session_meta.items()
            if meta["tenant_id"] == tenant_id
        ]

    def get_session_meta(self, name: str) -> dict[str, Any] | None:
        """T65.6: 取 session 元数据 (tenant_id / agent_id / created_at)."""
        return self._session_meta.get(name)

    def set_session_meta(self, name: str, *, tenant_id: str, agent_id: str) -> None:
        """T65.6: 给 session 写元数据 (POST /sessions 时调).

        T66.6.1: 镜像写到 lease_manager.sessions_index — 跨重启保留.
        """
        prev = self._session_meta.get(name, {})
        meta = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "created_at": prev.get("created_at", time.time()),
        }
        self._session_meta[name] = meta
        # 持久化到 sessions_index — 失败不抛 (内存写已生效)
        if self.lease_manager is not None:
            try:
                self.lease_manager.upsert_session_meta(
                    name, tenant_id, agent_id,
                    created_at_ms=int(meta["created_at"] * 1000),
                )
            except Exception:
                logger.exception("upsert_session_meta(%s) failed (non-fatal)", name)

    def release_session(self, name: str) -> bool:
        """T54: 关闭并移除指定 session. 返回是否真释放了一个.
        T65.1: 同时清掉 _session_last_used 跟踪.
        T65.6: 同时清掉 _session_meta.

        给 HTTP handler (跨线程) 用 — 内部调 self.run() 把协程扔到 loop 线程.
        T65.1 修: 不能在 event loop 线程上直接调这个, 会 deadlock (loop 等
        fut.result, fut 等 loop 处理). 在 loop 线程上请用 arelease_session().
        """
        if name == self.DEFAULT_SESSION:
            return False  # default 不能释放

        async def _release() -> bool:
            async with self.pool._lock:
                return self.pool._controllers.pop(name, None) is not None

        try:
            ok = self.run(_release())
            if ok:
                self._session_last_used.pop(name, None)
                self._session_meta.pop(name, None)
            return ok
        except Exception:
            return False

    async def arelease_session(self, name: str) -> bool:
        """T65.1: async 版 release — 给已经在 event loop 上的代码用
        (e.g. _sweep_idle_sessions). 不会 deadlock."""
        if name == self.DEFAULT_SESSION:
            return False
        try:
            async with self.pool._lock:
                ok = self.pool._controllers.pop(name, None) is not None
            if ok:
                self._session_last_used.pop(name, None)
                self._session_meta.pop(name, None)
            return ok
        except Exception:
            return False


# T51: 串行化所有 controller 操作, 避免多 HTTP 线程并发改 page state.
# 注意: 浏览器单实例多线程不安全, controller 的 _page / current_page 是共享可变状态.
# asyncio loop 自己单线程串行执行 coroutine, 但 await 切点之间会交错,
# 多个 HTTP 请求都调 controller.open() 会同时 await page.goto(), 互相覆盖.
_OP_LOCK_TIMEOUT_S = 30.0  # 等锁超过 30s → 503 错; 长任务应主动拆小


def _new_request_id() -> str:
    """T70.9: 生成唯一 request_id — 让 agent 能追踪多请求关联."""
    return uuid.uuid4().hex[:16]  # 16-char hex, 紧凑够排重


# T65.5: 容量公式常量 (设计文档 §1.2) — 64GB 单机推荐 M=6 / K=16.
# 公式: mem_per_browser = BASE + K × (CTX + P̄ × PAGE)
#       mem_total = M × mem_per_browser + DAEMON + OS_RESERVE
# K=16 是 hard limit (评审 D7) — 早期草稿 K=20 已作废.
_M_BASE_MB = 250      # Chromium browser+GPU+utility 基底 (headless-new 实测 180-280MB)
_M_CTX_MB = 15        # 空 BrowserContext (cookie jar / cache 索引 / storage 分区)
_M_PAGE_MB = 120      # 每活跃 page 的 renderer (现代 SPA RSS 中位 90-150MB, 保守中值)
_M_PAGES_AVG = 1.5    # 每 context 平均活跃 page 数 (主页面 + 偶发 popup)
_M_DAEMON_MB = 300    # daemon 进程自身 (Python + FastAPI + Playwright client)
_M_OS_RESERVE_MB = 2048  # OS + 文件缓存 + 突发预留 (Linux 64GB 机通常 idle 1.5-2GB)


class _DaemonBusy(Exception):
    """T51: 另一个 op 还占用浏览器 — 等锁超时."""

    def __init__(self, waited: float):
        self.waited = waited
        super().__init__(
            f"another operation still running (waited {waited:.1f}s); "
            f"check /queue or retry"
        )


def _acquire_op_lock_or_503(owner: "_AsyncOwner"):
    """T51: 上下文管理器 — 拿到 lock 或 raise _DaemonBusy.

    用法:
        with _acquire_op_lock_or_503(owner):
            result = owner.run(...)
    """
    import contextlib
    lock = owner.op_lock

    @contextlib.contextmanager
    def _ctx():
        if not lock.acquire(timeout=_OP_LOCK_TIMEOUT_S):
            raise _DaemonBusy(waited=_OP_LOCK_TIMEOUT_S)
        try:
            yield
        finally:
            lock.release()

    return _ctx()


# T48: error.code → HTTP status 映射
_STATUS_BY_CODE: dict[str, int] = {
    "NOT_IMPLEMENTED": 501,
    "MISSING_PARAM": 400,
    "INVALID_URL": 400,
    "NETWORK_FAIL": 502,
    "PAGE_NOT_OPENED": 409,
    "DAEMON_BUSY": 503,
    # T54: session CRUD 错误码
    "SESSION_NOT_FOUND": 404,
    "CANNOT_DELETE_DEFAULT": 400,
    "SESSION_CREATE_FAILED": 503,
    # T66.3: storage_state 快照查询 — 没有快照 404
    "SNAPSHOT_NOT_FOUND": 404,
    # T56: 降级错误码 (fable §5.9)
    "CAPACITY_DEGRADED": 503,    # L1: 拒新 session
    "DEGRADED_READONLY": 503,    # L3: 只读
    "SERVICE_UNAVAILABLE": 503,  # L4: 全拒
    # T58: SSRF blocked — fable §7.1 (URL 命中私网/meta)
    "SSRF_BLOCKED": 400,
    # T62: 收到 SIGTERM, daemon 进入 drain — 拒新 op, 等在飞完成
    "DAEMON_DRAINING": 503,
    # T65.2: ?strict=true 模式下 LLM 失败返 503 (retryable) — 默认 silent fallback 维持
    "LLM_UNAVAILABLE": 503,
    # T65.7: Lease/Fence — BUSY 409 (有 holder 在用), FENCE_MISMATCH 409 (旧 token)
    "BUSY": 409,
    "BUSY_LOWER_PRIORITY": 409,
    "FENCE_MISMATCH": 409,
    "LEASE_INVALID": 404,
    "LEASE_LOST": 409,
    "INTERNAL": 500,
}


class _SessionError(Exception):
    """T54: session 操作失败的业务异常 — 带 code 用于 HTTP 状态映射."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _LeaseError(Exception):
    """T65.7: Lease 操作失败的业务异常 — 带 code + optional holder info."""

    def __init__(self, code: str, message: str, *,
                 holder: dict[str, Any] | None = None,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.holder = holder
        self.status_code = status_code  # 显式 status override; None 时从 _STATUS_BY_CODE 取


class _LLMUnavailableError(Exception):
    """T65.2: ?strict=true 模式下 LLM proxy 抛错, 不再 silent fallback,
    直接抛此异常给 agent 显式处理. retryable=True."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "LLM_UNAVAILABLE"


class _DegradationError(Exception):
    """T56: 降级阻挡的业务异常 — 当 daemon 处于降级状态时拒绝."""

    def __init__(self, code: str, message: str, level: int) -> None:
        super().__init__(message)
        self.code = code
        self.level = level


class _DrainError(Exception):
    """T62: daemon 在 drain 状态, 拒新 op — 让 agent 重试或换节点.

    比 _DegradationError 简洁: 没有 level, 只有一个 reason.
    """

    def __init__(self, message: str, retry_after_s: int = 5) -> None:
        super().__init__(message)
        self.code = "DAEMON_DRAINING"
        self.retry_after_s = retry_after_s


# T52: 轻量 metrics registry — 不引 prometheus_client, 手写一个足够
class _MetricsRegistry:
    """请求级 metrics — 计数 + 直方图 (固定 buckets).

    Prometheus 文本格式输出. 在 _handle 钩子里采集, /metrics 暴露.
    线程安全 (一个 daemon 多 HTTP 线程).
    """

    _LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

    def __init__(self) -> None:
        import threading as _threading
        self._lock = _threading.Lock()
        # {label_key: count} — e.g. ("GET", "/open", "200") → N
        self._counters: dict[tuple[str, str, str], int] = {}
        # {label_key: {"count": N, "sum": S, "buckets": [累加]}}
        self._histograms: dict[tuple[str, str], dict[str, Any]] = {}

    def _labels(self, labels: dict[str, str]) -> str:
        return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))

    def inc(self, name: str, labels: dict[str, str], value: int = 1) -> None:
        key = tuple(sorted(labels.items()))
        full_key = (name, key)
        with self._lock:
            self._counters[full_key] = self._counters.get(full_key, 0) + value

    def observe(self, name: str, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        full_key = (name, key)
        with self._lock:
            h = self._histograms.get(full_key)
            if h is None:
                h = {
                    "count": 0,
                    "sum": 0.0,
                    "buckets": [0] * len(self._LATENCY_BUCKETS),
                }
                self._histograms[full_key] = h
            h["count"] += 1
            h["sum"] += value
            for i, b in enumerate(self._LATENCY_BUCKETS):
                if value <= b:
                    h["buckets"][i] += 1

    def render_prometheus(self) -> str:
        """Prometheus 文本格式 (0.0.4). 例:
          tb_requests_total{method="GET",path="/open",status="200"} 42
          tb_request_duration_seconds_bucket{method="GET",path="/open",le="0.5"} 38
        """
        lines: list[str] = []
        with self._lock:
            # counters — group by metric name
            counter_names = sorted({name for (name, _) in self._counters})
            for name in counter_names:
                lines.append(f"# TYPE tb_{name} counter")
                for (n, labels), value in sorted(self._counters.items()):
                    if n != name:
                        continue
                    label_str = self._labels(dict(labels))
                    lines.append(f"tb_{n}_total{{{label_str}}} {value}")
            # histograms
            hist_names = sorted({name for (name, _) in self._histograms})
            for name in hist_names:
                lines.append(f"# TYPE tb_{name} histogram")
                for (n, labels), h in sorted(self._histograms.items()):
                    if n != name:
                        continue
                    label_str = self._labels(dict(labels))
                    # bucket 行 — Prometheus 要求 bucket 累加 (le)
                    running = 0
                    for i, b in enumerate(self._LATENCY_BUCKETS):
                        running = h["buckets"][i]  # already cumulative because observe 写累加
                        # 注: 上面 observe 是对每个请求, 在所有 <= b 的 bucket 各 +1; 已经是累加
                        lines.append(
                            f'tb_{n}_bucket{{{label_str},le="{b}"}} {running}'
                        )
                    lines.append(f'tb_{n}_bucket{{{label_str},le="+Inf"}} {h["count"]}')
                    lines.append(f'tb_{n}_count{{{label_str}}} {h["count"]}')
                    lines.append(f'tb_{n}_sum{{{label_str}}} {h["sum"]:.6f}')
        return "\n".join(lines) + "\n"


def _make_handler(daemon: "TransparentBrowserDaemon"):
    """Build BaseHTTPRequestHandler subclass bound to a daemon instance.

    T48: extracted as module-level factory so tests can construct a daemon
    and inspect / hit the handler without subprocess.
    """
    class Handler(BaseHTTPRequestHandler):
        server_version = "TransparentBrowser/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug(fmt, *args)

        def do_GET(self) -> None:
            daemon._handle(self, "GET")

        def do_POST(self) -> None:
            daemon._handle(self, "POST")

        def do_DELETE(self) -> None:
            daemon._handle(self, "DELETE")

    return Handler


class TransparentBrowserDaemon:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, headless: bool = True, storage_state_path: str | None = None, event_bus_path: str | None = None, snapshots_root: str | None = None, snapshots_db: str | None = None, ssrf_allowlist: frozenset[str] | None = None, allow_data_scheme: bool = False, m_browsers: int = 6, k_contexts: int = 16, watchdog_interval_s: float = 5.0, sweep_interval_s: float = 60.0, session_idle_timeout_s: float | None = None, drain_timeout_s: float = 30.0, leases_db: str | None = None, lease_heartbeat_ttl_s: float = 15.0, memory_db: str | None = None, query_cache_path: str | None = None, query_concurrency: int = 4) -> None:
        import time as _time
        self.host = host
        self.port = port
        self.started_at = _time.time()
        self.owner = _AsyncOwner(headless=headless, storage_state_path=storage_state_path,
                              k_contexts=k_contexts)
        # T55: 持久化 Event Bus — SSE Last-Event-ID 续传 + 跨 SSE 状态共享
        self.event_bus = EventBus(event_bus_path)
        self.owner.run_coro(self.event_bus.start())
        # T68+ T69: shared SemanticQuery 实例 (跨请求共享 cache / llm / 状态)
        # 多 agent 共享 daemon 时, 同 query 命中 cache 能跨请求 + 跨重启
        from semantic_browser.query import SemanticQuery
        cache_path = query_cache_path or str(Path.home() / ".semantic-browser" / "query_cache.json")
        try:
            self._semantic_query = SemanticQuery(cache_persist_path=cache_path)
            self._query_cache_path = cache_path
        except Exception as e:
            logger.warning("failed to init shared SemanticQuery: %s; /v1/query will create per-request instances", e)
            self._semantic_query = None
            self._query_cache_path = None
        # T69: 并发限制 — 同一时刻最大 N 个 query 在跑 (避免浏览器/内存过载)
        import asyncio as _asyncio
        self._query_semaphore = _asyncio.Semaphore(query_concurrency)
        # T76: 滑动窗口 query log (audit/debug/metrics) — 最近 100 条
        from collections import deque
        self._query_log = deque(maxlen=100)
        self._query_log_lock = _asyncio.Lock()
        # T65.7: Lease/Fence — 多 agent 共享 daemon 所有权原语 (设计 §2)
        from semantic_browser.daemon.lease import LeaseManager
        self.lease_manager = LeaseManager(leases_db, heartbeat_ttl_s=lease_heartbeat_ttl_s)
        self.lease_manager.start()
        # T66.6.1: 把 lease_manager 传给 owner — _AsyncOwner.__init__ 用它预热
        # _session_meta (跨重启保留 tenant/agent 元数据).
        self.owner.lease_manager = self.lease_manager
        # 触发预热 (owner 已经 init 过了, 这里手动重跑一次)
        try:
            for sid, tid, aid, _ft, cats_ms in self.lease_manager.list_session_meta():
                self.owner._session_meta.setdefault(sid, {
                    "tenant_id": tid,
                    "agent_id": aid,
                    "created_at": (cats_ms / 1000.0) if cats_ms else time.time(),
                })
        except Exception:
            logger.exception("post-init load _session_meta failed")
        self.httpd: ThreadingHTTPServer | None = None
        self._shutting_down = False
        # T51: op 跟踪 — 浏览器锁在 owner.op_lock 上
        self._current_op: str | None = None
        self._op_started_at: float | None = None
        self._op_waiters: int = 0
        # T52: metrics
        self.metrics = _MetricsRegistry()
        # T56: 单进程内 DegradationController — 不经 Prometheus 回路 (fable §5.7)
        # 默认 L0 健康; 内存/CPU/loop_lag 触发时升级到 L3 只读, L4 全拒
        self._degradation_level = 0
        # /capacity 缓存 (定期刷新)
        self._capacity_max_contexts = k_contexts  # T60: K — 每实例 context 上限 (fable §1.2)
        # T60: M — 实例数 (当前 pool 共享 1 个 chromium 进程; 留作多 worker 扩展口)
        self._capacity_m_browsers = m_browsers
        # T60: 心跳 watchdog 间隔 (秒); 0 = 关闭
        self._watchdog_interval_s = watchdog_interval_s
        # T60: 心跳 / 健康状态 (每次 watchdog tick 更新; 0 表示 daemon 启动还没跑过 tick)
        self._last_heartbeat_ts: float | None = None
        # T60: 健康实例数 (实际活跃 controllers)
        self._healthy_browsers = 1  # 默认 1 — 启动后未崩就健康
        self._watchdog_task: asyncio.Task | None = None
        # T58: SSRF guardrail (fable §7.1) — 默认 deny, 可配 allowlist (测试 fixture / 内网工具)
        self._ssrf_allowlist: frozenset[str] = ssrf_allowlist or frozenset()
        # T58: 测试 fixture 用 data: URL 时通过此 flag 临时允许; production 必为 False
        self._allow_data_scheme: bool = allow_data_scheme
        # T66.8: SSRF 预解析缓存 — 命中 (host+port) 后直接跳过解析, 减少 lat.
        # 也堵住 DNS rebinding 路径 (解析一次锁住 IP).
        self._ssrf_cache: dict[str, tuple[str, frozenset[str]]] = {}
        # T59: SSE pressure events — 上次发布的压力等级 (None=未发布, 'normal'/'soft'/'high'/'critical')
        self._pressure_level: str | None = None
        # T61: storage_state 自动快照 (fable §5.4)
        self.snapshot_store = SnapshotStore(snapshots_root, snapshots_db)
        self._sweep_interval_s = sweep_interval_s
        self._sweep_task: asyncio.Task | None = None
        # MemoryStore — daemon 持久化浏览记忆 (pages/links/actions/notes).
        # 之前 daemon 只包了 BrowserController, _BrowserShim 没暴露 store,
        # 导致 /stats /note /notes /history /graph /find /extract-topic 全报
        # AttributeError '_BrowserShim has no attribute store|find|...'.
        # 这里复用 ~/.semantic-browser/memory.db (跟 SemanticBrowser engine 同库),
        # 同时挂到 owner.browser.store 让老的 self.owner.browser.store.X 调用通.
        _memory_db = memory_db or os.path.expanduser("~/.semantic-browser/memory.db")
        self.memory_store = MemoryStore(_memory_db)
        # daemon 级 session — 一次启动一个, 所有 op 共用 (跟 engine 行为一致).
        self.memory_session_id = f"daemon_{int(time.time())}"
        self.memory_store.start_session(self.memory_session_id)
        self.owner.browser.store = self.memory_store  # backward-compat
        # T65.1: session idle 自动回收 — 闲置超过 N 秒的 session 自动 close + 移除.
        # CLI --session-idle-timeout 优先; 否则读 env DAEMON_SESSION_IDLE_TIMEOUT_S;
        # 默认 300s 适合长 agent 会话. None 表示走 env/default.
        self._session_idle_timeout_s: float = (
            session_idle_timeout_s
            if session_idle_timeout_s is not None
            else float(os.environ.get("DAEMON_SESSION_IDLE_TIMEOUT_S", "300"))
        )
        # T62: graceful drain (fable §5.8)
        # SIGTERM/SIGINT 触发 _draining 标记, 拒新 op, 等在飞完成, 然后真退出.
        self._draining: bool = False
        self._drain_started_at: float | None = None
        self._drain_timeout_s: float = drain_timeout_s
        self._drain_event = threading.Event()  # 用于跨线程等 op_lock 释放
        # T63.2 (#3 修): LLM-augment 页面分类 — 启发式置信度低时 (e.g. example.com 这种
        # 简单 landing page) 跑 LLM 二次判断. lazy init, OPENAI_API_KEY 缺失时为 None,
        # 不影响现有启发式-only 路径. URL → 分类结果 缓存 (256 LRU) 避免重复 LLM call.
        self._llm_classifier: Any = None
        self._llm_classifier_lock = threading.Lock()
        self._classify_cache: dict[str, dict[str, Any]] = {}
        self._classify_cache_max = 256
        # T64: 运维观测 — LLM call 成功 / 失败计数, /capacity 暴露
        self._classify_llm_calls: int = 0
        self._classify_llm_failures: int = 0
        self._classify_cache_hits: int = 0

    def serve_forever(self) -> None:
        daemon = self
        self.httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(daemon))
        logger.warning("Transparent Browser daemon listening on http://%s:%d", self.host, self.port)
        # T60: 启 watchdog 心跳 (在 asyncio loop 上跑; 5s 一跳)
        self._start_watchdog()
        # T61: 启 storage_state 快照 sweeper (60s 扫 dirty)
        self._start_snapshot_sweeper()
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def _start_watchdog(self) -> None:
        """T60: 后台 heartbeat + 健康检查 (fable §5.5 / §5.7).

        每 watchdog_interval_s 跑一次:
          - 检查 op_lock 是否被卡 (>30s 没释放 → 发 browser.lock_stuck 警告)
          - 检查 owner 是否还活着 (browser 进程是否在)
          - 发 system.heartbeat 到 bus (暴露给 /events 订阅者, 监控可视化)

        T65.1 修: loop 在 owner 线程跑, _start_watchdog 是 HTTP server 线程调的,
        不能 loop.create_task() (跨线程 silently 被 loop 忽略). 必须
        asyncio.run_coroutine_threadsafe() 调度, 返 concurrent.futures.Future
        (cancel() 一样用).
        """
        if self._watchdog_interval_s <= 0:
            return
        loop = self.owner.loop

        async def _tick():
            while True:
                try:
                    await self._watchdog_once()
                except Exception:
                    logger.exception("watchdog tick failed")
                await asyncio.sleep(self._watchdog_interval_s)

        self._watchdog_task = asyncio.run_coroutine_threadsafe(_tick(), loop)

    async def _watchdog_once(self) -> None:
        """一次 tick — 发心跳 + 检测 op_lock 卡死."""
        self._last_heartbeat_ts = time.time()
        # 检测 1: op_lock 卡死 (被持 >30s)
        if self._op_started_at is not None:
            held_for = time.time() - self._op_started_at
            if held_for > 30.0:
                # 卡死 — 发警告 (每次 tick 都发, 噪声但 daemon 不该挂这么久)
                try:
                    self.event_bus.publish(
                        "browser.lock_stuck",
                        {"op": self._current_op, "held_seconds": round(held_for, 1),
                         "op_locked": self.owner.op_lock.locked(),
                         "ts": time.time()},
                    )
                except Exception:
                    logger.exception("failed to publish lock_stuck")
        # 检测 2: 实际活跃 browser instance
        try:
            sessions = self.owner.list_sessions()
            self._healthy_browsers = 1  # 单 pool 单 chromium — 始终视为健康
        except Exception:
            self._healthy_browsers = 0
            try:
                self.event_bus.publish(
                    "browser.crashed",
                    {"reason": "list_sessions_failed", "ts": time.time()},
                )
            except Exception:
                pass
        # 心跳发到 bus — /events 订阅者用来判断 daemon 还活着
        try:
            self.event_bus.publish(
                "system.heartbeat",
                {"pid": os.getpid(), "browsers_alive": self._healthy_browsers,
                 "M": self._capacity_m_browsers, "K": self._capacity_max_contexts,
                 "sessions_active": len(self.owner.list_sessions()),
                 "degradation_level": self._degradation_level,
                 "ts": self._last_heartbeat_ts},
            )
        except Exception:
            logger.exception("failed to publish heartbeat")

    def shutdown(self) -> None:
        """T49/T62: 优雅关闭 — 走完整 drain 流程: 标记 draining → 等在飞
        op 完成（或超时） → 真退出. 同步签名，调用方（信号 handler）拿到的
        是 drain 启动后的不等结果。实际等待由后台 drain 线程完成.
        """
        if self._shutting_down:
            return
        self._begin_drain()
        # 实际 close 在后台线程做 — 信号 handler 不能阻塞
        threading.Thread(target=self._finish_shutdown_after_drain,
                         name="tb-daemon-drain", daemon=True).start()

    def _begin_drain(self) -> None:
        """T62: 标记 daemon 开始 drain — 新 op 拿 503, /health 报 draining.

        idempotent: 多次调用安全.
        """
        if self._draining:
            return
        self._draining = True
        self._drain_started_at = time.time()
        self._shutting_down = True
        # 通知在飞的 op — 走 event bus, agent 订阅可见
        try:
            self.event_bus.publish(
                "daemon.draining",
                {"drain_timeout_s": self._drain_timeout_s,
                 "in_flight": self._current_op,
                 "ts": self._drain_started_at},
            )
        except Exception:
            logger.exception("drain: failed to publish daemon.draining")
        logger.warning("daemon draining (timeout=%ds, in_flight=%r)",
                       int(self._drain_timeout_s), self._current_op)

    def _finish_shutdown_after_drain(self) -> None:
        """T62: 实际关闭 — 等在飞 op 完成 (或超时) → 关 event loop/资源."""
        # 等当前 op (若在飞) 完成, 或 drain 超时
        if self._current_op is not None and self._op_started_at is not None:
            deadline = self._drain_started_at + self._drain_timeout_s if self._drain_started_at else time.time() + self._drain_timeout_s
            # 短间隔轮询 op_lock — 不抢锁, 只是等释放
            while self.owner.op_lock.locked() and time.time() < deadline:
                time.sleep(0.05)
            held_for = time.time() - (self._op_started_at or time.time())
            if self.owner.op_lock.locked():
                logger.warning(
                    "drain timeout: op %r still holding lock after %.1fs, forcing close",
                    self._current_op, held_for,
                )
                try:
                    self.event_bus.publish(
                        "daemon.drain_timeout",
                        {"op": self._current_op, "held_seconds": round(held_for, 1)},
                    )
                except Exception:
                    pass
            else:
                logger.info("drain: in-flight op completed after %.1fs", held_for)
        self._drain_event.set()  # 通知任何阻塞 wait 的线程
        self._finish_shutdown()

    def _finish_shutdown(self) -> None:
        """T62: 实际关闭 httpd / owner / snapshot store."""
        # T60: 停 watchdog 后台 task
        if self._watchdog_task is not None:
            try:
                self._watchdog_task.cancel()
            except Exception:
                pass
            self._watchdog_task = None
        # T61: 停 snapshot sweeper
        if self._sweep_task is not None:
            try:
                self._sweep_task.cancel()
            except Exception:
                pass
            self._sweep_task = None
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                logger.exception("Error stopping http server")
        try:
            self.owner.close()
        except Exception:
            logger.exception("Error closing browser owner")
        # T61: 关闭 SnapshotStore (关 sqlite)
        try:
            self.snapshot_store.close()
        except Exception:
            pass
        # T65.7: 关闭 LeaseManager (停 reaper + 关 sqlite)
        try:
            self.lease_manager.close()
        except Exception:
            pass
        # 删 PID 文件 (我们自己起的 daemon 才有)
        pid_file = _pid_path(self.port)
        try:
            if pid_file.exists() and pid_file.read_text().splitlines()[:1] == [str(os.getpid())]:
                pid_file.unlink()
        except OSError:
            pass

    # T51: 端点白名单 — 不需要 op_lock 的纯只读 / 元数据查询.
    # 其它端点 (open / click / snapshot / discover / etc.) 都串行化, 避免 controller 状态被覆盖.
    # T62: /admin/* 也放行 — 这些只翻 state flag (degrade/restore/drain),
    # 不摸 controller, 不该跟 /open /click 抢锁. 否则 /admin/drain 会
    # 排在所有 in-flight /open 后面, 等它跑起来 drain 标志才生效,
    # 期间继续到达的 /open 全被放行 (B 测试用例失败原因).
    _NO_LOCK_PATHS = frozenset({
        "/health", "/queue", "/stats", "/capacity", "/metrics", "/events",
        "/admin/drain", "/admin/drain/cancel", "/admin/degrade", "/admin/restore",
        "/v1/query/log", "/v1/query/cache/clear", "/v1/query/stats",  # T82: query-related 用 semaphore
    })

    # T56: 降级检查触发点 — 写 op 在 L3+ 被拒, 全 op 在 L4 被拒
    _WRITE_OPS = frozenset({
        "/open", "/click", "/type", "/hover", "/dblclick", "/rightclick",
        "/drag", "/select-option", "/fill-form", "/set-files",
        "/scroll", "/press", "/download", "/back", "/forward", "/reload",
        "/agent/run", "/agent/run/stream", "/discover", "/discover/stream",
        "/v1/query",  # T67: semantic query 写 (起 browser + 多次 LLM 调用)
        "/v1/query/stream",  # T68: SSE stream 也算写 op (起 browser)
        "/v1/query/cache/clear",  # T69: cache write 也算 write op (modify in-memory state)
    })

    # T56: 降级时仍允许的只读/控制端点 (L4 全拒时除外)
    # T59: /events 也放行 (agent 仍要订阅降级状态)
    _DEGRADED_ALLOWED = frozenset({
        "/health", "/healthz", "/readyz", "/queue", "/stats", "/capacity",
        "/metrics", "/events",
        "/admin/drain", "/admin/drain/cancel", "/admin/degrade", "/admin/restore",
    })

    def _compute_mem_budget(self) -> tuple[int, int, int]:
        """T65.5: 内存预算 (设计文档 §1.2 公式).
        mem_per_browser = BASE + K × (CTX + P̄ × PAGE)
        mem_total       = M × mem_per_browser + DAEMON + OS_RESERVE
        mem_high_watermark = mem_total × 0.80  (评审 D6: 80% 触发准入队列快速失败)

        返回 (mem_per_browser_mb, mem_total_mb, mem_high_watermark_mb).
        在 16vCPU/64GB 单机上 M=6/K=16 默认 → per_browser ≈ 3.4GB,
        total ≈ 22.5GB, high_watermark ≈ 18GB — 留 ~2.8× 余量给峰值/膨胀.
        """
        M = self._capacity_m_browsers
        K = self._capacity_max_contexts
        mem_per_browser = _M_BASE_MB + K * (_M_CTX_MB + _M_PAGES_AVG * _M_PAGE_MB)
        mem_total = M * mem_per_browser + _M_DAEMON_MB + _M_OS_RESERVE_MB
        mem_high = int(mem_total * 0.80)
        return int(mem_per_browser), int(mem_total), mem_high

    def _start_snapshot_sweeper(self) -> None:
        """T61: 后台 sweeper — 定时扫 dirty session, 抓 storage_state 快照.

        频率 (默认 60s) 满足 §5.4 RPO 上界. Sweep 走 daemon 自己的 event loop,
        调 SnapshotStore.take_snapshot (await controller._context.storage_state()).
        失败不重试, 只记 metrics + 发 system.snapshot.failed 到 bus.

        T65.1 修: 跟 _start_watchdog 同样的跨线程坑 — 必须 run_coroutine_threadsafe.
        """
        if self._sweep_interval_s <= 0:
            return
        loop = self.owner.loop

        async def _tick():
            while True:
                logger.info("sweep tick: starting (interval=%.1fs)", self._sweep_interval_s)
                try:
                    await self._sweep_snapshots_once()
                except Exception:
                    logger.exception("snapshot sweeper tick failed")
                # T65.1: 同一 tick 里也跑 idle 回收 — 不用再开后台 task
                try:
                    await self._sweep_idle_sessions()
                except Exception:
                    logger.exception("idle sweeper tick failed")
                logger.info("sweep tick: done")
                await asyncio.sleep(self._sweep_interval_s)

        self._sweep_task = asyncio.run_coroutine_threadsafe(_tick(), loop)

    async def _sweep_snapshots_once(self) -> None:
        """T61: 一次 sweep tick — 遍历所有 dirty session, 抓快照 + GC."""
        dirty = self.snapshot_store.dirty_sessions()
        if not dirty:
            return
        for sid in dirty:
            try:
                ctrl = await self.owner.aget_controller(sid)
                snap_id = await self.snapshot_store.take_snapshot(
                    sid, ctrl, trigger="auto_sweep",
                )
                if snap_id:
                    # T66.7.2 (C2): session-scoped 事件加 tenant_id —
                    # ops / 多 agent 共享时按 tenant 过滤事件流. 优先
                    # sessions_index (持久化, 跨重启), fallback in-memory.
                    self._publish_with_session_tenant(
                        "session.storage_state.saved",
                        {"session_id": sid, "snapshot_id": snap_id,
                         "trigger": "auto_sweep", "ts": time.time()},
                        session_id=sid,
                    )
                    # GC 旧快照 (留 3 份)
                    self.snapshot_store.gc_old_snapshots(sid)
            except Exception as e:
                logger.warning("sweep: snapshot failed for %s: %s", sid, e)
                self._publish_with_session_tenant(
                    "session.storage_state.failed",
                    {"session_id": sid, "reason": f"{type(e).__name__}: {e}",
                     "ts": time.time()},
                    session_id=sid,
                )

    def _sweep_idle_sessions(self) -> None:
        """T65.1: 一次 idle 回收 tick — 遍历所有 idle 超时的非 default session,
        调 release_session + 发 session.expired 到 EventBus.

        复用 snapshot sweeper 周期 (默认 60s) — 单独开 task 会增加无谓的 wakeup,
        且 idle 回收秒级精度无意义. 默认 300s timeout → 实际感知延迟 ≤ 360s.

        设计取舍: 不调 snapshot_store 抓 idle session 的 storage_state — 闲置的
        session 通常 agent 已结束工作, 强抓意义不大, 释放 BrowserContext 即可.
        agent 真要持久化应在 op 结束时显式 /admin/snapshot.

        T65.1 修: 必须 await owner.arelease_session (在 event loop 线程上),
        不能调 sync 的 release_session (内部 self.run → fut.result 死锁).
        因此整个方法变 async — 由 _tick() await.
        """
        if self._session_idle_timeout_s <= 0:
            return
        idle = self.owner.get_idle_sessions(self._session_idle_timeout_s)
        logger.info("idle recycle check: timeout=%.0fs, last_used=%s, idle=%s",
                    self._session_idle_timeout_s,
                    {n: int(time.time() - t) for n, t in self.owner._session_last_used.items()},
                    idle)
        return self._do_idle_recycle(idle)

    async def _do_idle_recycle(self, idle: list[str]) -> None:
        for sid in idle:
            try:
                ok = await self.owner.arelease_session(sid)
                logger.info("idle recycle: arelease_session(%s) returned %s",
                            sid, ok)
                if ok:
                    logger.info("idle recycle: released session %s (idle >= %.0fs)",
                                sid, self._session_idle_timeout_s)
                    # T66.7.2 (C2): session.expired 也加 tenant_id — ops 看
                    # "哪个 tenant 的 session 被回收" 必须能 filter. sweep 路径
                    # 跟 session.storage_state.saved 一样用 _publish_with_session_tenant.
                    self._publish_with_session_tenant(
                        "session.expired",
                        {"session_id": sid, "reason": "idle_timeout",
                         "timeout_s": self._session_idle_timeout_s,
                         "ts": time.time()},
                        session_id=sid,
                    )
            except Exception as e:
                logger.warning("idle recycle failed for %s: %s", sid, e)

    def _auto_degrade(self) -> None:
        """T56: 基于容量自动升降级 — 不经 Prometheus 回路 (fable §5.7).
        每请求调一次, 0ms 开销, 阈值用 capacity_ratio.
        只升不降 — 降级必须显式 /admin/restore, 防 admin bump 完被自动回落吃掉.

        T59: 同时发 SSE pressure 事件 (system.pressure + daemon.degraded) —
        agent 订阅 /events 主动避让, 不必每次轮询 /capacity.

        BUG-FIX: 之前用 if/elif — 同一次 ratio=0.95 进 if (升级 L0→L1) 后,
        elif 不会再评估, 永远卡在 L1. 改成 sequential if 让单次调用能连升.
        """
        n = len(self.owner.list_sessions())
        max_ = self._capacity_max_contexts
        ratio = n / max(max_, 1)
        # 升级到 L1 (拒新 session) — sequential if, 不是 if/elif (fable §5.7)
        if ratio >= 0.85 and self._degradation_level < 1:
            self._degradation_level = 1
            logger.warning("DegradationController: auto-bumped to L1 (capacity_ratio=%.2f)", ratio)
            self._emit_pressure_event("high", reason="auto_capacity", capacity_ratio=ratio)
        if ratio >= 0.95 and self._degradation_level < 2:
            self._degradation_level = 2
            logger.warning("DegradationController: auto-bumped to L2 (capacity_ratio=%.2f)", ratio)
            self._emit_pressure_event("critical", reason="auto_capacity", capacity_ratio=ratio)
        # 不再自动降 — admin/restore 显式降到 L0

    def _publish_with_session_tenant(self, topic: str, payload: dict[str, Any],
                                      *, session_id: str) -> None:
        """T66.7.2 (C2): 发 session-scoped 事件时自动带 tenant_id.

        优先 lease_manager.sessions_index (持久化, 跨重启保留), fallback 到
        in-memory meta. 最后兜底 DEFAULT_TENANT. dedup_key 自动加 tenant 前缀,
        防跨 tenant 串 dedup. producer_id 用 sessions_index 的 agent_id (若有).

        调用方不要自己拼 tenant_id, 用这个 helper 统一来源 — 跟 T66.6.3 B1 fix
        一致 (lease 路径直接从 lease 表读, 不用 body).
        """
        idx = self.lease_manager.get_session_meta(session_id)
        if idx is not None:
            tenant_id, agent_id = idx
        else:
            meta = self.owner.get_session_meta(session_id) or {}
            tenant_id = meta.get("tenant_id", _AsyncOwner.DEFAULT_TENANT)
            agent_id = meta.get("agent_id", _AsyncOwner.DEFAULT_AGENT)
        try:
            self.event_bus.publish(
                topic,
                payload,
                scope="session", scope_id=session_id,
                tenant_id=tenant_id,
                producer_kind="system", producer_id=agent_id,
                persistent=True,
            )
        except Exception:
            logger.exception("publish %s for session %s failed (non-fatal)",
                             topic, session_id)

    def _emit_pressure_event(self, level: str, *, reason: str,
                            capacity_ratio: float | None = None) -> None:
        """T59: 发 SSE pressure 事件 — system.pressure + daemon.degraded.

        只在 level 真变化时发 (避免满屏 spam). ratio 可选 (auto_capacity 时填,
        admin 显式时 None).

        三层 notification:
        - system.pressure{level: soft|high|critical}  — 通用 backpressure 信号
        - daemon.degraded{level, ratio}                — 显式降级事件
        """
        if level == self._pressure_level:
            return  # 没变化 — 不发
        prev = self._pressure_level
        self._pressure_level = level
        # capacity_ratio 计算 (若有)
        ratio = capacity_ratio
        if ratio is None:
            n = len(self.owner.list_sessions())
            ratio = round(n / max(self._capacity_max_contexts, 1), 3)
        try:
            self.event_bus.publish(
                "system.pressure",
                {"level": level, "prev": prev, "reason": reason,
                 "capacity_ratio": ratio, "ts": time.time()},
            )
            self.event_bus.publish(
                "daemon.degraded",
                {"level": self._degradation_level,
                 "label": ["L0_healthy", "L1_reject_new", "L2_preempt_low",
                           "L3_readonly", "L4_full"][self._degradation_level],
                 "pressure": level, "reason": reason,
                 "capacity_ratio": ratio, "ts": time.time()},
            )
        except Exception:
            logger.exception("failed to publish pressure event")

    def _enforce_degradation(self, method: str, path: str) -> None:
        """T56: 按当前 degradation level 拒绝不该走的请求.
        L1+ 拒新 session (POST /sessions) → CAPACITY_DEGRADED
        L3+ 拒所有写 op → DEGRADED_READONLY
        L4  拒除 /health 之外的全部 → SERVICE_UNAVAILABLE
        """
        level = self._degradation_level
        if level <= 0:
            return  # L0 全放行
        # L4: 仅 /health / /queue / /capacity / /metrics 仍可用
        if level >= 4:
            if path in self._DEGRADED_ALLOWED:
                return
            raise _DegradationError(
                "SERVICE_UNAVAILABLE",
                f"daemon at degradation L4 — refusing {method} {path} (only health/queue/capacity/metrics/admin available)",
                level,
            )
        # L3: 写 op 全拒
        if level >= 3 and path in self._WRITE_OPS:
            raise _DegradationError(
                "DEGRADED_READONLY",
                f"daemon at degradation L3 (readonly) — refusing write op {method} {path}",
                level,
            )
        # L1+: 拒新 session 创建 (其余放行)
        if level >= 1 and method == "POST" and path == "/sessions":
            raise _DegradationError(
                "CAPACITY_DEGRADED",
                f"daemon at degradation L{level} — refusing new session creation (capacity full)",
                level,
            )

    def _enforce_drain(self, method: str, path: str) -> None:
        """T62: drain 中 — 拒新 op. /health / /queue / /metrics 继续可用以便观测."""
        if not self._draining:
            return
        if path in self._DEGRADED_ALLOWED:
            return  # 仍让 agent 看 drain 状态
        elapsed = (time.time() - self._drain_started_at) if self._drain_started_at else 0.0
        raise _DrainError(
            f"daemon draining ({elapsed:.1f}s elapsed, timeout={int(self._drain_timeout_s)}s); "
            f"in_flight={self._current_op!r} — retry against a healthy daemon after drain completes",
            retry_after_s=5,
        )

    def _handle(self, req: BaseHTTPRequestHandler, method: str) -> None:
        import time as _time
        parsed = urlparse(req.path)
        path = parsed.path.rstrip("/") or "/"
        # T65.9: /v1/* namespace 共存 — 多 agent 走 /v1/, 老 dogfooding 走不带前缀.
        # v1 第一波只覆盖核心 8 个: healthz / capacity / events / sessions CRUD /
        # lease acquire+renew+release+get. 其余路由回退到不带 v1 的原始 handler.
        if path.startswith("/v1/"):
            v1_path = path[3:]  # strip "/v1" → 跟原 path 等价
            v1_routes = {
                "/healthz", "/readyz", "/capacity", "/events",
                "/sessions",  # POST 创建 + GET 列表
                "/query", "/query/stream", "/query/stats", "/query/cache/clear", "/query/log",  # T67+T68+T69+T76
            }
            # /v1/sessions/{id}/... (GET 详情 + DELETE + lease CRUD)
            if v1_path == "/healthz":
                path = "/healthz"
            elif v1_path == "/readyz":
                path = "/readyz"
            elif v1_path == "/capacity":
                path = "/capacity"
            elif v1_path == "/events":
                path = "/events"
            elif v1_path == "/sessions":
                path = "/sessions"
            elif v1_path == "/query":
                path = "/v1/query"  # 直路由 (不脱 v1 前缀, _dispatch 里识别)
            elif v1_path == "/query/stream":
                path = "/v1/query/stream"
            elif v1_path == "/query/stats":
                path = "/v1/query/stats"
            elif v1_path == "/query/log":
                path = "/v1/query/log"
            elif v1_path == "/query/cache/clear":
                path = "/v1/query/cache/clear"
            elif v1_path.startswith("/sessions/"):
                # 保留 lease 路径模式
                path = v1_path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        needs_lock = path not in self._NO_LOCK_PATHS
        # T82: /v1/query 类长操作不持 op_lock (用 _query_semaphore 限流) — 让多 agent 真并发
        #        op_lock 是粗粒度串行, 长 query 占着会让其他 op (read health/stats) 都 503
        if path in ("/v1/query", "/v1/query/stream"):
            needs_lock = False
        started_at = _time.time()
        final_status = 200  # 假设成功, 异常分支会改
        final_code = ""     # T52: 失败时的 error.code
        # T56: 自动升降级 — 不抛异常, 只改 _degradation_level
        self._auto_degrade()
        try:
            # T56: 降级阻挡 — 在拿 op_lock 前就拒 (L4 情况连锁都不该争)
            self._enforce_degradation(method, path)
            # T62: drain 阻挡 — 在拿 op_lock 前就拒 (在飞的让它跑完, 新的一律拒)
            self._enforce_drain(method, path)
            # T65.7: POST + DELETE 都可能带 body (lease release / preempt 等)
            body = self._read_json(req) if method in ("POST", "DELETE") else {}
            if needs_lock:
                lock_wait_start = _time.time()
                with _acquire_op_lock_or_503(self.owner):
                    lock_wait_s = _time.time() - lock_wait_start
                    self.metrics.observe("op_lock_wait", {"path": path}, lock_wait_s)
                    self._op_waiters_lock = getattr(self, "_op_waiters_lock", None)
                    self._op_waiters += 1
                    try:
                        self._current_op = f"{method} {path}"
                        self._op_started_at = _time.time()
                        op_hold_start = _time.time()
                        result = self._dispatch(method, path, {**query, **body}, req)
                        op_hold_s = _time.time() - op_hold_start
                        self.metrics.observe("op_lock_hold", {"path": path}, op_hold_s)
                    finally:
                        self._current_op = None
                        self._op_started_at = None
                        self._op_waiters -= 1
            else:
                result = self._dispatch(method, path, {**query, **body}, req)
            # T50: SSE 端点自己写了响应, _handle 不要再发
            if result == "_SSE_HANDLED":
                return
            # T52: /metrics 自己写了 text/plain 响应
            if result == "_RAW_HANDLED":
                return
            # T48: success envelope. None data means "no result found" — still ok.
            self._send(req, 200, ok(result))
        except Exception as e:
            # T51: 锁等不到 → 自定义错误码 + 503 + Retry-After
            if isinstance(e, _DaemonBusy):
                final_status = 503
                final_code = "DAEMON_BUSY"
                self._send(req, 503, {
                    "ok": False, "data": None,
                    "error": {"code": "DAEMON_BUSY", "message": str(e), "retryable": True},
                })
                return
            # T54: session 错误码走自定义异常, 不经 classify_exception
            if isinstance(e, _SessionError):
                final_status = _STATUS_BY_CODE.get(e.code, 500)
                final_code = e.code
                self._send(req, final_status, err(e.code, str(e), retryable=False))
                return
            # T65.7: Lease/Fence 错误 — 带 holder info 让 client 看到是谁占着
            if isinstance(e, _LeaseError):
                final_status = e.status_code or _STATUS_BY_CODE.get(e.code, 500)
                final_code = e.code
                err_body: dict[str, Any] = {
                    "code": e.code, "message": str(e), "retryable": False,
                }
                if e.holder:
                    err_body["holder"] = e.holder
                self._send(req, final_status, {"ok": False, "data": None,
                                               "error": err_body})
                return
            # T65.2: ?strict=true 模式下 LLM 失败 — 503 + Retry-After 让 agent
            # 知道是临时性故障, 可以 defer / 切到本地启发式
            if isinstance(e, _LLMUnavailableError):
                final_status = _STATUS_BY_CODE.get(e.code, 503)
                final_code = e.code
                self._send_with_extra_headers(req, final_status, {
                    "ok": False, "data": None,
                    "error": {
                        "code": e.code, "message": str(e),
                        "retryable": True, "strict": True,
                    },
                }, {"Retry-After": "5"})
                return
            # T56: 降级阻挡 — 503 + Retry-After (让 agent 知道要等/降级请求)
            if isinstance(e, _DegradationError):
                final_status = _STATUS_BY_CODE.get(e.code, 503)
                final_code = e.code
                # L4 / 写拒绝都给 Retry-After: 30s — agent 可以 defer
                self._send_with_extra_headers(req, final_status, {
                    "ok": False, "data": None,
                    "error": {
                        "code": e.code, "message": str(e),
                        "retryable": True, "level": e.level,
                    },
                }, {"Retry-After": "30"})
                return
            # T62: drain 阻挡 — 503 + Retry-After (agent 应换节点或挂起)
            if isinstance(e, _DrainError):
                final_status = _STATUS_BY_CODE.get(e.code, 503)
                final_code = e.code
                self._send_with_extra_headers(req, final_status, {
                    "ok": False, "data": None,
                    "error": {
                        "code": e.code, "message": str(e),
                        "retryable": True, "draining": True,
                    },
                }, {"Retry-After": str(e.retry_after_s)})
                return
            # T48: 统一走 classify_exception, 错误带 code / message / retryable
            classified = classify_exception(e)
            code = classified["error"]["code"]
            final_status = _STATUS_BY_CODE.get(code, 500)
            final_code = code
            if final_status >= 500:
                logger.exception("Request failed: %s %s", method, path)
            self._send(req, final_status, classified)
        finally:
            # T52: 记录 metrics — 即使 SSE / 异常也记录
            elapsed = _time.time() - started_at
            self.metrics.inc("requests", {
                "method": method, "path": path, "status": str(final_status),
            })
            if final_code:
                self.metrics.inc("errors", {
                    "method": method, "path": path, "code": final_code,
                })
            # duration 只记非 SSE (SSE 长流会扭曲直方图)
            if path not in ("/discover/stream", "/agent/run/stream", "/events"):
                self.metrics.observe("request_duration", {
                    "method": method, "path": path,
                }, elapsed)

    def _read_json(self, req: BaseHTTPRequestHandler) -> dict[str, Any]:
        """读 POST body — 兼容 JSON 和 form-urlencoded.

        T48/T65.7: 多数端点 (e.g. /sessions, /open) 走 JSON; 但 T65.7 lease 的
        fence_token 也常走 query string (?fence_token=5). 这里先试 JSON,
        失败再试 form-urlencoded, 都没有返 {}.
        """
        length = int(req.headers.get("content-length", "0") or "0")
        if length == 0:
            return {}
        raw = req.rfile.read(length).decode("utf-8")
        # Try JSON first
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: form-urlencoded (e.g. fence_token=5)
        try:
            parsed = parse_qs(raw, keep_blank_values=True)
            return {k: v[-1] for k, v in parsed.items()}
        except Exception:
            return {}

    def _send(self, req: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.send_response(status)
        req.send_header("content-type", "application/json; charset=utf-8")
        req.send_header("content-length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def _send_with_extra_headers(
        self, req: BaseHTTPRequestHandler, status: int,
        payload: dict[str, Any], extra_headers: dict[str, str],
    ) -> None:
        """T56: 额外加 header (Retry-After 等) — 在 end_headers 之前."""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.send_response(status)
        req.send_header("content-type", "application/json; charset=utf-8")
        req.send_header("content-length", str(len(data)))
        for k, v in extra_headers.items():
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(data)

    def _send_raw(self, req: BaseHTTPRequestHandler, status: int, body: str, content_type: str) -> None:
        """T52: 非 JSON 响应 (text/plain for /metrics)."""
        data = body.encode("utf-8")
        req.send_response(status)
        req.send_header("content-type", content_type)
        req.send_header("content-length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def _dispatch(self, method: str, path: str, args: dict[str, Any], req: BaseHTTPRequestHandler | None = None) -> Any:
        if method == "GET" and path == "/health":
            # T49: 健康检查带上下文 — pid / uptime / 当前页 URL, agent 排查时省一次 roundtrip
            # T62: drain 状态 — agent 看到 draining=true 时切到备份节点
            return self._handle_health_full()
        # T66.5: /healthz (liveness) — 只验 "进程在跑 + PID 有效", 永远 200.
        # k8s liveness probe 失败时由 kubelet 重启, 不该把 drain/L4 当失败.
        if method == "GET" and path == "/healthz":
            return {
                "alive": True,
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - self.started_at, 1),
            }
        # T66.5: /readyz (readiness) — "能接流量". 不满足 → 503 + Retry-After.
        if method == "GET" and path == "/readyz":
            return self._handle_readyz()
        # T51: 当前 op 队列状态 — agent 决定是否要等
        if method == "GET" and path == "/queue":
            import time as _time
            now = _time.time()
            return {
                "current_op": self._current_op,
                "running_for_s": round(now - self._op_started_at, 2) if self._op_started_at else None,
                "lock_held": self.owner.op_lock.locked(),
                "waiters": self._op_waiters,
                "lock_timeout_s": _OP_LOCK_TIMEOUT_S,
            }
        if method == "GET" and path == "/state":
            return self.owner.run(self._state(session=args.get("session")))
        # T56: /capacity — 容量 + 退路状态读出 (agent 决定是否要排队)
        # T60: M×K 容量模型 (fable §1.2)
        if method == "GET" and path == "/capacity":
            sessions = self.owner.list_sessions()
            M = self._capacity_m_browsers
            K = self._capacity_max_contexts
            slots_total = M * K
            # T65.5: 内存预算走设计文档 §1.2 公式 — 用常量替代硬编码数字, 让
            # 评审 D7 (K=16) + D11 (16GB 小机 M=4/K=8) 调整时一处改动即可.
            mem_per_browser_mb, mem_total_mb, mem_high_watermark_mb = self._compute_mem_budget()
            return {
                "sessions_active": len(sessions),
                "sessions_max": K,
                "capacity_ratio": round(len(sessions) / max(slots_total, 1), 3),
                "degradation_level": self._degradation_level,
                "degradation_label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][self._degradation_level],
                "pressure_level": self._pressure_level or "normal",
                # T60: M×K 容量字段 (fable §1.2) — M 是 browser 实例数,
                # K 是 per-browser 上限 session 数, slots_total = M*K.
                # agent 用 slots_total / capacity_ratio 算"还能开几个 session".
                "M": M,
                "K": K,
                "slots_total": slots_total,
                # T63.1: 去掉冗余 — browsers_count 跟 M 重复, last_heartbeat_ts
                # 跟 heartbeat_age_s 二选一 (留 age 字段, agent 不需绝对时间戳).
                "mem_per_browser_estimate_mb": mem_per_browser_mb,
                "mem_total_estimate_mb": mem_total_mb,
                # T65.5: 高水位线 — 超过后触发准入队列快速失败 (评审 D6)
                "mem_high_watermark_mb": mem_high_watermark_mb,
                "watchdog_heartbeat_age_s": round(time.time() - self._last_heartbeat_ts, 1)
                    if self._last_heartbeat_ts else None,
                # T64: LLM 分类计数器 + 缓存命中率 — 运维判断 LLM 是否健康.
                # failure_rate = failures / calls; 0/0 时是 None.
                "llm_classify_calls": self._classify_llm_calls,
                "llm_classify_failures": self._classify_llm_failures,
                "llm_classify_failure_rate": (
                    round(self._classify_llm_failures / max(self._classify_llm_calls, 1), 3)
                    if self._classify_llm_calls else None
                ),
                "classify_cache_size": len(self._classify_cache),
                "classify_cache_hits": self._classify_cache_hits,
                # T65.6: 按 tenant 分布 — 多 agent 共享 daemon 时给 ops 一眼
                # 看出每 tenant 用了多少 slot.
                "tenants": {
                    tid: sum(1 for m in self.owner._session_meta.values()
                             if m["tenant_id"] == tid)
                    for tid in {m["tenant_id"] for m in self.owner._session_meta.values()}
                },
            }
        # T59: /events — SSE stream of all EventBus events (持久 + live)
        if method == "GET" and path == "/events":
            if req is None:
                raise ValueError("/events requires req context")
            self._stream_events(req, args)
            return "_SSE_HANDLED"
        # T52: Prometheus metrics 端点 — 返回 text/plain, Prometheus 直接抓
        if method == "GET" and path == "/metrics":
            import time as _time
            body = self.metrics.render_prometheus()
            # 附加 daemon_uptime gauge (非 histogram/counter, 直接拼)
            uptime = _time.time() - self.started_at
            body += f"tb_daemon_uptime_seconds {uptime:.2f}\n"
            self._send_raw(req, 200, body, "text/plain; version=0.0.4; charset=utf-8")
            return "_RAW_HANDLED"
        # T56: admin 端点 — 显式 bump/restore 降级 (测试用, 也给运维用)
        if method == "POST" and path == "/admin/degrade":
            level = int(args.get("level", 1))
            if level < 1 or level > 4:
                raise ValueError(f"degradation level must be 1..4, got {level}")
            self._degradation_level = level
            logger.warning("DegradationController: admin set to L%d", level)
            # T59: admin 显式降级 → 发 pressure (按等级映射)
            pressure = {1: "high", 2: "high", 3: "critical", 4: "critical"}.get(level, "high")
            self._emit_pressure_event(pressure, reason=f"admin_degrade_L{level}")
            return {"level": level, "label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][level]}
        if method == "POST" and path == "/admin/restore":
            self._degradation_level = 0
            logger.info("DegradationController: admin restored to L0")
            # T59: 恢复 → 发 normal pressure (让 agent 解锁)
            self._emit_pressure_event("normal", reason="admin_restore")
            return {"level": 0, "label": "L0_healthy"}
        # T62: /admin/drain — 显式触发 drain (无需 SIGTERM, 给 ops/测试用).
        # 仅翻标志, 不退出进程; 真实 drain+exit 仍由 SIGTERM 走 shutdown().
        if method == "POST" and path == "/admin/drain":
            self._begin_drain()
            return {"draining": True, "in_flight": self._current_op,
                    "drain_started_at": self._drain_started_at,
                    "drain_timeout_s": self._drain_timeout_s}
        # T66.4: /admin/drain/cancel — 撤销 drain 标志, 让 daemon 恢复接流量.
        # 误触 drain / 提前中止排水时用. 实时 + 简单: 只翻标志 + 发事件.
        if method == "POST" and path == "/admin/drain/cancel":
            was_draining = self._draining
            self._draining = False
            self._drain_started_at = None
            try:
                # T66.6.3 (B1): drain 是 global admin op, 无 tenant 上下文 —
                # tenant_id='anonymous' 是预期语义, 不要改成 session 维度的 tenant.
                # 改 'global' 字符串会破坏订阅过滤, 保持 'anonymous' 即可.
                self.event_bus.publish(
                    "daemon.drain.cancelled",
                    {"ts": time.time(), "was_draining": was_draining},
                    scope="global", tenant_id=_AsyncOwner.DEFAULT_TENANT,
                    dedup_key=f"drain_cancel:{int(time.time())}",
                    persistent=True,
                )
            except Exception:
                logger.exception("drain cancel: failed to publish event")
            return {"draining": False, "was_draining": was_draining}
        # T54: session CRUD — list / create / delete
        # T65.6: 加 ?tenant_id= 过滤 + tenant_id/agent_id 元数据 (在 metadata 字段).
        # 非 detail 模式保持 list[str] 不变 (向后兼容 dogfooding 路径);
        # metadata 字段额外暴露每 session 的 tenant/agent 归属.
        if method == "GET" and path == "/sessions":
            tenant_filter = args.get("tenant_id")
            if tenant_filter:
                sessions = self.owner.list_sessions_for_tenant(tenant_filter)
            else:
                sessions = self.owner.list_sessions()
            # T63.1: ?detail=1 时每 session 返 url+title — agent 想看 N 个
            # session 各自当前在哪个页, 不必 N+1 次 /state?session=NAME.
            # 失败 (controller 死了/lazy 没 init) 时 url/title 留 None, 不抛.
            # T65.6: detail 模式也带 tenant_id/agent_id 元数据.
            detail = str(args.get("detail", "")).lower() in ("1", "true", "yes")
            if detail:
                items = []
                for s in sessions:
                    entry: dict[str, Any] = {"name": s}
                    meta = self.owner.get_session_meta(s)
                    if meta:
                        entry["tenant_id"] = meta["tenant_id"]
                        entry["agent_id"] = meta["agent_id"]
                        entry["created_at"] = meta["created_at"]
                    try:
                        ctrl = self.owner.run(self.owner.aget_controller(s))
                        entry["url"] = self.owner.run(ctrl.get_url())
                        entry["title"] = self.owner.run(ctrl.get_title())
                    except Exception:
                        entry["url"] = None
                        entry["title"] = None
                    items.append(entry)
                resp: dict[str, Any] = {
                    "sessions": items, "active_count": len(items), "detail": True,
                }
                if tenant_filter:
                    resp["tenant_id"] = tenant_filter
                return resp
            # 非 detail 模式: backward-compat — sessions 仍为 list[str]
            # (dogfooding 路径大量依赖), tenant/agent 元数据放 metadata 字段.
            metadata: dict[str, dict[str, Any]] = {}
            for s in sessions:
                meta = self.owner.get_session_meta(s)
                if meta:
                    metadata[s] = {
                        "tenant_id": meta["tenant_id"],
                        "agent_id": meta["agent_id"],
                    }
            resp_simple: dict[str, Any] = {
                "sessions": sessions, "active_count": len(sessions),
                "metadata": metadata,
            }
            if tenant_filter:
                resp_simple["tenant_id"] = tenant_filter
            return resp_simple
        if method == "POST" and path == "/sessions":
            name = args.get("name") or f"agent-{len(self.owner.list_sessions()) + 1}"
            # T65.6: 接受 tenant_id + agent_id 元数据 — 没带则用 default (anonymous).
            # 给多 agent 共享 daemon 时按 tenant 隔离 session 列表.
            requested_tenant = args.get("tenant_id") or _AsyncOwner.DEFAULT_TENANT
            requested_agent = args.get("agent_id") or _AsyncOwner.DEFAULT_AGENT
            try:
                _ = self.owner.get_controller(name)
            except Exception as e:
                raise _SessionError("SESSION_CREATE_FAILED", f"{type(e).__name__}: {e}") from None
            # T66.8 (high): 修前 — 已存在 session 的重建 (同名 POST /sessions) 会用
            # body 的 tenant_id 覆盖 sessions_index. 攻击者拿到 session 名就能改
            # 它的 tenant binding, 破坏多租户隔离. 修后 — 已存在 session 的
            # tenant 锁住 (不允许改), agent_id 同理 (同一 tenant 才能迁 agent).
            existing = self.lease_manager.get_session_meta(name)
            if existing is not None and existing[0] != _AsyncOwner.DEFAULT_TENANT:
                # 已绑定到真实 tenant → 锁住, 防 hijack. 但允许 anonymous → real
                # (首次绑定) + 同 tenant 的 agent_id 更新.
                tenant_id, agent_id = existing
                if requested_tenant != tenant_id:
                    raise _SessionError(
                        "TENANT_IMMUTABLE",
                        f"session {name!r} belongs to tenant {tenant_id!r}; "
                        f"cannot rebind to {requested_tenant!r}",
                    )
                # 同 tenant → agent_id 可以更新 (e.g. agent 换人)
                agent_id = requested_agent
            else:
                tenant_id, agent_id = requested_tenant, requested_agent
            self.owner.set_session_meta(name, tenant_id=tenant_id, agent_id=agent_id)
            # T66.7.3 (C4): session 创建也发审计 — ops 重建 session 历史需要.
            # dedup 按 session_name (同名重建是不同事件, 但生产里名字唯一 OK).
            try:
                self.event_bus.publish(
                    "session.created",
                    {"session_id": name, "tenant_id": tenant_id,
                     "agent_id": agent_id, "ts": time.time()},
                    scope="session", scope_id=name,
                    tenant_id=tenant_id,
                    producer_kind="agent", producer_id=agent_id,
                    dedup_key=f"session_created:{name}",
                    persistent=True,
                )
            except Exception:
                logger.exception("session create: failed to publish event")
            return {
                "name": name, "created": True,
                "tenant_id": tenant_id, "agent_id": agent_id,
                "active": self.owner.list_sessions(),
            }
        # T65.7: Lease/Fence HTTP 入口 — /sessions/{name}/lease + /renew + DELETE
        # 多 agent 共享 daemon 时, 获取 lease = 拿所有权, 写 op 必须带 lease_id + fence_token.
        # 注意: 必须在 DELETE /sessions/{name} 之前匹配, 否则会被吞.
        if method == "POST" and re.match(r"^/sessions/[^/]+/lease$", path):
            return self._handle_lease_acquire(path, args)
        if method == "POST" and re.match(r"^/sessions/[^/]+/lease/[^/]+/renew$", path):
            return self._handle_lease_renew(path, args)
        if method == "DELETE" and re.match(r"^/sessions/[^/]+/lease/[^/]+$", path):
            return self._handle_lease_release(path, args)
        # T65.7: 读 lease 状态 — /sessions/{name}/lease (GET)
        if method == "GET" and re.match(r"^/sessions/[^/]+/lease$", path):
            return self._handle_lease_get(path, args)
        # T66.1: Reattach — POST /sessions/{name}/reattach
        # daemon 重启 / 实例 crash 后, 旧 agent 用 lease_id+fence_token 恢复所有权.
        if method == "POST" and re.match(r"^/sessions/[^/]+/reattach$", path):
            return self._handle_lease_reattach(path, args)
        # T66.2: Handoff — POST /sessions/{name}/handoff + /handoff/accept
        # 当前 holder A 把 lease 主动让渡给 B (offer + 30s 内 accept).
        if method == "POST" and re.match(r"^/sessions/[^/]+/handoff$", path):
            return self._handle_handoff_offer(path, args)
        if method == "POST" and re.match(r"^/sessions/[^/]+/handoff/accept$", path):
            return self._handle_handoff_accept(path, args)
        # T66.3: Storage state read — GET /sessions/{name}/storage_state
        if method == "GET" and re.match(r"^/sessions/[^/]+/storage_state$", path):
            return self._handle_storage_state(path, args)
        if method == "DELETE" and path.startswith("/sessions/"):
            name = path[len("/sessions/"):]
            if not name:
                raise _SessionError("MISSING_PARAM", "session name required after /sessions/")
            if name == self.owner.DEFAULT_SESSION:
                raise _SessionError("CANNOT_DELETE_DEFAULT", "cannot delete default session")
            # T66.7.3 (C4): release 之前拿 metadata — 删除后 sessions_index 行
            # 还在 (DELETE session 不清 sessions_index, 那是 lease 的事), 但
            # in-memory meta 会被清掉. 优先用 sessions_index 的 tenant_id.
            idx = self.lease_manager.get_session_meta(name)
            if idx is not None:
                tenant_id, agent_id = idx
            else:
                meta = self.owner.get_session_meta(name) or {}
                tenant_id = meta.get("tenant_id", _AsyncOwner.DEFAULT_TENANT)
                agent_id = meta.get("agent_id", _AsyncOwner.DEFAULT_AGENT)
            released = self.owner.release_session(name)
            if not released:
                raise _SessionError("SESSION_NOT_FOUND", f"session {name!r} not found")
            # T66.7.3 (C4): session 删除也发审计.
            try:
                self.event_bus.publish(
                    "session.deleted",
                    {"session_id": name, "tenant_id": tenant_id,
                     "agent_id": agent_id, "ts": time.time()},
                    scope="session", scope_id=name,
                    tenant_id=tenant_id,
                    producer_kind="agent", producer_id=agent_id,
                    dedup_key=f"session_deleted:{name}",
                    persistent=True,
                )
            except Exception:
                logger.exception("session delete: failed to publish event")
            return {"name": name, "released": True, "active": self.owner.list_sessions()}
        if method == "POST" and path == "/open":
            return self.owner.run(self._open(
                args["url"], args.get("session"),
                detail=args.get("detail", "summary"),
                classify_force=str(args.get("classify", "")).lower() in ("1", "true", "force"),
                classify_strict=str(args.get("strict", "")).lower() in ("1", "true"),
            ))
        if method == "GET" and path == "/snapshot":
            return self.owner.run(self._snapshot(
                detail_level=args.get("detail_level", "normal"),
                session=args.get("session"),
            ))
        if method == "GET" and path == "/snapshot-vision":
            return self.owner.run(self._snapshot_vision(
                goal=args.get("goal", ""),
                provider=args.get("provider"),
                model=args.get("model"),
                full_page=bool(args.get("full_page", True)),
                session=args.get("session"),
            ))
        if method == "GET" and path == "/read":
            return self.owner.run(self._read(format=args.get("format", "markdown"), session=args.get("session")))
        if method == "POST" and path == "/click":
            return self.owner.run(self._click(args["ref"], session=args.get("session")))
        if method == "POST" and path == "/click/healed":
            return self.owner.run(self._click_healed(args["ref"]))
        if method == "POST" and path == "/type":
            return self.owner.run(self._type(args["ref"], args["text"], session=args.get("session")))
        if method == "POST" and path == "/type/healed":
            return self.owner.run(self._type_healed(args["ref"], args["text"]))
        if method == "POST" and path == "/hover":
            return self.owner.run(self._hover(args["ref"]))
        if method == "POST" and path == "/dblclick":
            return self.owner.run(self._dblclick(args["ref"]))
        if method == "POST" and path == "/rightclick":
            return self.owner.run(self._rightclick(args["ref"]))
        if method == "POST" and path == "/drag":
            return self.owner.run(self._drag(args["from_ref"], args["to_ref"]))
        if method == "POST" and path == "/drag/html5":
            return self.owner.run(self.owner.browser.controller.drag_html5(
                args["from_ref"], args["to_ref"],
            ))
        if method == "POST" and path == "/select-option":
            return self.owner.run(self._select_option(args["ref"], args["value"]))
        if method == "POST" and path == "/fill-form":
            return self.owner.run(self._fill_form(args["fields"]))
        if method == "POST" and path == "/with-retry":
            # body: {"action": "click|type|open", "args": {...}, "max_retries": 2}
            action_name = args["action"]
            action_args = args.get("args", {})
            max_retries = int(args.get("max_retries", 2))
            # T66.8: SSRF guard — action=open 时 url 也在 SSRF 黑名单兜底.
            # 修前 with-retry(open 分支) 直接 ctrl.open(args["url"]) 不 check.
            if action_name == "open" and "url" in action_args:
                self._check_url(action_args["url"], where="with_retry.open")
            return self.owner.run(self._with_retry(action_name, action_args, max_retries))
        if method == "POST" and path == "/set-files":
            return self.owner.run(self._set_files(args["ref"], args["paths"]))
        if method == "POST" and path == "/download":
            return self.owner.run(self._download(
                args.get("trigger_ref"),
                args.get("save_to"),
                int(args.get("timeout_ms", 30000)),
            ))
        if method == "POST" and path == "/scroll":
            return self.owner.run(self._scroll(args.get("direction", "down"), int(args.get("amount", 500))))
        if method == "POST" and path == "/wait-for/text":
            return self.owner.run(self._wait_for_text(
                args["text"], int(args.get("timeout_ms", 10000)),
                args.get("in_selector", "body"),
            ))
        if method == "POST" and path == "/wait-for/ref":
            return self.owner.run(self._wait_for_ref(
                args["ref"], int(args.get("timeout_ms", 10000)),
            ))
        if method == "POST" and path == "/wait-for/url":
            return self.owner.run(self._wait_for_url(
                args["pattern"], int(args.get("timeout_ms", 10000)),
            ))
        if method == "POST" and path == "/press":
            return self.owner.run(self._press(args["key"]))
        if method == "POST" and path == "/back":
            return self.owner.run(self._back())
        if method == "POST" and path == "/forward":
            return self.owner.run(self._forward())
        if method == "POST" and path == "/screenshot":
            return self.owner.run(self._screenshot(args.get("path")))
        if method == "POST" and path == "/screenshot/annotated":
            # 返回 PNG bytes (base64) + sidecar JSON
            return self.owner.run(self._screenshot_annotated(args.get("path")))
        if method == "POST" and path == "/screenshot/sidecar":
            # 只要 sidecar (没 PNG), 给 LLM 用来 plan 操作
            return self.owner.run(self._screenshot_sidecar())
        # T18: 调试接口 — agent 看 JS console / network / page error
        if method == "GET" and path == "/console":
            type_filter = args.get("type") or None
            limit = int(args.get("limit", 100))
            return self.owner.browser.controller.get_console_messages(
                type_filter=type_filter, limit=limit,
            )
        if method == "GET" and path == "/network":
            only_failed = args.get("only_failed", "false").lower() in ("1", "true", "yes")
            method_filter = args.get("method") or None
            limit = int(args.get("limit", 100))
            return self.owner.browser.controller.get_network_requests(
                only_failed=only_failed, method=method_filter, limit=limit,
            )
        # T39: response headers 查询 (从 network 缓冲里按 URL 取最近一次响应)
        if method == "GET" and path == "/response-headers":
            url = args.get("url", "")
            if not url:
                raise ValueError("url required")
            return self.owner.run(self.owner.browser.controller.get_response_headers(url))
        # T39: DOM diff — 当前 snapshot vs 传入 ref 集合
        if method == "GET" and path == "/dom-diff":
            refs_param = args.get("before_refs", "")
            before_refs = set(refs_param.split(",")) if refs_param else set()
            return self.owner.run(self.owner.browser.controller.get_dom_diff(before_refs))
        # T39: 按 URL 抓 JS 源码 (deep 模式)
        if method == "GET" and path == "/script-source":
            url = args.get("url", "")
            if not url:
                raise ValueError("url required")
            return self.owner.run(self.owner.browser.controller.fetch_script_source(url))
        # T40a: 客户端存储探针 (local/session + cookies)
        if method == "GET" and path == "/storage":
            return self.owner.run(self.owner.browser.controller.get_storage())
        # T40f: 安全头结构化
        if method == "GET" and path == "/security-headers":
            url = args.get("url", "")
            if not url:
                raise ValueError("url required")
            return self.owner.run(self.owner.browser.controller.get_security_headers(url))
        # T40b: Hidden paths probe (httpx 探测常见路径)
        if method == "GET" and path == "/probe-paths":
            url = args.get("url", "")
            if not url:
                raise ValueError("url required")
            cats_raw = args.get("categories", "")
            categories = [c for c in cats_raw.split(",") if c] if cats_raw else None
            return self.owner.run(self.owner.browser.controller.probe_paths(
                url, categories=categories,
            ))
        # T40g: 从页面 JS 提取 API endpoints
        if method == "GET" and path == "/extract-api-endpoints":
            return self.owner.run(
                self.owner.browser.controller.extract_api_endpoints()
            )
        # T42b: JS 库版本 + CVE 识别
        if method == "GET" and path == "/extract-js-libraries":
            return self.owner.run(
                self.owner.browser.controller.extract_js_libraries()
            )
        # T42g: GraphQL introspection
        if method == "GET" and path == "/detect-graphql":
            endpoint = args.get("endpoint", "")
            if not endpoint:
                raise ValueError("endpoint required")
            return self.owner.run(
                self.owner.browser.controller.detect_graphql(endpoint)
            )
        if method == "GET" and path == "/errors":
            limit = int(args.get("limit", 50))
            return self.owner.browser.controller.get_page_errors(limit=limit)
        # T40i: WebSocket 连接列表
        if method == "GET" and path == "/websockets":
            limit = int(args.get("limit", 100))
            return self.owner.browser.controller.get_websockets(limit=limit)
        if method == "POST" and path == "/debug/clear":
            self.owner.browser.controller.clear_event_buffer()
            return {"cleared": True}
        # T43a: 子域名枚举
        if method == "GET" and path == "/enumerate-subdomains":
            return self.owner.run(self.owner.browser.controller.enumerate_subdomains(
                host=args["host"],
                include_tls_san=str(args.get("include_tls_san", "true")).lower() != "false",
            ))
        # T43b: JS secret 扫描
        if method == "GET" and path == "/extract-secrets-from-js":
            return self.owner.run(self.owner.browser.controller.extract_secrets_from_js())
        # T43c: WAF 指纹
        if method == "GET" and path == "/detect-waf":
            return self.owner.run(self.owner.browser.controller.detect_waf())
        # T43d: 开放重定向 sink
        if method == "GET" and path == "/find-open-redirect-sinks":
            return self.owner.run(self.owner.browser.controller.find_open_redirect_sinks())
        # T43e: 敏感信息泄露
        if method == "GET" and path == "/find-disclosure":
            return self.owner.run(self.owner.browser.controller.find_disclosure())
        # T43f: 备份/源码/配置文件
        if method == "GET" and path == "/analyze-exposed-files":
            return self.owner.run(self.owner.browser.controller.analyze_exposed_files(
                base_url=args.get("base_url") or None,
            ))
        # T43g: OpenAPI/Swagger 发现
        if method == "GET" and path == "/discover-api-specs":
            return self.owner.run(self.owner.browser.controller.discover_api_specs(
                base_url=args.get("base_url") or None,
            ))
        # T43h: TLS 证书 SAN
        if method == "GET" and path == "/tls-subdomains":
            return self.owner.run(self.owner.browser.controller.tls_subdomains(
                host=args["host"], port=int(args.get("port", 443)),
            ))
        # T43i: 技术栈指纹
        if method == "GET" and path == "/fingerprint-tech":
            return self.owner.run(self.owner.browser.controller.fingerprint_tech())
        # T43j: JWT 解码
        if method == "GET" and path == "/decode-jwts":
            return self.owner.run(self.owner.browser.controller.decode_jwts())
        # T44a: DNS 记录
        if method == "GET" and path == "/dns-records":
            return self.owner.run(self.owner.browser.controller.dns_records(host=args["host"]))
        # T44l 也支持 host
        if method == "GET" and path == "/check-subdomain-takeover" and "host" in args:
            subs = args.get("subdomains")
            return self.owner.run(self.owner.browser.controller.check_subdomain_takeover(
                host=args["host"],
                subdomains=subs if isinstance(subs, list) else None,
            ))
        # T44b: Wayback Machine
        if method == "GET" and path == "/wayback-urls":
            return self.owner.run(self.owner.browser.controller.wayback_urls(
                url=args["url"], limit=int(args.get("limit", 200)),
            ))
        # T44c: DOM XSS sinks
        if method == "GET" and path == "/find-xss-sinks":
            return self.owner.run(self.owner.browser.controller.find_xss_sinks())
        # T44d: auth methods
        if method == "GET" and path == "/detect-auth-methods":
            return self.owner.run(self.owner.browser.controller.detect_auth_methods())
        # T44e: CSRF coverage
        if method == "GET" and path == "/check-csrf-coverage":
            return self.owner.run(self.owner.browser.controller.check_csrf_coverage())
        # T44f: IDOR URLs
        if method == "GET" and path == "/find-idor-urls":
            return self.owner.run(self.owner.browser.controller.find_idor_urls())
        # T44g: cloud resources
        if method == "GET" and path == "/find-cloud-resources":
            return self.owner.run(self.owner.browser.controller.find_cloud_resources())
        # T44h: HTTP methods
        if method == "GET" and path == "/probe-http-methods":
            paths = args.get("paths")
            return self.owner.run(self.owner.browser.controller.probe_http_methods(
                base_url=args.get("base_url") or None,
                paths=paths if isinstance(paths, list) else None,
            ))
        # T44i: 2FA
        if method == "GET" and path == "/detect-2fa":
            return self.owner.run(self.owner.browser.controller.detect_2fa())
        # T44j: external resources
        if method == "GET" and path == "/inventory-external-resources":
            return self.owner.run(self.owner.browser.controller.inventory_external_resources())
        # T44k: CSP parse
        if method == "GET" and path == "/parse-csp":
            return self.owner.run(self.owner.browser.controller.parse_csp())
        # T44l: subdomain takeover
        if method == "GET" and path == "/check-subdomain-takeover":
            subs = args.get("subdomains")
            return self.owner.run(self.owner.browser.controller.check_subdomain_takeover(
                subdomains=subs if isinstance(subs, list) else None,
            ))
        # T47: a11y audit (axe-core)
        if method == "GET" and path == "/a11y-audit":
            max_nodes = int(args.get("max_nodes_per_violation", 5))
            standards = args.get("standards")
            if not isinstance(standards, list):
                standards = None
            return self.owner.run(self.owner.browser.controller.a11y_audit(
                max_nodes_per_violation=max_nodes,
                standards=standards,
            ))
        # T17: cookie / storage 管理
        if method == "GET" and path == "/cookies":
            url = args.get("url") or None
            return self.owner.run(self.owner.browser.controller.get_cookies(url))
        if method == "POST" and path == "/cookies/set":
            return self.owner.run(self.owner.browser.controller.set_cookie(
                name=args["name"], value=args["value"],
                url=args.get("url") or None,
                domain=args.get("domain") or None,
                path=args.get("path", "/"),
            ))
        if method == "POST" and path == "/cookies/delete":
            return self.owner.run(self.owner.browser.controller.delete_cookie(
                name=args["name"], url=args.get("url") or None,
            ))
        if method == "POST" and path == "/cookies/clear":
            n = self.owner.run(self.owner.browser.controller.clear_cookies())
            return {"cleared": n}
        if method == "GET" and path == "/storage":
            kind = args.get("kind", "local")
            return self.owner.run(self.owner.browser.controller.read_storage(kind=kind))
        if method == "POST" and path == "/storage/set":
            return self.owner.run(self.owner.browser.controller.set_storage(
                key=args["key"], value=args["value"], kind=args.get("kind", "local"),
            ))
        if method == "POST" and path == "/storage/clear":
            return self.owner.run(self.owner.browser.controller.clear_storage(
                kind=args.get("kind", "local"),
            ))
        # T16: 键盘 / 焦点 / Tab 导航
        if method == "GET" and path == "/focus":
            return self.owner.run(self.owner.browser.controller.get_focused_element())
        if method == "POST" and path == "/focus":
            return self.owner.run(self.owner.browser.controller.focus(args["ref"]))
        if method == "POST" and path == "/tab":
            shift = args.get("shift", "false").lower() in ("1", "true", "yes")
            count = int(args.get("count", 1))
            return self.owner.run(self.owner.browser.controller.tab(shift=shift, count=count))
        if method == "POST" and path == "/keyboard/shortcut":
            keys = args["keys"] if isinstance(args["keys"], list) else [args["keys"]]
            return self.owner.run(self.owner.browser.controller.keyboard_shortcut(*keys))
        if method == "POST" and path == "/keyboard/type":
            text = args["text"]
            delay_ms = int(args.get("delay_ms", 0))
            return self.owner.run(self.owner.browser.controller.type_into_active(
                text, delay_ms=delay_ms,
            ))
        if method == "POST" and path == "/agent/run":
            # T66.8: SSRF guard — start_url 也要 check. agent 爬虫从 start_url
            # 出发可达私网, 不 check 等于把整个 SSRF guardrail 当装饰品.
            start_url = args.get("start_url", "")
            if start_url:
                self._check_url(start_url, where="agent_run")
            return self.owner.run(self._run_agent(args))
        # T67: /v1/query — semantic query 入口 (model-driven browser semantic layer)
        if method == "POST" and path == "/v1/query":
            start_url = args.get("start_url", "") or ""
            if start_url:
                # T66.8 SSRF: 跟 /agent/run /discover 一样检查 start_url
                self._check_url(start_url, where="v1_query")
            return self.owner.run(self._run_semantic_query(args))
        # T67+ T68: /v1/query/stream — SSE 流式 progress
        if method == "POST" and path == "/v1/query/stream":
            start_url = args.get("start_url", "") or ""
            if start_url:
                self._check_url(start_url, where="v1_query.stream")
            if req is None:
                raise ValueError("/v1/query/stream requires req context")
            self._stream_semantic_query(req, args)
            return "_SSE_HANDLED"
        # T68: /v1/query/stats — 监控 endpoint
        if method == "GET" and path == "/v1/query/stats":
            return self.owner.run(self._run_query_stats())
        # T76: GET /v1/query/log — 最近 N 条 query log
        if method == "GET" and path == "/v1/query/log":
            limit = int(args.get("limit", 50)) if str(args.get("limit", "")).isdigit() else 50
            return self._run_query_log(limit=limit)
        # T69: POST /v1/query/cache/clear — 清空共享 SemanticQuery cache
        if method == "POST" and path == "/v1/query/cache/clear":
            if self._semantic_query is not None:
                return self._semantic_query.clear_cache()
            return {"cleared": 0, "remaining": 0, "note": "no shared SemanticQuery configured"}
        # T53: SSE 流式 agent run — 复用 on_step 钩子推 step-by-step
        if method == "POST" and path == "/agent/run/stream":
            if req is None:
                raise ValueError("/agent/run/stream requires req context")
            # T66.8: SSRF guard — 跟 /agent/run 一致
            start_url = args.get("start_url", "")
            if start_url:
                self._check_url(start_url, where="agent_run.stream")
            self._stream_agent_run(req, args)
            return "_SSE_HANDLED"
        # T29: dry-run plan preview
        if method == "POST" and path == "/agent/plan":
            return self.owner.run(self._plan_agent(args))
        # T27: 跨 session goal memory
        if method == "GET" and path == "/memory/stats":
            from semantic_browser.memory.goal_memory import GoalMemory
            return GoalMemory().stats()
        if method == "GET" and path == "/memory/list":
            from semantic_browser.memory.goal_memory import GoalMemory
            limit = int(args.get("limit", 20))
            return {"entries": GoalMemory().list_recent(limit)}
        if method == "POST" and path == "/memory/clear":
            from semantic_browser.memory.goal_memory import GoalMemory
            GoalMemory().clear()
            return {"cleared": True}
        # T23/T24: LLM 智能辅助端点
        if method == "GET" and path == "/llm/stats":
            from semantic_browser.llm import get_default_service
            return get_default_service().stats()
        if method == "POST" and path == "/llm/slice":
            return self.owner.run(self._llm_slice(args))
        if method == "POST" and path == "/llm/summarize":
            return self.owner.run(self._llm_summarize(args))
        if method == "POST" and path == "/llm/extract":
            return self.owner.run(self._llm_extract(args))
        if method == "POST" and path == "/llm/find-ref":
            return self.owner.run(self._llm_find_ref(args))
        if method == "POST" and path == "/state/save":
            return self.owner.run(self._save_state(args.get("path")))
        if method == "GET" and path == "/tab/list":
            return self.owner.browser.controller.list_tabs()
        if method == "POST" and path == "/tab/new":
            url = args.get("url", "")
            # T66.8: SSRF guard — 修前 _tab_new 直接调 controller.new_tab(url),
            # 绕开了 _open 的 _ssrf_check. 攻击者可打到 169.254.169.254 /
            # 内部服务 / file:// — 跟 _open 行为不一致.
            self._check_url(url, where="tab_new")
            return self.owner.run(self._tab_new(url))
        if method == "POST" and path == "/tab/switch":
            idx = int(args["index"])
            return self.owner.run(self._tab_switch(idx))
        if method == "POST" and path == "/tab/close":
            idx = int(args["index"]) if "index" in args else None
            return self.owner.run(self._tab_close(idx))
        if method == "GET" and path == "/frame/list":
            return self.owner.run(self.owner.browser.controller.list_frames())
        if method == "POST" and path == "/frame/switch":
            name_or_url = args["name_or_url"]
            return self.owner.run(self.owner.browser.controller.switch_frame(name_or_url))
        if method == "POST" and path == "/frame/to-top":
            self.owner.run(self.owner.browser.controller.to_top_frame())
            return {"active": "main"}
        if method == "GET" and path == "/history":
            pages = self._get_visited_pages(args.get("domain", ""))
            return {"pages": pages, "count": len(pages)}
        if method == "GET" and path == "/graph":
            url = args.get("url") or self.owner.run(self.owner.browser.controller.get_url())
            return self._get_site_graph(url).to_dict()
        # T30: live site map discovery (vs /graph 走历史库)
        if method == "POST" and path == "/discover":
            # T66.8: SSRF guard — start_url 也要 check.
            start_url = args.get("start_url", "")
            if start_url:
                self._check_url(start_url, where="discover")
            return self.owner.run(self._discover(args))
        # T50: 流式版 — SSE (Server-Sent Events), 客户端可用 EventSource 消费
        if method == "GET" and path == "/discover/stream":
            if req is None:
                raise ValueError("/discover/stream requires req context")
            # T66.8: SSRF guard — start_url 跟 /discover 一致.
            start_url = args.get("start_url", "")
            if start_url:
                self._check_url(start_url, where="discover.stream")
            self._stream_discover(req, args)
            return "_SSE_HANDLED"
        if method == "POST" and path == "/find":
            url = args["url"]
            keyword = args["keyword"]
            max_results = int(args.get("max_results", 10))
            return self.owner.run(self._find(url, keyword, max_results=max_results))
        if method == "POST" and path == "/extract-topic":
            url = args["url"]
            keyword = args["keyword"]
            max_chars = int(args.get("max_chars", 4000))
            return self.owner.run(self._extract_topic(url, keyword, max_chars=max_chars))
        if method == "POST" and path == "/note":
            url = args["url"]
            note = args["note"]
            self.memory_store.add_note(url, note)
            return {"saved": True, "url": url}
        if method == "GET" and path == "/stats":
            return self.memory_store.stats()
        if method == "POST" and path == "/run-workflow":
            return self.owner.run(self._run_workflow(args["workflow_file"]))
        if method == "GET" and path == "/notes":
            url = args.get("url", "")
            limit = int(args.get("limit", 50))
            if url:
                rows = self.memory_store.get_notes(url)[:limit]
                return {"count": len(rows), "notes": rows}
            with self.memory_store._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                notes_list = [dict(r) for r in rows]
            return {"count": len(notes_list), "notes": notes_list}
        raise ValueError(f"unknown endpoint: {method} {path}")

    # ── T65.7: Lease/Fence HTTP handlers ──────────────────────────

    def _handle_lease_acquire(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/lease — 获取/抢占 lease.

        Body (form-encoded):
          agent_id: str (必填)
          tenant_id: str (默认 'anonymous')
          priority: int (默认 1; 数字越小越高, 0=critical)
          preempt: 'true'/'false' (默认 false)
          ttl_s: float (默认 daemon 配置)

        T66.8: tenant 锁 — 已存在 session (sessions_index 有行) 时, 忽略 body
        的 tenant_id, 用 stored 那个. 否则 attacker 拿到 session 名就能改
        tenant binding. 新 session 才用 body 的.
        """
        m = re.match(r"^/sessions/([^/]+)/lease$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        agent_id = args.get("agent_id") or ""
        if not agent_id:
            raise _LeaseError("MISSING_PARAM", "agent_id required")
        requested_tenant = args.get("tenant_id") or _AsyncOwner.DEFAULT_TENANT
        # T66.8: tenant 锁 — 已存在 sessions_index 行 + 该 tenant != 'anonymous'
        # (即已经被某个真实 tenant 绑过) 时, body 的 tenant 必须一致. 防跨租户
        # hijack: 攻击者拿到 session 名, 用别的 tenant acquire lease 来改 binding.
        #
        # 例外: 现 tenant='anonymous' → 允许 update 到真实 tenant (典型用法:
        # POST /sessions (不传 tenant) → POST .../lease {tenant_id=acme}). 这是
        # "首次绑定", 不算 hijack. 锁住是为了防止 hijack, 不是阻止首次绑定.
        existing = self.lease_manager.get_session_meta(session_name)
        if existing is not None and existing[0] != _AsyncOwner.DEFAULT_TENANT:
            tenant_id = existing[0]
            if requested_tenant != tenant_id:
                raise _LeaseError(
                    "TENANT_IMMUTABLE",
                    f"session {session_name!r} belongs to tenant {tenant_id!r}; "
                    f"cannot acquire under {requested_tenant!r}",
                    status_code=403,
                )
        else:
            tenant_id = requested_tenant
        try:
            priority = int(args.get("priority", "1"))
        except ValueError:
            priority = 1
        preempt = str(args.get("preempt", "")).lower() in ("1", "true", "yes")
        ttl_s: float | None = None
        if args.get("ttl_s"):
            try:
                ttl_s = float(args["ttl_s"])
            except ValueError:
                pass

        result = self.lease_manager.acquire(
            session_id=session_name, agent_id=agent_id, tenant_id=tenant_id,
            priority=priority, preempt=preempt, ttl_s=ttl_s,
        )
        if not result.ok:
            # 409 Conflict — 当前 holder 在 lease 字段
            raise _LeaseError(result.error or "UNKNOWN", f"acquire failed: {result.error}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=409)
        out: dict[str, Any] = {"lease": result.lease.to_dict()}
        if result.preempted:
            out["preempted"] = result.preempted.to_dict()
        # 同时记 session metadata (T65.6 一致性)
        meta = self.owner.get_session_meta(session_name) or {}
        if not meta or meta.get("tenant_id") == _AsyncOwner.DEFAULT_TENANT:
            self.owner.set_session_meta(session_name, tenant_id=tenant_id, agent_id=agent_id)
        # T66.7.1 (C1): 核心 lease 获取应发审计事件 — 多 agent 共享 daemon
        # 时 ops/agent 想知道 "谁在何时拿到了所有权". dedup 按 lease_id — 同一
        # lease_id 重 acquire (PREEMPTED 路径) 是新事件 (租户不同时间点).
        try:
            self.event_bus.publish(
                "session.lease.acquired",
                {"session_id": session_name,
                 "lease_id": result.lease.lease_id,
                 "fence_token": result.lease.fence_token,
                 "agent_id": agent_id,
                 "preempted_lease_id": (
                     result.preempted.lease_id if result.preempted else None),
                 "priority": priority,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=tenant_id,
                producer_kind="agent", producer_id=agent_id,
                dedup_key=f"lease_acquired:{result.lease.lease_id}",
                persistent=True,
            )
        except Exception:
            logger.exception("lease acquire: failed to publish event")
        return out

    def _handle_lease_renew(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/lease/{lease_id}/renew — 心跳续约."""
        m = re.match(r"^/sessions/([^/]+)/lease/([^/]+)/renew$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        lease_id = m.group(2)
        fence_token_str = args.get("fence_token", "0")
        try:
            fence_token = int(fence_token_str)
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        ok, reason = self.lease_manager.heartbeat(lease_id, fence_token)
        if not ok:
            raise _LeaseError(reason, f"heartbeat failed: {reason}", status_code=409)
        cur = self.lease_manager.get_lease(lease_id)
        return {"lease": cur.to_dict() if cur else None}

    def _handle_lease_release(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """DELETE /sessions/{name}/lease/{lease_id} — 主动释放."""
        m = re.match(r"^/sessions/([^/]+)/lease/([^/]+)$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        lease_id = m.group(2)
        fence_token_str = args.get("fence_token", "0")
        try:
            fence_token = int(fence_token_str)
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        reason = args.get("reason") or "released"
        ok, r = self.lease_manager.release(lease_id, fence_token, reason=reason)
        if not ok:
            raise _LeaseError(r, f"release failed: {r}", status_code=409)
        # T66.7.1 (C1): 主动释放也要审计 — ops 看 "谁在何时放弃所有权".
        # tenant_id 从 lease 表读 (权威, 不是 body). release 路径 lease 还在表里.
        cur = self.lease_manager.get_lease(lease_id)
        try:
            self.event_bus.publish(
                "session.lease.released",
                {"session_id": session_name,
                 "lease_id": lease_id,
                 "fence_token": fence_token,
                 "reason": reason,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=(cur.tenant_id if cur else _AsyncOwner.DEFAULT_TENANT),
                producer_kind="agent",
                producer_id=(cur.agent_id if cur else "anonymous"),
                dedup_key=f"lease_released:{lease_id}",
                persistent=True,
            )
        except Exception:
            logger.exception("lease release: failed to publish event")
        return {"lease_id": lease_id, "state": "RELEASED", "reason": reason}

    def _handle_lease_get(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """GET /sessions/{name}/lease — 看当前 active lease."""
        m = re.match(r"^/sessions/([^/]+)/lease$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        cur = self.lease_manager.get_active_for_session(session_name)
        return {
            "session_id": session_name,
            "lease": cur.to_dict() if cur else None,
        }

    # ── T66.1: Reattach ──────────────────────────────────────────

    def _handle_lease_reattach(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/reattach — daemon 重启后恢复所有权.

        Body:
          lease_id: str (必填)
          fence_token: int (必填)
          agent_id: str (default: 复用原 lease 的 agent_id)
          tenant_id: str (default 'anonymous')

        Returns:
          {recovered: true, lease, age_ms, advice}
          advice="re_verify_auth" if age > 300s (登录态可能过期)
        """
        m = re.match(r"^/sessions/([^/]+)/reattach$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        lease_id = args.get("lease_id") or ""
        if not lease_id:
            raise _LeaseError("MISSING_PARAM", "lease_id required")
        try:
            fence_token = int(args.get("fence_token", "0"))
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        agent_id = args.get("agent_id") or ""

        cur = self.lease_manager.get_lease(lease_id)
        if cur is None:
            raise _LeaseError("LEASE_INVALID", f"lease {lease_id!r} not found",
                              status_code=404)
        # T66.6.3 (B1): audit event 必须用原 lease 的 tenant_id, 不用 request body —
        # body 经常漏传/写错, 而 lease 表里的 tenant_id 是权威. reattach 本质
        # 是恢复, 不是新建 — tenant 应继承原 lease.
        tenant_id = cur.tenant_id
        # 缺 agent_id 时复用原 lease的 (这样 fence_token 不需要重新交换)
        effective_agent = agent_id or cur.agent_id
        result = self.lease_manager.reattach(
            lease_id=lease_id, fence_token=fence_token,
            agent_id=effective_agent, tenant_id=tenant_id,
        )
        if not result.ok:
            err = result.error or "UNKNOWN"
            status = 410 if err in ("LEASE_LOST", "LEASE_INVALID") else 409
            raise _LeaseError(err, f"reattach failed: {err}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=status)
        # age_ms — 从 acquired_at_ms 到现在
        now_ms = int(time.time() * 1000)
        age_ms = max(0, now_ms - result.lease.acquired_at_ms)
        advice: str | None = None
        if age_ms > 300_000:  # 300s = 5 分钟 (fable §5.4)
            advice = "re_verify_auth"
        # T66.1: 触发 aget_controller 确保 BrowserContext 还在; 不在也 OK (lazy init)
        try:
            self.owner.run(self.owner.aget_controller(session_name))
        except Exception:
            pass  # T54 后 lazy init, 不强制要求存在
        # 发 audit 事件 — 持久化, dedup 保证幂等
        self.event_bus.publish(
            "session.restored",
            {"session_id": session_name, "lease_id": lease_id,
             "fence_token": result.lease.fence_token,
             "agent_id": effective_agent, "age_ms": age_ms, "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=effective_agent,
            dedup_key=f"restore:{lease_id}:{result.lease.fence_token}",
            persistent=True,
        )
        out: dict[str, Any] = {
            "recovered": True,
            "lease": result.lease.to_dict(),
            "age_ms": age_ms,
            "advice": advice,
        }
        return out

    # ── T66.2: Handoff ───────────────────────────────────────────

    def _handle_handoff_offer(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/handoff — 当前 holder A 主动让渡给 B.

        Body:
          agent_id: str (B 的 agent_id, 必填)
          tenant_id: str (default 'anonymous')
          ttl_s: float (default 30s)

        Returns:
          {offer_token, expires_at_ms, offered_to}
        """
        m = re.match(r"^/sessions/([^/]+)/handoff$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        to_agent = args.get("agent_id") or ""
        if not to_agent:
            raise _LeaseError("MISSING_PARAM", "agent_id required (the recipient)")
        ttl_s = 30.0
        if args.get("ttl_s"):
            try:
                ttl_s = float(args["ttl_s"])
            except ValueError:
                pass
        # 当前 holder 必须是 lease 元数据里的 agent_id (assume single holder)
        cur = self.lease_manager.get_active_for_session(session_name)
        if cur is None:
            raise _LeaseError("LEASE_INVALID",
                              f"no active lease for session {session_name!r}",
                              status_code=404)
        # T66.7.1 (C1): tenant_id 用原 lease 的 (跟 T66.6.2 accept 路径一致),
        # body 漏传也不退化成 'anonymous'. body 仍可显式覆盖 (e.g. tenant 重命名),
        # 跟 acquire 路径行为对称.
        tenant_id = args.get("tenant_id") or cur.tenant_id
        ok, offer_token, err, deadline_ms = self.lease_manager.offer(
            session_id=session_name, from_agent=cur.agent_id, to_agent=to_agent,
            tenant_id=tenant_id, ttl_s=ttl_s,
        )
        if not ok:
            status = 409 if err == "BUSY" else 410
            raise _LeaseError(err or "UNKNOWN",
                              f"handoff offer failed: {err}",
                              holder=cur.to_dict(), status_code=status)
        # T66.7.1 (C1): offer 也发审计 — 跟 session.handed_off (accept 端) 配对.
        # dedup 按 offer_token, 即使重新发 offer 也是不同事件 (token 是新的).
        try:
            self.event_bus.publish(
                "session.handoff.offered",
                {"session_id": session_name,
                 "from_agent": cur.agent_id,
                 "to_agent": to_agent,
                 "offer_token": offer_token,
                 "deadline_ms": deadline_ms,
                 "ttl_s": ttl_s,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=tenant_id,
                producer_kind="agent", producer_id=cur.agent_id,
                dedup_key=f"handoff_offered:{offer_token}",
                persistent=True,
            )
        except Exception:
            logger.exception("handoff offer: failed to publish event")
        return {
            "offer_token": offer_token,
            "expires_at_ms": deadline_ms,
            "offered_to": to_agent,
        }

    def _handle_handoff_accept(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/handoff/accept — B 用 offer_token 接受.

        Body:
          offer_token: str (必填)
          agent_id: str (B 的 agent_id, 必填, 必须等于 offer_to)
          tenant_id: str (default 'anonymous')

        Returns:
          {lease, acquired_from: old_lease_id}
        """
        m = re.match(r"^/sessions/([^/]+)/handoff/accept$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        offer_token = args.get("offer_token") or ""
        if not offer_token:
            raise _LeaseError("MISSING_PARAM", "offer_token required")
        to_agent = args.get("agent_id") or ""
        if not to_agent:
            raise _LeaseError("MISSING_PARAM", "agent_id required (must match offer_to)")
        # T66.6.2 (B3): tenant_id 不能从 request body 拿 (用户经常漏传) — 必须
        # 用原 lease 的 tenant_id (offer 时的), 防止 handoff 后 metadata 写 anonymous.
        # 原 offer 是从 cur (active lease) 的 agent 视角发起的, tenant_id 也在 cur 里.
        cur = self.lease_manager.get_active_for_session(session_name)
        if cur is None:
            raise _LeaseError("LEASE_INVALID",
                              f"no active offer for session {session_name!r}",
                              status_code=404)
        tenant_id = cur.tenant_id

        result = self.lease_manager.accept_handoff(
            session_id=session_name, to_agent=to_agent,
            offer_token=offer_token, tenant_id=tenant_id,
        )
        if not result.ok:
            err = result.error or "UNKNOWN"
            status = 410 if err in ("LEASE_LOST", "OFFER_NOT_FOUND",
                                   "OFFER_EXPIRED", "LEASE_INVALID") else 409
            raise _LeaseError(err,
                              f"handoff accept failed: {err}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=status)
        out: dict[str, Any] = {"lease": result.lease.to_dict()}
        if result.preempted:
            out["acquired_from"] = result.preempted.lease_id
        # T66.2: 同步 session metadata 到新 agent. T66.6.2 (B3): 用新 lease 的
        # tenant_id (accept_handoff 已经写到 sessions_index), 不用 request body.
        self.owner.set_session_meta(session_name,
                                    tenant_id=result.lease.tenant_id,
                                    agent_id=to_agent)
        # audit event
        self.event_bus.publish(
            "session.handed_off",
            {"session_id": session_name,
             "from_agent": result.preempted.agent_id if result.preempted else None,
             "to_agent": to_agent,
             "new_lease_id": result.lease.lease_id,
             "fence_token": result.lease.fence_token,
             "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=result.lease.tenant_id,
            producer_kind="agent", producer_id=to_agent,
            dedup_key=f"handoff:{result.lease.lease_id}",
            persistent=True,
        )
        return out

    # ── T66.3: Storage state read ────────────────────────────────

    def _handle_storage_state(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """GET /sessions/{name}/storage_state — 读最新 storage_state 快照.

        读 SnapshotStore.latest_snapshot(), 不存在 → 404 SNAPSHOT_NOT_FOUND.
        每次导出 emit 一次审计事件 (T61 §7.8).
        """
        m = re.match(r"^/sessions/([^/]+)/storage_state$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        snap = self.snapshot_store.latest_snapshot(session_name)
        if snap is None:
            raise _SessionError("SNAPSHOT_NOT_FOUND",
                                f"no snapshot for session {session_name!r}")
        # 审计事件 — 每次导出都记, dedup 按 sha256(content)
        import hashlib
        content_bytes = json.dumps(snap["content"], ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")
        content_sha = hashlib.sha256(content_bytes).hexdigest()
        # T66.6.3 (B1): 优先读 sessions_index (持久化, 跨重启保留) — T66.6.1
        # 修完后 _AsyncOwner 启动时已预热, 但 owner.get_session_meta 读的是
        # in-memory dict, 跟 sessions_index 可能脱节. 直接查 lease_manager
        # 是 source of truth, 在两个地方都查不到时 fallback 到 in-memory.
        idx = self.lease_manager.get_session_meta(session_name)
        if idx is not None:
            tenant_id, agent_id = idx
        else:
            meta = self.owner.get_session_meta(session_name) or {}
            tenant_id = meta.get("tenant_id", _AsyncOwner.DEFAULT_TENANT)
            agent_id = meta.get("agent_id", _AsyncOwner.DEFAULT_AGENT)
        self.event_bus.publish(
            "session.storage_state.exported",
            {"session_id": session_name, "snapshot_id": snap["snapshot_id"],
             "size_bytes": snap["size_bytes"], "content_sha256": content_sha[:16],
             "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=agent_id,
            dedup_key=f"ss_export:{session_name}:{content_sha[:16]}",
            persistent=True,
        )
        return snap

    # ── T66.5: Probes ────────────────────────────────────────────

    def _handle_health_full(self) -> dict[str, Any]:
        """T49+T62: /health 老路径 — 返完整 context (status / pid / uptime / drain / etc).
        保持 backward-compat, agent dogfooding 路径大量依赖.
        """
        page_url = None
        try:
            # T49: 只读 current_page.url — 不要触发 _ensure_page 创建 about:blank
            page = self.owner.browser.controller.current_page
            if page is not None and not page.is_closed():
                page_url = page.url
        except Exception:
            pass
        status = "draining" if self._draining else "ok"
        elapsed = (
            round(time.time() - self._drain_started_at, 1)
            if (self._draining and self._drain_started_at)
            else None
        )
        return {
            "status": status,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "page_url": page_url,
            "draining": self._draining,
            "drain_elapsed_s": elapsed,
            "drain_timeout_s": self._drain_timeout_s,
            "in_flight_op": self._current_op,
        }

    def _handle_readyz(self) -> dict[str, Any]:
        """T66.5: /readyz — k8s readiness probe. 能接流量才返 200, 否则 503.

        不满足任一条 → 503 + Retry-After: 30 (走 _DegradationError 路径):
          1. not self._draining (drain 中不接新 op)
          2. self._degradation_level < 4 (L4 全拒)
          3. self._browser_pool is not None (T60 watchdog 已 init)
        """
        reasons: list[str] = []
        if self._draining:
            reasons.append("draining")
        if getattr(self, "_degradation_level", 0) >= 4:
            reasons.append("degraded_l4")
        pool = getattr(self.owner, "pool", None)
        if pool is None:
            reasons.append("no_browser_pool")
        if reasons:
            # 走 _DegradationError 路径 → 自动 Retry-After: 30
            raise _DegradationError(
                "SERVICE_UNAVAILABLE",
                f"not ready: {','.join(reasons)}",
                level=getattr(self, "_degradation_level", 0),
            )
        return {"ready": True, "pid": os.getpid()}

    async def _state(self, session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        url = await ctrl.get_url()
        title = await ctrl.get_title()
        # T63.2 (#3 修): 优先吃 /open 已分类结果, 0 额外 I/O. /open 后立即 /state
        # 是常见模式 (agent 想知道分类确认). 无缓存 (e.g. daemon 重启) → 走
        # 启发式 fast-path (不调 LLM, /state 是高频 polling 端点, 必须轻量).
        try:
            cached = self._classify_cache.get(url)
            if cached:
                page_type = cached["page_type"]
            else:
                page = ctrl.current_page
                if page is not None:
                    from semantic_browser.snapshot.engine import SnapshotEngine
                    from semantic_browser.classifier.heuristic import PageClassifier
                    snap = await SnapshotEngine(page).capture(base_url=url)
                    cls = PageClassifier().classify(snap)
                    page_type = cls.page_type
                else:
                    page_type = None
        except Exception:
            page_type = None
        return {"url": url, "title": title, "type": page_type}

    def _check_url(self, url: str, *, where: str = "open") -> str:
        """T58 + T66.8: SSRF 集中校验 — 任何 handler 拿用户 URL 都应调我.

        修前: 只有 /open 调 _ssrf_check. /tab/new + /with-retry(open 分支)
        + /discover + /agent/run 都接收 url 但直接传给 controller,
        绕开 SSRF 闸 — agent 可打到 169.254.169.254 / 内部服务.

        修后: 这个方法统一入口, 各 handler 进来先调, 失败抛 SSRFBlockedError
        → 自动 400 + error envelope (跟 _open 路径一致).
        """
        from semantic_browser.safety.ssrf import check_url as _ssrf_check, SSRFBlockedError
        try:
            return _ssrf_check(
                url, allowlist=self._ssrf_allowlist,
                allow_data=self._allow_data_scheme,
            )
        except SSRFBlockedError as e:
            raise SSRFBlockedError(f"{where}() blocked: {e}") from None

    async def _open(self, url: str, session: str | None = None,
                    *, detail: str = "summary",
                    classify_force: bool = False,
                    classify_strict: bool = False) -> dict[str, Any]:
        # T58 + T66.8: 用集中 _check_url helper — 跟其它入口一致
        checked_url = self._check_url(url, where="open")
        ctrl = await self.owner.aget_controller(session)
        page = await ctrl.open(checked_url)
        # T61: navigate 成功 → 标记 session dirty (sweep 会抓快照)
        # session=None → default session (跟 controller pool 行为一致)
        effective_session = session or _AsyncOwner.DEFAULT_SESSION
        self.snapshot_store.mark_dirty(effective_session)
        snap = await SnapshotEngine(page).capture(base_url=checked_url)
        # T63.2 (#3 修): 启发式 → 缓存 → LLM-augment 三段式分类.
        # 1. 启发式先跑 (0 LLM 调用, < 1ms)
        # 2. URL 已有缓存 → 直接复用
        # 3. 启发式置信度 < 0.5 且 OPENAI_API_KEY 配了 → LLM 二次判断
        #    失败/超时 → silent 回启发式
        # T64: ?classify=force 跳过 cache (内容变了或测试场景),
        # 同时打点 classify_latency_ms 让 agent / 运维知道这次耗时.
        import time as _time
        _t0 = _time.monotonic()
        cls_result, src = await self._classify_with_cache(
            snap, force=classify_force, strict=classify_strict,
        )
        classify_ms = round((_time.monotonic() - _t0) * 1000.0, 1)
        result: dict[str, Any] = {
            "url": snap.url, "title": snap.title,
            "type": cls_result["page_type"],
            "type_confidence": cls_result.get("confidence"),
            "type_source": src,  # "heuristic" | "cached" | "llm"
            "classify_latency_ms": classify_ms,  # T64: 可观测 — 缓存 0ms / 启发式 <1ms / LLM 200ms-2s
        }
        # T63.2 (#2 修): 默认 summary 模式比 _open 之前更丰富 — agent 第一次
        # open 后能立刻知道 "这个页面是干嘛的", 不用再调 /snapshot. 体积仍小
        # (kb 级), 全字段都是 snapshot 已有值, 0 额外 I/O.
        headings = [b for b in snap.text_blocks if b.tag in ("h1", "h2", "h3")]
        h1 = next((b for b in headings if b.tag == "h1"), None)
        if h1:
            result["heading"] = h1.text
            result["heading_source"] = "h1"
        elif snap.title:
            # T64: 页面无 h1 (e.g. 搜索结果页 / listing) — 退到 title 当 heading
            # 让 agent 永远拿到非空主标题, 不必 fallback None
            result["heading"] = snap.title
            result["heading_source"] = "title"
        else:
            result["heading"] = None
            result["heading_source"] = None
        result["top_headings"] = [
            f"[{b.tag}] {b.text[:80]}" for b in headings[:5]
        ]
        result["meta"] = {
            k: snap.meta.get(k, "") for k in ("description", "lang", "charset")
            if snap.meta.get(k)
        }
        result["counts"] = {
            "text_blocks": len(snap.text_blocks),
            "links": len(snap.links),
            "controls": len(snap.controls),
            "forms": len(snap.forms),
            "scripts": len(snap.scripts),
        }
        if detail == "full":
            # 完整 snapshot — agent 想拿 text_blocks/aria/scripts 等重的字段
            # 都直接给, 不要再调 /snapshot
            result["snapshot"] = snap.to_dict()
        else:
            # 默认 summary — 顺便把 clickable refs 一起给, 节省一次
            # roundtrip (agent 开完页马上能 click, 不用先 /snapshot).
            # 字段精简: ref + text + (href|kind), 不带 outer_html/aria 这种重的
            refs: list[dict[str, Any]] = []
            seen_refs: set[str] = set()
            # T64.1: 索引 snap.links by ref — control 里 kind=link 的项可反查 href
            # (snapshot engine 把 <a href> 同时塞到 links[] 和 controls[kind=link],
            # 但 controls 没 href 字段. agent 看到 kind=link 但无 href 无法跳转.)
            link_hrefs: dict[str, str] = {l.ref: l.href for l in snap.links if l.ref}
            for link in snap.links:
                if link.ref and link.ref not in seen_refs:
                    refs.append({
                        "ref": link.ref, "kind": "link",
                        "text": link.text, "href": link.href,
                    })
                    seen_refs.add(link.ref)
            for ctrl_info in snap.controls:
                if ctrl_info.ref and ctrl_info.ref not in seen_refs:
                    entry: dict[str, Any] = {
                        "ref": ctrl_info.ref, "kind": ctrl_info.kind,
                        "text": ctrl_info.label, "input_name": ctrl_info.input_name,
                    }
                    # T64.1: kind=link 但 href 缺失时, 反查 snap.links by ref 兜底
                    if ctrl_info.kind == "link" and "href" not in entry:
                        # 先查 link_hrefs, 再用 text 模糊匹配
                        href = link_hrefs.get(ctrl_info.ref)
                        if not href and ctrl_info.label:
                            for l in snap.links:
                                if l.text and l.text.strip() == ctrl_info.label.strip():
                                    href = l.href
                                    break
                        if href:
                            entry["href"] = href
                    refs.append(entry)
                    seen_refs.add(ctrl_info.ref)
            result["refs"] = refs
            result["ref_count"] = len(refs)
        # 记录到 MemoryStore — 让 /history /graph /stats 能反映 daemon 的浏览.
        # 之前 daemon /open 不写 memory, 这些端点即使修了 AttributeError 也是空.
        try:
            self._record_to_memory(checked_url, snap, cls_result)
        except Exception:
            logger.exception("record_to_memory(%s) failed (non-fatal)", checked_url)
        return result

    def _record_to_memory(self, url: str, snap: Any, cls_result: dict[str, Any]) -> None:
        """把一次 open 的 snapshot + 分类结果落到 MemoryStore.

        镜像 SemanticBrowser._record_to_memory 的语义, 但 daemon 没有 engine 实例,
        所以在 daemon 层直接调 store. 失败不致命 — memory 是辅助查询, 不影响 open 主路径.
        """
        store = self.memory_store
        store.record_page(
            url=url,
            domain=snap.domain,
            title=snap.title,
            page_type=cls_result.get("page_type", "unknown"),
            confidence=float(cls_result.get("confidence", 0.0)),
            meta=snap.meta,
            snapshot_json=snap.to_json(),
        )
        store.record_links(
            from_url=url,
            links=[{"href": l.href, "text": l.text} for l in snap.links],
        )
        store.record_action(
            session_id=self.memory_session_id,
            action="open",
            url=url,
            detail=f"classified as {cls_result.get('page_type', 'unknown')}",
        )
        store.increment_page_visit(self.memory_session_id)

    async def _find(self, url: str, keyword: str, *, max_results: int = 10) -> dict[str, Any]:
        """服务端 find: open url → 提取 article → find_sections(keyword).

        镜像 SemanticBrowser.find 的语义. 复用 self._open (含 SSRF guard + 记忆),
        再拿当前 page 跑 ContentExtractor. 空 keyword 抛 ValueError → handler 转 400.
        """
        if not keyword or not keyword.strip():
            raise ValueError("keyword is empty; provide a non-empty keyword to search for")
        await self._open(url)
        from semantic_browser.extractor.content import ContentExtractor
        ctrl = await self.owner.aget_controller(None)
        page = ctrl.current_page
        if page is None:
            return {"keyword": keyword, "found": False, "sections": [], "total_sections": 0}
        article = await ContentExtractor(page).extract_article()
        if article is None:
            return {"keyword": keyword, "found": False, "sections": [], "total_sections": 0}
        sections = article.find_sections(keyword, max_results=max_results)
        return {
            "keyword": keyword,
            "found": bool(sections),
            "sections": sections,
            "total_sections": len(article.sections),
        }

    async def _extract_topic(self, url: str, keyword: str, *, max_chars: int = 4000) -> dict[str, Any]:
        """服务端 extract_topic: open url → 提取 article → extract_topic(keyword)."""
        if not keyword or not keyword.strip():
            raise ValueError("keyword is empty; provide a non-empty keyword to extract")
        await self._open(url)
        from semantic_browser.extractor.content import ContentExtractor
        ctrl = await self.owner.aget_controller(None)
        page = ctrl.current_page
        if page is None:
            return {"keyword": keyword, "found": False, "sections": [], "total_chars": 0, "section_count": 0}
        article = await ContentExtractor(page).extract_article()
        if article is None:
            return {"keyword": keyword, "found": False, "sections": [], "total_chars": 0, "section_count": 0}
        return article.extract_topic(keyword, max_chars=max_chars)

    def _get_visited_pages(self, domain: str = "", limit: int = 100) -> list[dict]:
        """历史查询 — domain 为空返所有 (按 visited_at 倒序)."""
        store = self.memory_store
        if domain:
            return store.get_pages_by_domain(domain, limit=limit)
        import sqlite3
        with store._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pages ORDER BY visited_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def _get_site_graph(self, root_url: str):
        """站点拓扑图 — 复用 GraphBuilder + MemoryStore."""
        return GraphBuilder(self.memory_store).build(root_url)

    def _get_llm_classifier(self) -> Any:
        """T63.2: lazy init LLMEnhancedClassifier — 仅在 OPENAI_API_KEY 配了才创建.
        没配 → 返回 None, _classify_with_cache 走纯启发式路径, 0 LLM 调用."""
        # 早期 fast-path: 没 KEY 直接 None, 不拿锁
        if not os.environ.get("OPENAI_API_KEY"):
            return None
        # 已有 → 复用
        if self._llm_classifier is not None:
            cached_key = os.environ.get("OPENAI_API_KEY", "")
            # KEY 中途变了 → 重置 (测试场景下可能改 env)
            if getattr(self._llm_classifier, "_last_api_key", None) != cached_key:
                self._llm_classifier = None
            else:
                return self._llm_classifier
        with self._llm_classifier_lock:
            if self._llm_classifier is not None:
                return self._llm_classifier
            from semantic_browser.classifier.llm_enhanced import LLMEnhancedClassifier
            clf = LLMEnhancedClassifier(threshold=0.5)
            clf._last_api_key = os.environ.get("OPENAI_API_KEY", "")  # type: ignore[attr-defined]
            self._llm_classifier = clf
            return clf

    async def _classify_with_cache(self, snap: Any,
                                    *, force: bool = False,
                                    strict: bool = False) -> tuple[dict[str, Any], str]:
        """T63.2: 三段式分类 — 启发式必跑, 命中缓存返 'cached',
        低置信度 + LLM 可用时跑 LLM, 失败/超时 silent 回启发式.

        T64: force=True 跳过缓存, 重跑分类 (content 变了或测试场景).
        T65.2: strict=True 时 LLM 失败 → 抛 _LLMUnavailableError, 不 silent
        fallback. agent 必须显式处理 (重试 / 切到启发式 / 拒分类).

        返回 (ClassificationResult.to_dict 风格 dict, source_label).
        """
        from semantic_browser.classifier.heuristic import PageClassifier
        # 1. 缓存命中 → 直接返 (按 URL 维度, agent 二次访问同 URL 秒返)
        #    force=True 时跳过 — 给 agent 强制重分类的口子
        cache_key = snap.url
        if not force and cache_key in self._classify_cache:
            self._classify_cache_hits += 1
            return self._classify_cache[cache_key], "cached"
        # 2. 启发式 (同步, < 1ms)
        heur = PageClassifier().classify(snap)
        heur_dict = self._normalize_confidence(heur.to_dict())
        # 高置信度 / LLM 不可用 → 直接用启发式结果
        clf = self._get_llm_classifier()
        if clf is None or heur.confidence >= 0.5:
            self._cache_put(cache_key, heur_dict)
            return heur_dict, "heuristic"
        # 3. LLM augment — T65.2: 直接调底层 _llm_classify, 绕过 wrapper 的 silent
        # fallback, 这样 server 端才能真正"看见" LLM 失败 (计数 + strict raise).
        try:
            llm_res = await clf._llm_classify(snap)
            if llm_res is None:
                # LLM 配置缺失 (key/url/model 任一空) — 走 heuristic 路径
                logger.warning("LLM not configured (key/url/model missing), "
                               "falling back to heuristic for %s", snap.url)
                self._cache_put(cache_key, heur_dict)
                return heur_dict, "heuristic"
            llm_dict = self._normalize_confidence(llm_res.to_dict())
            self._classify_llm_calls += 1
            self._cache_put(cache_key, llm_dict)
            return llm_dict, "llm"
        except Exception as e:  # noqa: BLE001
            self._classify_llm_failures += 1
            logger.warning("LLM classify failed for %s: %s", snap.url, e)
            # T65.2: strict mode 不允许 silent fallback, 让 agent 自己决定
            if strict:
                raise _LLMUnavailableError(
                    f"LLM classify failed for {snap.url}: {type(e).__name__}: {e}"
                ) from e
            self._cache_put(cache_key, heur_dict)
            return heur_dict, "heuristic"

    @staticmethod
    def _normalize_confidence(result: dict[str, Any]) -> dict[str, Any]:
        """T64: 启发式/LLM 偶返 conf=0.0 让 agent 误以为分类器坏了. 给个
        物理 floor: unknown=0.05 (承认不确定), 其他类型=0.10 (有结果但
        把握低). 真实高置信度 (>0.10) 完全不受影响."""
        conf = result.get("confidence", 0.0)
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        page_type = result.get("page_type", "unknown")
        floor = 0.05 if page_type == "unknown" else 0.10
        if conf < floor:
            result = dict(result)  # 不改原 dict 引用
            result["confidence"] = floor
        return result

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        """T63.2: LRU-ish 缓存 put — 超出 max 时清最早插入 (dict popitem
        在 3.7+ 是 FIFO 插入序). 实际 eviction 不严格, 只在超 cap 一步删一条,
        agent 长会话期内 cache 命中率高 (重复 /open 同一站 是常态)."""
        if len(self._classify_cache) >= self._classify_cache_max:
            try:
                self._classify_cache.popitem()  # FIFO eviction
            except KeyError:
                pass
        self._classify_cache[key] = value

    async def _snapshot(self, detail_level: str = "normal", session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        page = ctrl.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        return (await SnapshotEngine(page).capture(
            base_url=page.url, detail_level=detail_level,
        )).to_dict()

    async def _snapshot_vision(
        self,
        *,
        goal: str = "",
        provider: str | None = None,
        model: str | None = None,
        full_page: bool = True,
        session: str | None = None,
    ) -> dict[str, Any]:
        """T38: 截图 → vision LLM → 结构化描述."""
        from semantic_browser.snapshot.vision import capture_vision_snapshot
        ctrl = await self.owner.aget_controller(session)
        page = ctrl.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        vsnap = await capture_vision_snapshot(
            ctrl, goal=goal, provider=provider, model=model, full_page=full_page,
        )
        return vsnap.to_dict()

    async def _read(self, format: str = "markdown", session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        page = ctrl.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        from semantic_browser.extractor.content import ContentExtractor
        article = await ContentExtractor(page).extract_article()
        return {"format": format, "content": article.to_markdown() if format == "markdown" else article.to_dict()}

    async def _click(self, ref: str, session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        ok = await ctrl.click(ref)
        return {"success": ok, "url": await ctrl.get_url()}

    async def _type(self, ref: str, text: str, session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        ok = await ctrl.type_text(ref, text)
        return {"success": ok, "text_length": len(text)}

    async def _click_healed(self, ref: str) -> dict[str, Any]:
        return await self.owner.browser.controller.click_with_healing(ref)

    async def _type_healed(self, ref: str, text: str) -> dict[str, Any]:
        return await self.owner.browser.controller.type_with_healing(ref, text)

    async def _hover(self, ref: str) -> dict[str, Any]:
        ok = await self.owner.browser.controller.hover(ref)
        return {"success": ok, "ref": ref}

    async def _dblclick(self, ref: str) -> dict[str, Any]:
        ok = await self.owner.browser.controller.dblclick(ref)
        return {"success": ok, "ref": ref}

    async def _rightclick(self, ref: str) -> dict[str, Any]:
        ok = await self.owner.browser.controller.rightclick(ref)
        return {"success": ok, "ref": ref}

    async def _drag(self, from_ref: str, to_ref: str) -> dict[str, Any]:
        ok = await self.owner.browser.controller.drag(from_ref, to_ref)
        return {"success": ok, "from_ref": from_ref, "to_ref": to_ref}

    async def _select_option(self, ref: str, value: Any) -> dict[str, Any]:
        ok = await self.owner.browser.controller.select_option(ref, value)
        return {"success": ok, "ref": ref, "value": value}

    async def _fill_form(self, fields: dict[str, str]) -> dict[str, Any]:
        result = await self.owner.browser.controller.fill_form(fields)
        ok_count = sum(1 for v in result.values() if v)
        return {"results": result, "ok_count": ok_count, "total": len(result)}

    async def _with_retry(self, action_name: str, args: dict[str, Any], max_retries: int) -> dict[str, Any]:
        """T12: 用 retry 包装一个动作。action_name ∈ {open, click, type}"""
        ctrl = self.owner.browser.controller
        async def _do():
            if action_name == "open":
                await ctrl.open(args["url"])
                return {"ok": True, "url": args["url"]}
            if action_name == "click":
                ok = await ctrl.click(args["ref"])
                if not ok:
                    raise RuntimeError(f"click {args['ref']} failed")
                return {"ok": True, "ref": args["ref"]}
            if action_name == "type":
                ok = await ctrl.type_text(args["ref"], args["text"])
                if not ok:
                    raise RuntimeError(f"type {args['ref']} failed")
                return {"ok": True, "ref": args["ref"]}
            raise ValueError(f"unsupported retry action: {action_name!r}")
        result = await ctrl.with_retry(_do, max_retries=max_retries, what=action_name)
        return {**result, "retries": ctrl.retry_count}

    async def _set_files(self, ref: str, paths: list[str]) -> dict[str, Any]:
        return await self.owner.browser.controller.set_files(ref, paths)

    async def _download(self, trigger_ref: str | None, save_to: str | None, timeout_ms: int) -> dict[str, Any]:
        return await self.owner.browser.controller.download_file(
            trigger_ref=trigger_ref, save_to=save_to, timeout_ms=timeout_ms,
        )

    async def _scroll(self, direction: str, amount: int) -> dict[str, Any]:
        await self.owner.browser.controller.scroll(direction, amount)
        return {"direction": direction, "amount": amount}

    async def _wait_for_text(self, text: str, timeout_ms: int, in_selector: str) -> dict[str, Any]:
        ok = await self.owner.browser.controller.wait_for_text(text, timeout_ms=timeout_ms, in_selector=in_selector)
        return {"found": ok, "text": text, "timeout_ms": timeout_ms}

    async def _wait_for_ref(self, ref: str, timeout_ms: int) -> dict[str, Any]:
        ok = await self.owner.browser.controller.wait_for_ref(ref, timeout_ms=timeout_ms)
        return {"found": ok, "ref": ref, "timeout_ms": timeout_ms}

    async def _wait_for_url(self, pattern: str, timeout_ms: int) -> dict[str, Any]:
        ok = await self.owner.browser.controller.wait_for_url(pattern, timeout_ms=timeout_ms)
        return {"found": ok, "pattern": pattern, "url": await self.owner.browser.controller.get_url(),
                "timeout_ms": timeout_ms}

    async def _press(self, key: str) -> dict[str, Any]:
        await self.owner.browser.controller.press_key(key)
        return {"key": key}

    async def _back(self) -> dict[str, Any]:
        await self.owner.browser.controller.back()
        return await self._state()

    async def _forward(self) -> dict[str, Any]:
        await self.owner.browser.controller.forward()
        return await self._state()

    async def _screenshot(self, path: str | None) -> dict[str, Any]:
        data = await self.owner.browser.controller.screenshot(path=path)
        return {"path": path, "bytes": len(data)}

    async def _screenshot_annotated(self, path: str | None) -> dict[str, Any]:
        """带 ref 标签的截图: PNG base64 + sidecar (每个 ref 的 bbox+kind)."""
        import base64
        from semantic_browser.snapshot.annotate import (
            collect_refs_from_page, annotate_screenshot,
        )
        page = self.owner.browser.controller.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        png = await page.screenshot(path=path, full_page=False)
        refs = await collect_refs_from_page(page)
        annotated, sidecar = annotate_screenshot(png, refs)
        # 写文件 (如果指定了 path)
        if path:
            import os
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(annotated)
        return {
            "path": path,
            "bytes": len(annotated),
            "png_base64": base64.b64encode(annotated).decode("ascii"),
            "sidecar": sidecar,
        }

    async def _screenshot_sidecar(self) -> dict[str, Any]:
        """只拿 ref 元素位置信息 (不画图, 不传 PNG), 供 LLM plan."""
        from semantic_browser.snapshot.annotate import collect_refs_from_page
        page = self.owner.browser.controller.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        refs = await collect_refs_from_page(page)
        sidecar = {
            "image_size": [page.viewport_size["width"], page.viewport_size["height"]],
            "ref_count": len(refs),
            "visible_count": sum(1 for r in refs if r.visible),
            "refs": [
                {
                    "ref": r.ref, "kind": r.kind, "label": r.label,
                    "bbox": list(r.bbox),
                }
                for r in refs
            ],
        }
        return sidecar

    async def _save_state(self, path: str | None) -> dict[str, Any]:
        # T66.7.3 (C7): 显式 /state/save (vs auto_sweep) 也发审计 — 用户驱动
        # 的 storage state 保存是高价值事件, ops 想知道 "谁何时显式备份了".
        # 注意 _save_state 没接 session 参数 (用 default session, 跟 _open 一致).
        # BUG-FIX: 之前调 self.owner.browser.save_storage_state — _BrowserShim
        # 只暴露 .controller, 该方法在 controller 上. 自 T8 起的 latent bug,
        # T66.7.3 触发 audit event 才暴露.
        saved = await self.owner.browser.controller.save_storage_state(path)
        default_sid = _AsyncOwner.DEFAULT_SESSION
        idx = self.lease_manager.get_session_meta(default_sid)
        if idx is not None:
            tenant_id, agent_id = idx
        else:
            meta = self.owner.get_session_meta(default_sid) or {}
            tenant_id = meta.get("tenant_id", _AsyncOwner.DEFAULT_TENANT)
            agent_id = meta.get("agent_id", _AsyncOwner.DEFAULT_AGENT)
        try:
            import hashlib as _hl
            sz = 0
            try:
                sz = os.path.getsize(saved) if saved and os.path.exists(saved) else 0
            except OSError:
                pass
            self.event_bus.publish(
                "state.exported",
                {"session_id": default_sid, "path": saved, "size_bytes": sz,
                 "trigger": "user_explicit", "ts": time.time()},
                scope="session", scope_id=default_sid,
                tenant_id=tenant_id,
                producer_kind="agent", producer_id=agent_id,
                dedup_key=f"state_exported:{default_sid}:{int(time.time())}",
                persistent=True,
            )
        except Exception:
            logger.exception("save_state: failed to publish event")
        return {"path": saved}

    async def _tab_new(self, url: str) -> dict[str, Any]:
        page = await self.owner.browser.controller.new_tab(url)
        return {"index": self.owner.browser.controller.active_index,
                "url": page.url, "title": await page.title()}

    async def _tab_switch(self, index: int) -> dict[str, Any]:
        page = await self.owner.browser.controller.switch_tab(index)
        return {"index": index, "url": page.url, "title": await page.title()}

    async def _tab_close(self, index: int | None) -> dict[str, Any]:
        remaining = await self.owner.browser.controller.close_tab(index)
        active = self.owner.browser.controller.active_index
        return {"closed": index, "remaining": remaining, "active": active}

    async def _run_workflow(self, workflow_file: str) -> dict[str, Any]:
        from semantic_browser.workflow.runner import WorkflowRunner, load_workflow
        workflow = load_workflow(workflow_file)
        runner = WorkflowRunner(self.owner.browser.controller)
        result = await runner.run(workflow)
        return result.to_dict()

    async def _run_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.agent import GoalAgent
        # T31: 流式输出 — 把每步写到 SSE-like 行缓冲 (daemon 客户端按行读)
        progress_log: list[dict[str, Any]] = []
        async def on_step(record):
            entry = {
                "step": record.step, "action": record.action,
                "args": record.args, "success": record.success,
                "error": record.error,
            }
            progress_log.append(entry)
            # 写到 stderr (如果 stream=True) — 不影响 HTTP 响应体
            if args.get("stream"):
                import sys
                print(f"[step {record.step}] {record.action} "
                      f"{'✓' if record.success else '✗'} {entry['args']}",
                      file=sys.stderr, flush=True)
        agent = GoalAgent(
            self.owner.browser.controller,
            tier=args.get("tier", "smart"),
            max_steps=int(args.get("max_steps", 20)),
            use_smart_slicing=bool(args.get("use_smart_slicing", True)),
            use_failure_diagnostics=bool(args.get("use_failure_diagnostics", True)),
            on_step=on_step,
            allow_destructive=bool(args.get("allow_destructive", False)),
        )
        result = await agent.run(
            goal=args["goal"],
            start_url=args.get("start_url") or None,
        )
        out = result.to_dict()
        if args.get("stream"):
            out["progress"] = progress_log  # 额外字段, 客户端可校验
        return out

    async def _plan_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.agent import GoalAgent
        agent = GoalAgent(
            self.owner.browser.controller,
            tier=args.get("tier", "smart"),
        )
        return await agent.plan(
            goal=args["goal"],
            max_steps=int(args.get("max_plan_steps", 8)),
        )

    async def _discover(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.llm import discover, format_sitemap_for_llm
        result = await discover(
            self.owner.browser.controller,
            start_url=args["start_url"],
            max_pages=int(args.get("max_pages", 15)),
            max_depth=int(args.get("max_depth", 2)),
            same_domain_only=bool(args.get("same_domain_only", True)),
            delay_ms=int(args.get("delay_ms", 100)),
        )
        return {
            "root_url": result.root_url,
            "pages_visited": result.pages_visited,
            "pages_failed": [{"url": u, "error": e} for u, e in result.pages_failed],
            "flat_list": result.flat_list,
            "tree_text": result.tree_text,
            "llm_summary": format_sitemap_for_llm(result),
            "graph_dict": result.graph.to_dict(),
        }

    async def _run_semantic_query(self, args: dict[str, Any]) -> dict[str, Any]:
        """T67: /v1/query 处理器.

        接受 query 文本, 可选 start_url + budget + max_pages.
        内部跑 SemanticQuery 全流程 (plan → browse → relevance → synthesize).
        返 SemanticAnswer 的 dict 形式 + T70 request_id.
        """
        from semantic_browser.query import SemanticQuery

        query_text = (args.get("query") or args.get("goal") or "").strip()
        if not query_text:
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "MISSING_PARAM",
                    "message": "query is required",
                    "retryable": False,
                },
            }

        budget = args.get("budget")
        max_pages = args.get("max_pages")
        try:
            budget_int = int(budget) if budget is not None else None
            max_pages_int = int(max_pages) if max_pages is not None else None
        except (TypeError, ValueError):
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "INVALID_PARAM",
                    "message": f"budget/max_pages must be int (got budget={budget}, max_pages={max_pages})",
                    "retryable": False,
                },
            }

        # T70.15: 跟 SemanticQuery.__init__ 一致 — budget >= 1, max_pages >= 0
        if budget_int is not None and budget_int < 1:
            budget_int = 1   # 兜底防 client 错传 budget=0/-1
        if max_pages_int is not None:
            if max_pages_int < 0:
                max_pages_int = 1   # 兜底
            if max_pages_int > 5:
                max_pages_int = 5   # daemon 上限

        # T69: 共享 daemon-wide SemanticQuery (cache 跨请求持久)
        sq = self._semantic_query if self._semantic_query is not None else SemanticQuery()
        own_sq = self._semantic_query is None
        # T76: 记录 query 元数据 (滑动窗口, 100 条)
        request_id = _new_request_id()
        import time as _time
        log_entry: dict[str, Any] = {
            "request_id": request_id,
            "query": query_text[:200],  # 截断防止 log 太大
            "start_url": (args.get("start_url") or "")[:200],
            "budget": budget_int,
            "max_pages": max_pages_int,
            "started_at": _time.time(),
            "status": "running",
        }
        try:
            # T69: 并发 semaphore
            async with self._query_semaphore:
                answer = await sq.run(
                    query_text,
                    start_url=args.get("start_url") or None,
                    budget=budget_int,
                    max_pages=max_pages_int,
                )
            log_entry["status"] = "success" if answer.success else "failed"
            log_entry["confidence"] = answer.confidence
            log_entry["tokens_used"] = answer.tokens_used.get("used", {}).get("total", 0)
            log_entry["cache_hit"] = answer.tokens_used.get("cache_hit", False)
            log_entry["sources"] = list(answer.sources)[:5]
            log_entry["elapsed_s"] = answer.elapsed_s()
            # T81: Prometheus metrics — query-specific
            try:
                if answer.tokens_used.get("cache_hit"):
                    self.metrics.inc("query_cache_hits", {"path": "/v1/query"}, 1)
                else:
                    self.metrics.inc("query_cache_misses", {"path": "/v1/query"}, 1)
                tokens = answer.tokens_used.get("used", {}).get("total", 0)
                self.metrics.inc("query_tokens_used", {"path": "/v1/query"}, tokens)
                if answer.elapsed_s() is not None:
                    self.metrics.observe("query_duration_seconds", {"path": "/v1/query"}, answer.elapsed_s())
                if answer.confidence > 0:
                    self.metrics.observe("query_confidence", {"path": "/v1/query"}, answer.confidence)
            except Exception:
                pass
            # daemon 的 _dispatch 把返回值再包一层 Result envelope;
            # 这里返 inner 数据, 让外层包装成 {ok:True, data:{...answer...}}
            return {
                "request_id": args.get("_request_id") or _new_request_id(),
                "answer": answer.to_dict(),
            }
        except Exception as e:
            logger.exception("/v1/query failed")
            raise RuntimeError(f"QUERY_FAILED: {type(e).__name__}: {e}")[:300]
        finally:
            # T76: 记录到滑动窗口 (即使失败也记)
            try:
                async with self._query_log_lock:
                    self._query_log.append(log_entry)
            except Exception:
                pass
            # 只有当 sq 不是共享实例时才关 (避免把 daemon-wide 的 browser 也关了)
            if own_sq:
                try:
                    await sq.close()
                except Exception:
                    pass

    # T68+: /v1/query/stats — LLM 服务 + cache 配置
    async def _run_query_stats(self) -> dict[str, Any]:
        from semantic_browser.llm import get_default_service
        try:
            llm_stats = get_default_service().stats() if hasattr(get_default_service(), 'stats') else {}
        except Exception:
            llm_stats = {}
        # T69: 共享 SemanticQuery 的 cache_stats 现在是 daemon 进程级
        cache_stats = {}
        cache_health = {}
        if self._semantic_query is not None:
            try:
                cache_stats = self._semantic_query.cache_stats()
                # T90: cache health (warning / critical / ok / cold)
                cache_health = self._semantic_query.cache_health()
            except Exception:
                pass
        # T69: 并发 semaphore 状态
        semaphore_info = {
            "concurrency_limit": self._query_concurrency if hasattr(self, '_query_concurrency') else 4,
            "available_now": self._query_semaphore._value if hasattr(self, '_query_semaphore') else None,
        }
        return {
            "llm": llm_stats,
            "cache": cache_stats,
            "cache_health": cache_health,  # T90: warning/critical/ok/cold/disabled
            "concurrency": semaphore_info,
            "cache_persist_path": getattr(self, '_query_cache_path', None),
            "query_log_summary": {
                "total_logged": len(self._query_log),
                "recent_total": len(self._query_log),
            },
        }

    def _run_query_log(self, limit: int = 50) -> dict[str, Any]:
        """T76: 返最近 N 条 query log (滑动窗口, 默认 50, 上限 100)."""
        try:
            limit_int = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit_int = 50
        return {
            "limit": limit_int,
            "count": len(self._query_log),
            "entries": list(self._query_log)[-limit_int:],
        }

    def _stream_discover(self, req: BaseHTTPRequestHandler, args: dict[str, Any]) -> None:
        """T50: SSE 流式 discover — 每页/失败/done 一个 event.
        T55: 持久化到 Event Bus + Last-Event-ID 续传.

        Event 格式 (JSON, 一行):
          data: {"type": "start", "start_url": "...", ...}
          data: {"type": "page", "url": "...", "title": "...", "pages_done": N}
          data: {"type": "failure", "url": "...", "error": "..."}
          data: {"type": "done_result", "result": {...完整 result...}}

        实现要点:
        - 每 event 写入 event_bus (持久化) — SSE 帧带 `id: <seq>` 让 client 用 Last-Event-ID 续传
        - 同样的 event 写到 thread-safe live_queue, HTTP handler 读 live 推送
        - client 重连时: Last-Event-ID header → 从 bus replay + 然后接 live
        """
        import json as _json
        from semantic_browser.llm import discover, format_sitemap_for_llm

        start_url = args["start_url"]
        max_pages = int(args.get("max_pages", 15))
        max_depth = int(args.get("max_depth", 2))
        same_domain_only = bool(args.get("same_domain_only", True))
        delay_ms = int(args.get("delay_ms", 100))

        # T55: SSE 续传游标 (W3C Last-Event-ID)
        last_event_id = int(req.headers.get("Last-Event-ID", "0") or "0")

        # SSE headers
        req.send_response(200)
        req.send_header("content-type", "text/event-stream; charset=utf-8")
        req.send_header("cache-control", "no-cache")
        req.send_header("x-accel-buffering", "no")  # 禁用 nginx buffering
        req.send_header("connection", "keep-alive")
        req.end_headers()

        # T55: 先 replay 续传 (从 bus 读历史), 然后再接 live
        topic = f"discover.{start_url}"
        replayed = 0
        if last_event_id > 0:
            for ev in self.event_bus.replay(since_seq=last_event_id, topic=topic, limit=500):
                frame = (b"id: " + str(ev["seq"]).encode("utf-8") + b"\n"
                         + b"data: " + _json.dumps(ev["payload"], ensure_ascii=False).encode("utf-8") + b"\n\n")
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                    replayed += 1
                except (BrokenPipeError, ConnectionResetError):
                    return

        event_queue: queue.Queue = queue.Queue(maxsize=128)

        async def run_with_progress() -> None:
            async def progress_cb(event: dict) -> None:
                # T55: 同时 publish 到 bus (持久) + queue (live)
                seq = self.event_bus.publish(topic, event)
                event_queue.put_nowait({**event, "_seq": seq})

            try:
                result = await discover(
                    await self.owner.aget_controller(),
                    start_url=start_url,
                    max_pages=max_pages,
                    max_depth=max_depth,
                    same_domain_only=same_domain_only,
                    delay_ms=delay_ms,
                    progress_callback=progress_cb,
                )
                final = {
                    "root_url": result.root_url,
                    "pages_visited": result.pages_visited,
                    "pages_failed": [{"url": u, "error": e} for u, e in result.pages_failed],
                    "flat_list": result.flat_list,
                    "tree_text": result.tree_text,
                    "llm_summary": format_sitemap_for_llm(result),
                    "graph_dict": result.graph.to_dict(),
                }
                event_queue.put_nowait({"type": "_final", "result": final})
            except Exception as e:
                logger.exception("discover/stream failed")
                event_queue.put_nowait({"type": "_final", "result": {"_error": f"{type(e).__name__}: {e}"}})

        # 在 daemon 自己的 event loop 上调度 discover
        self.owner.loop.call_soon_threadsafe(
            asyncio.ensure_future, run_with_progress()
        )

        # HTTP handler 线程 (当前): 从 queue 读 event, 写 SSE 帧
        try:
            final_result: dict[str, Any] | None = None
            idle_ticks = 0
            while True:
                try:
                    event = event_queue.get(timeout=15)
                    idle_ticks = 0
                except queue.Empty:
                    idle_ticks += 1
                    # 超时 — 发 keepalive 防中间设备断连; 上限 4 次 (60s) 后放弃
                    if idle_ticks > 4:
                        logger.warning("SSE stream idle too long; closing")
                        break
                    try:
                        req.wfile.write(b": keepalive\n\n")
                        req.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                if event.get("type") == "_final":
                    final_result = event["result"]
                    break
                seq = event.pop("_seq", None)
                if seq is not None:
                    req.wfile.write(f"id: {seq}\n".encode("utf-8"))
                frame = b"data: " + _json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    logger.warning("SSE client disconnected mid-stream")
                    return

            done = {"type": "done_result"}
            if final_result and "_error" in final_result:
                done["error"] = final_result["_error"]
            elif final_result:
                done["result"] = final_result
            seq_final = self.event_bus.publish(topic, done)
            req.wfile.write(f"id: {seq_final}\n".encode("utf-8"))
            req.wfile.write(b"data: " + _json.dumps(done, ensure_ascii=False).encode("utf-8") + b"\n\n")
            req.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("SSE client disconnected")

    def _stream_agent_run(self, req: BaseHTTPRequestHandler, args: dict[str, Any]) -> None:
        """T53: SSE 流式 agent_run — 每 step 一个 event (复用 on_step 钩子).
        T55: 持久化到 Event Bus + Last-Event-ID 续传.

        Event 格式 (JSON 一行):
          data: {"type": "start", "goal": "...", "max_steps": N}
          data: {"type": "step", "step": N, "action": "...", ...}
          data: {"type": "done_result", "result": {完整 GoalResult.to_dict()}}
        """
        import json as _json
        from semantic_browser.agent import GoalAgent

        goal = args["goal"]
        max_steps = int(args.get("max_steps", 20))
        tier = args.get("tier", "smart")
        allow_destructive = bool(args.get("allow_destructive", False))
        start_url = args.get("start_url") or None

        # T55: SSE 续传游标
        last_event_id = int(req.headers.get("Last-Event-ID", "0") or "0")

        req.send_response(200)
        req.send_header("content-type", "text/event-stream; charset=utf-8")
        req.send_header("cache-control", "no-cache")
        req.send_header("x-accel-buffering", "no")
        req.send_header("connection", "keep-alive")
        req.end_headers()

        topic = f"agent_run.{goal[:50]}"
        # T55: replay 历史
        if last_event_id > 0:
            for ev in self.event_bus.replay(since_seq=last_event_id, topic=topic, limit=500):
                frame = (b"id: " + str(ev["seq"]).encode("utf-8") + b"\n"
                         + b"data: " + _json.dumps(ev["payload"], ensure_ascii=False).encode("utf-8") + b"\n\n")
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        event_queue: queue.Queue = queue.Queue(maxsize=128)
        loop_ref = self.owner.loop

        async def on_step(record):
            entry = {
                "type": "step",
                "step": record.step,
                "action": record.action,
                "args": record.args,
                "success": record.success,
                "error": record.error,
                "thought": record.thought,
            }
            try:
                seq = self.event_bus.publish(topic, entry)
                event_queue.put_nowait({**entry, "_seq": seq})
            except queue.Full:
                logger.warning("agent_run SSE queue full; dropping step %s", record.step)

        async def run_agent() -> None:
            try:
                agent = GoalAgent(
                    await self.owner.aget_controller(),
                    tier=tier,
                    max_steps=max_steps,
                    on_step=on_step,
                    allow_destructive=allow_destructive,
                )
                result = await agent.run(goal=goal, start_url=start_url)
                event_queue.put_nowait({"type": "_final", "result": result.to_dict()})
            except Exception as e:
                logger.exception("agent/run/stream failed")
                event_queue.put_nowait({"type": "_final", "result": {"_error": f"{type(e).__name__}: {e}"}})

        # start event — 先 publish 到 bus 拿 seq, 再写 SSE 帧
        start_payload = {"type": "start", "goal": goal, "max_steps": max_steps}
        seq_start = self.event_bus.publish(topic, start_payload)
        try:
            req.wfile.write(f"id: {seq_start}\n".encode("utf-8"))
            req.wfile.write(b"data: " + _json.dumps(start_payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
            req.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        loop_ref.call_soon_threadsafe(asyncio.ensure_future, run_agent())

        try:
            final_result: dict[str, Any] | None = None
            idle_ticks = 0
            while True:
                try:
                    event = event_queue.get(timeout=15)
                    idle_ticks = 0
                except queue.Empty:
                    idle_ticks += 1
                    if idle_ticks > 4:
                        logger.warning("agent_run SSE stream idle too long; closing")
                        break
                    try:
                        req.wfile.write(b": keepalive\n\n")
                        req.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                if event.get("type") == "_final":
                    final_result = event["result"]
                    break
                seq = event.pop("_seq", None)
                if seq is not None:
                    req.wfile.write(f"id: {seq}\n".encode("utf-8"))
                frame = b"data: " + _json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    logger.warning("agent_run SSE client disconnected mid-stream")
                    return

            done = {"type": "done_result"}
            if final_result and "_error" in final_result:
                done["error"] = final_result["_error"]
            elif final_result:
                done["result"] = final_result
            seq_final = self.event_bus.publish(topic, done)
            req.wfile.write(f"id: {seq_final}\n".encode("utf-8"))
            req.wfile.write(b"data: " + _json.dumps(done, ensure_ascii=False).encode("utf-8") + b"\n\n")
            req.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("agent_run SSE client disconnected")

    def _stream_events(self, req: BaseHTTPRequestHandler, args: dict[str, Any]) -> None:
        """T59: SSE stream of EventBus events — system.pressure + 全部 daemon.* topic.

        Args (query string):
          topics: 逗号分隔的 topic pattern 列表 (默认 "*" 全部). example: "system.*"
          since_seq: 可选, 起始游标 (int); 默认用 Last-Event-ID header (W3C SSE)

        Event 格式 (每 frame 一行 JSON data, 带 id):
          id: <seq>
          data: {"topic": "system.pressure", "payload": {...}, "ts": ..., "seq": ...}

        协议复用 T55 SSE 续传契约:
          1. client 不带 Last-Event-ID → 从 max_seq 重启 (默认收到启动后的事件)
          2. client 带 Last-Event-ID=N → 从 bus 重传 seq>N 的, 再接 live
          3. 中途断连 → 重连时带 Last-Event-ID (接续)

        实现:
          - bus.subscribe() 返回 asyncio.Queue; HTTP 线程不能 await 它
          - 套路: 在 daemon 自己的 event loop 上 schedule 一个 bridge task,
            从 asyncio.Queue.get() 拿到 record, 用 loop.call_soon_threadsafe
            通过 thread-safe Queue 投递给 HTTP handler
        """
        import json as _json

        topics_param = args.get("topics", "*") or "*"
        topic_patterns = [t.strip() for t in topics_param.split(",") if t.strip()] or ["*"]
        # T65.8: ?tenant_id= 过滤 — 多 agent 共享 daemon 时只收本租户的事件.
        # 默认 "anonymous" — 单 agent 场景不串就行
        tenant_filter = args.get("tenant_id") or None
        # Last-Event-ID header 是 W3C SSE 标准: 客户端最后收到的 event id.
        # 0 (=header 不存在或 "0") → client 想从开头收
        # N>0 → client 想从 seq>N 开始 (跳过已收的)
        last_event_id_hdr = req.headers.get("Last-Event-ID")
        if last_event_id_hdr is not None:
            # header 存在 — 显式 cursor (W3C spec)
            try:
                last_event_id = int(last_event_id_hdr.strip() or "0")
            except ValueError:
                last_event_id = 0
        else:
            # header 缺失 — 从 since_seq query param 或默认 0
            last_event_id = int(args.get("since_seq", 0) or 0)

        # SSE headers
        req.send_response(200)
        req.send_header("content-type", "text/event-stream; charset=utf-8")
        req.send_header("cache-control", "no-cache")
        req.send_header("x-accel-buffering", "no")
        req.send_header("connection", "keep-alive")
        req.end_headers()

        # 1) Replay historical events from bus (跨 / 重连 续传)
        replayed = 0
        # 仅在 Last-Event-ID header 存在 OR since_seq>0 时 replay;
        # 否则 (= 默认 0) 只接 live, 避免每次重连都从头刷一遍老事件
        since_param = int(args.get("since_seq", 0) or 0)
        if last_event_id_hdr is not None or since_param > 0:
            # 全部 topic 走 replay (top-level selector "*") — bus.replay 暂不支持 glob
            # 拿到所有 seq > last_event_id 的事件, payload 带 topic 可以过滤
            # 这里简化: 不过滤 (Replay 阶段给 caller 全部; live 再按 topic 过滤)
            for ev in self.event_bus.replay(since_seq=last_event_id, limit=500,
                                           tenant_id=tenant_filter):
                # topic 过滤 (若有 patterns 非 "*")
                if "*" not in topic_patterns:
                    ev_topic = ev["topic"]
                    if not any(_topic_matches_pattern(p, ev_topic) for p in topic_patterns):
                        continue
                frame = (b"id: " + str(ev["seq"]).encode("utf-8") + b"\n"
                         + b"data: " + _json.dumps(
                             {"topic": ev["topic"], "payload": ev["payload"],
                              "ts": ev["ts"], "seq": ev["seq"]},
                             ensure_ascii=False).encode("utf-8") + b"\n\n")
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                    replayed += 1
                except (BrokenPipeError, ConnectionResetError):
                    return
        logger.info("T59 /events: replayed=%d patterns=%s", replayed, topic_patterns)

        # 2) Live events — poll-based bridge (避免 asyncio.Queue 跨线程问题).
        # 每 0.2s 从 bus.replay(since_seq) 拉新事件, 经 bridge_q 投给 HTTP handler.
        # 上次最大 seq 持在 last_seq_box (用 list 包 mutable container);
        # bridge() 内 nonlocal 关键字不能用 (async def 不能直接 nonlocal from outer);
        # 改用 list 间接更新.
        bridge_q: queue.Queue = queue.Queue(maxsize=256)
        stop_bridge = [False]
        last_seq_box = [self.event_bus.max_seq]

        async def bridge() -> None:
            try:
                while not stop_bridge[0]:
                    events = self.event_bus.replay(since_seq=last_seq_box[0], limit=200,
                                            tenant_id=tenant_filter)
                    for ev in events:
                        # topic 过滤 (bus.replay 全部返回, 这里按 pattern 过滤)
                        if "*" not in topic_patterns:
                            ev_topic = ev["topic"]
                            if not any(_topic_matches_pattern(p, ev_topic) for p in topic_patterns):
                                last_seq_box[0] = max(last_seq_box[0], ev["seq"])
                                continue
                        try:
                            bridge_q.put_nowait(ev)
                        except queue.Full:
                            # 满: 丢最旧 (跟 bus 同样丢策略)
                            try:
                                bridge_q.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                bridge_q.put_nowait(ev)
                            except queue.Full:
                                pass
                        last_seq_box[0] = max(last_seq_box[0], ev["seq"])
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("events bridge crashed")

        # 在 daemon loop 上 schedule bridge task
        loop_ref = self.owner.loop
        future = asyncio.run_coroutine_threadsafe(bridge(), loop_ref)
        try:
            idle_ticks = 0
            while True:
                try:
                    record = bridge_q.get(timeout=15)
                    idle_ticks = 0
                except queue.Empty:
                    idle_ticks += 1
                    if idle_ticks > 4:  # 60s 没事件 — 关闭 (防 zombie 连接)
                        logger.info("T59 /events idle timeout; closing")
                        break
                    try:
                        req.wfile.write(b": keepalive\n\n")
                        req.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    continue
                # 写 SSE frame
                frame = (b"id: " + str(record["seq"]).encode("utf-8") + b"\n"
                         + b"data: " + _json.dumps(
                             {"topic": record["topic"], "payload": record["payload"],
                              "ts": record["ts"], "seq": record["seq"]},
                             ensure_ascii=False).encode("utf-8") + b"\n\n")
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            # 停 bridge task
            stop_bridge[0] = True
            try:
                future.cancel()
            except Exception:
                pass

    # ── T67 fix: 之前是 orphan methods (缩进错, 被 AST 当成 _topic_matches_pattern 的嵌套) ──
    # 现在移到类内, /llm/slice /llm/summarize /llm/extract /llm/find-ref 端点实际可用
    async def _llm_slice(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.llm import slice_refs_for_goal, get_default_service
        from semantic_browser.snapshot.engine import SnapshotEngine
        page = self.owner.browser.controller.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        engine = SnapshotEngine(page)
        snap = await engine.capture(base_url=page.url)
        useful = await slice_refs_for_goal(
            snap, args["goal"],
            max_refs=int(args.get("max_refs", 15)),
            llm=get_default_service(),
            tier=args.get("tier", "cheap"),
        )
        return {"useful_refs": useful, "count": len(useful)}

    async def _llm_summarize(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.llm import summarize_text, get_default_service
        summary = await summarize_text(
            args["text"],
            max_chars=int(args.get("max_chars", 500)),
            llm=get_default_service(),
            tier=args.get("tier", "cheap"),
        )
        return {"summary": summary}

    async def _llm_extract(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.llm import extract_fields, get_default_service
        fields = await extract_fields(
            args["text"], args["schema"],
            llm=get_default_service(),
            tier=args.get("tier", "cheap"),
        )
        return {"fields": fields}

    async def _llm_find_ref(self, args: dict[str, Any]) -> dict[str, Any]:
        from semantic_browser.llm import find_ref_by_label, get_default_service
        from semantic_browser.snapshot.engine import SnapshotEngine
        page = self.owner.browser.controller.current_page
        if page is None:
            raise ValueError("no active page; call /open first")
        engine = SnapshotEngine(page)
        snap = await engine.capture(base_url=page.url)
        ref = await find_ref_by_label(
            snap, args["description"],
            llm=get_default_service(),
            tier=args.get("tier", "cheap"),
        )
        return {"ref": ref}

    def _stream_semantic_query(self, req: BaseHTTPRequestHandler, args: dict[str, Any]) -> None:
        """T67+ T68: SSE 流式 /v1/query — 每 phase 一个 event.

        Event 格式 (JSON 一行):
          data: {"type":"start", "query": "...", ...}
          data: {"type":"phase", "phase":"plan_start|plan_done|browse_done|relevance_done|synth_done|...", ...}
          data: {"type":"final", "answer": {...完整 SemanticAnswer.to_dict()...}}
        """
        import json as _json
        from semantic_browser.query import SemanticQuery

        query_text = (args.get("query") or args.get("goal") or "").strip()
        if not query_text:
            req.send_response(400)
            req.send_header("content-type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": false, "error": {"code": "MISSING_PARAM", "message": "query is required"}}')
            return

        budget = args.get("budget")
        max_pages = args.get("max_pages")
        try:
            budget_int = int(budget) if budget is not None else None
            max_pages_int = int(max_pages) if max_pages is not None else None
        except (TypeError, ValueError):
            req.send_response(400)
            req.send_header("content-type", "application/json")
            req.end_headers()
            req.wfile.write(b'{"ok": false, "error": {"code": "INVALID_PARAM"}}')
            return
        # T70.15: 跟 SemanticQuery.__init__ 一致 — budget >= 1, max_pages >= 0
        if budget_int is not None and budget_int < 1:
            budget_int = 1   # daemon 兜底, 不让 client 错传导致崩溃
        if max_pages_int is not None:
            if max_pages_int < 0:
                max_pages_int = 1   # 同上兜底
            if max_pages_int > 5:
                max_pages_int = 5   # daemon 上限, 防滥用

        req.send_response(200)
        req.send_header("content-type", "text/event-stream; charset=utf-8")
        req.send_header("cache-control", "no-cache")
        req.send_header("x-accel-buffering", "no")
        req.send_header("connection", "keep-alive")
        req.end_headers()

        # write start frame (T70.10: 加 request_id 让 client 关联 events)
        start_entry = {
            "type": "start",
            "query": query_text,
            "budget": budget_int,
            "max_pages": max_pages_int,
            "request_id": _new_request_id(),
        }
        req.wfile.write(b"data: " + _json.dumps(start_entry, ensure_ascii=False).encode("utf-8") + b"\n\n")
        try:
            req.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        # bridge queue (thread-safe; thread from HTTP handler → asyncio loop)
        event_queue: queue.Queue = queue.Queue(maxsize=256)
        loop_ref = self.owner.loop

        def push_phase(entry: dict) -> None:
            """sync callback from SemanticQuery._record_step (在 asyncio loop 里执行)."""
            try:
                event_queue.put_nowait(entry)
            except queue.Full:
                logger.warning("/v1/query/stream queue full, drop")

        async def run_query() -> None:
            try:
                # T69+: 用 daemon-wide 共享实例 (cache 跨请求命中)
                own_sq = self._semantic_query is None
                sq = self._semantic_query if self._semantic_query is not None else SemanticQuery(on_phase=push_phase)
                try:
                    answer = await sq.run(
                        query_text,
                        start_url=args.get("start_url") or None,
                        budget=budget_int,
                        max_pages=max_pages_int,
                    )
                    event_queue.put_nowait({"type": "_final", "answer": answer.to_dict()})
                finally:
                    # 仅在 own_sq 时关 (避免关掉 daemon-wide 实例)
                    if own_sq:
                        try:
                            await sq.close()
                        except Exception:
                            pass
            except Exception as e:
                logger.exception("/v1/query/stream failed")
                event_queue.put_nowait({"type": "_final", "answer": {"_error": f"{type(e).__name__}: {e}"}})

        # schedule run_query on the daemon's loop
        future = asyncio.run_coroutine_threadsafe(run_query(), loop_ref)

        # pump events from queue to SSE stream until "_final"
        try:
            seq = 0
            while True:
                try:
                    entry = event_queue.get(timeout=30)
                except queue.Empty:
                    # keepalive
                    try:
                        req.wfile.write(b": keepalive\n\n")
                        req.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        future.cancel()
                        return
                    continue
                seq += 1
                if entry.get("type") == "_final":
                    # final event — include answer
                    final = {"type": "final", **entry}
                    frame = (b"id: " + str(seq).encode("utf-8") + b"\n"
                             + b"data: " + _json.dumps(final, ensure_ascii=False).encode("utf-8") + b"\n\n")
                else:
                    entry_with_type = {"type": "phase", **entry}
                    frame = (b"id: " + str(seq).encode("utf-8") + b"\n"
                             + b"data: " + _json.dumps(entry_with_type, ensure_ascii=False).encode("utf-8") + b"\n\n")
                try:
                    req.wfile.write(frame)
                    req.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    future.cancel()
                    return
                if entry.get("type") == "_final":
                    return
        finally:
            try:
                future.cancel()
            except Exception:
                pass


def _topic_matches_pattern(pattern: str, topic: str) -> bool:
    """T59 helper: pattern can be 'system.*' or exact 'system.pressure' or '*'."""
    if pattern == "*":
        return True
    if pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tb-daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--headed", action="store_true", help="show browser window")
    parser.add_argument("--state", help="Playwright storage_state JSON path")
    # T58: SSRF allowlist — comma-separated host patterns (*.example.com 也支持).
    # 生产不设 (默认 deny 私网); 测试 fixture 设 "data:,*testserver*" 之类的.
    parser.add_argument("--ssrf-allowlist", default="",
                        help="comma-separated allowlist (empty = default deny private/loopback/meta)")
    # T58: 测试 fixture 用 data: URL 时通过此 flag 临时允许; production 必为 False
    parser.add_argument("--allow-data-scheme", action="store_true",
                        help="T58: allow data: URLs (testing only, NEVER in production)")
    # T65.5: M×K 容量参数 (fable §1.2); 默认 6/16 是 16vCPU/64GB 推荐值
    # (评审 D7). 当前实现共享单 chromium, M 仅作 /capacity 字段暴露;
    # K 真正传给 ControllerPool (已硬编码在 _AsyncOwner).
    parser.add_argument("--m-browsers", type=int, default=6,
                        help="T65.5: M — browser 实例数 (fable §1.2 推荐 6, 64GB)")
    parser.add_argument("--k-contexts", type=int, default=16,
                        help="T65.5: K — 每实例 BrowserContext 上限 (fable §1.2 hard=16)")
    parser.add_argument("--watchdog-interval", type=float, default=5.0,
                        help="T60: 心跳 watchdog tick 间隔秒 (0=关闭)")
    parser.add_argument("--sweep-interval", type=float, default=60.0,
                        help="T65.1: snapshot sweeper + idle recycle 周期秒 (0=关闭)")
    parser.add_argument("--session-idle-timeout", type=float, default=300.0,
                        help="T65.1: session idle 自动回收秒数 (0=关闭, 默认 5min)")
    parser.add_argument("--lease-heartbeat-ttl-s", type=float, default=15.0,
                        help="T65.7: lease 默认 TTL (s), 客户端 1/3 TTL 续约一次")
    parser.add_argument("--drain-timeout", type=float, default=30.0,
                        help="T62: SIGTERM 后等在飞 op 完成的最长秒数 (fable §5.8)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # T49: 启动前检查 PID 文件 + 端口占用 — 比 OSError 早一步给清晰错误
    pid_file = _pid_path(args.port)
    stale = _check_stale_pid(pid_file)
    if stale:
        print(f"warning: removed stale PID file {pid_file} (pid {stale} not running)", file=__import__("sys").stderr)
    if _port_in_use(args.host, args.port):
        sys.exit(f"error: port {args.port} already in use on {args.host} (another daemon or process?)")

    # 写 PID 文件供 `tb daemon stop` 使用。start_new_session 让子进程独立, 不影响父进程。
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n{args.host}\n")

    daemon = TransparentBrowserDaemon(args.host, args.port, headless=not args.headed, storage_state_path=args.state,
        ssrf_allowlist=frozenset(p.strip() for p in args.ssrf_allowlist.split(",") if p.strip()),
        allow_data_scheme=args.allow_data_scheme,
        m_browsers=args.m_browsers, k_contexts=args.k_contexts,
        watchdog_interval_s=args.watchdog_interval,
        sweep_interval_s=args.sweep_interval,
        session_idle_timeout_s=args.session_idle_timeout,
        lease_heartbeat_ttl_s=args.lease_heartbeat_ttl_s,
        drain_timeout_s=args.drain_timeout,
    )

    # T49: 优雅关闭 — SIGTERM/SIGINT 触发 shutdown() 而不是 OS 默认退出 (会跳过 finally)
    import signal as _signal
    def _graceful(signum, frame):
        logger.warning("Received signal %d, shutting down", signum)
        daemon.shutdown()
    _signal.signal(_signal.SIGTERM, _graceful)
    _signal.signal(_signal.SIGINT, _graceful)

    try:
        daemon.serve_forever()
    finally:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


def _check_stale_pid(pid_file: Path) -> int | None:
    """PID 文件存在但进程已死 → 删. 返回死掉的 PID (供提示), 否则 None."""
    info = _read_pid_file(pid_file)
    if info is None:
        return None
    pid, _ = info
    if _pid_alive(pid):
        return None
    try:
        pid_file.unlink()
    except OSError:
        pass
    return pid


def _port_in_use(host: str, port: int) -> bool:
    """端口是否已被占用. 用 socket.bind 试, 失败即占用.

    SO_REUSEADDR=1 让 TIME_WAIT 状态的端口也能 bind 成功 (因为我们后续真起 daemon 也要 REUSEADDR),
    否则 daemon 死后立刻重启会误报 'port in use'.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


if __name__ == "__main__":
    main()
