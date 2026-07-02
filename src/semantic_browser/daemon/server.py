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
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from semantic_browser.result import classify_exception, err, ok
from urllib.parse import parse_qs, urlparse

from semantic_browser.engine import SemanticBrowser
from semantic_browser.snapshot.engine import SnapshotEngine
from semantic_browser.browser.controller import BrowserConfig, BrowserController
from semantic_browser.browser.pool import ControllerPool
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
    """

    DEFAULT_SESSION = "default"

    class _BrowserShim:
        """Backward-compat: 让 owner.browser.controller.X() 还能工作."""

        def __init__(self, controller: BrowserController) -> None:
            self.controller = controller

    def __init__(self, headless: bool = True, storage_state_path: str | None = None) -> None:
        import threading as _threading
        self.loop = asyncio.new_event_loop()
        self.config = BrowserConfig(
            headless=headless,
            storage_state_path=os.path.expanduser(storage_state_path) if storage_state_path else None,
        )
        # T54: 共享 chromium 进程 + 多 BrowserContext
        self.pool = ControllerPool(self.config, max_contexts=20)
        self.thread = threading.Thread(target=self._run_loop, name="tb-daemon-loop", daemon=True)
        self.thread.start()
        # T51: 浏览器操作串行化锁 (放在 owner 上, _acquire_op_lock_or_503 直接拿)
        self.op_lock = _threading.Lock()
        self.run(self.pool.start())
        # 预创建 default session — 保留 .browser 兼容旧代码
        default_ctrl = self.run(self.pool.acquire(self.DEFAULT_SESSION))
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
        """
        name = name or self.DEFAULT_SESSION
        return self.run(self.pool.acquire(name))

    async def aget_controller(self, name: str | None = None) -> BrowserController:
        """T54: async 版本 — 给已经在 event loop 上的 coroutine 用 (不会 deadlock)."""
        name = name or self.DEFAULT_SESSION
        return await self.pool.acquire(name)

    def run_coro(self, coro):
        """T55: 在 daemon event loop 上跑一个 coroutine (从 daemon 主线程调用)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=60)

    def list_sessions(self) -> list[str]:
        """T54: 列出所有活跃 session 名."""
        return self.pool.list_active()

    def release_session(self, name: str) -> bool:
        """T54: 关闭并移除指定 session. 返回是否真释放了一个."""
        if name == self.DEFAULT_SESSION:
            return False  # default 不能释放

        async def _release() -> bool:
            async with self.pool._lock:
                return self.pool._controllers.pop(name, None) is not None

        try:
            return self.run(_release())
        except Exception:
            return False


# T51: 串行化所有 controller 操作, 避免多 HTTP 线程并发改 page state.
# 注意: 浏览器单实例多线程不安全, controller 的 _page / current_page 是共享可变状态.
# asyncio loop 自己单线程串行执行 coroutine, 但 await 切点之间会交错,
# 多个 HTTP 请求都调 controller.open() 会同时 await page.goto(), 互相覆盖.
_OP_LOCK_TIMEOUT_S = 30.0  # 等锁超过 30s → 503 错; 长任务应主动拆小


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
    # T56: 降级错误码 (fable §5.9)
    "CAPACITY_DEGRADED": 503,    # L1: 拒新 session
    "DEGRADED_READONLY": 503,    # L3: 只读
    "SERVICE_UNAVAILABLE": 503,  # L4: 全拒
    "INTERNAL": 500,
}


class _SessionError(Exception):
    """T54: session 操作失败的业务异常 — 带 code 用于 HTTP 状态映射."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DegradationError(Exception):
    """T56: 降级阻挡的业务异常 — 当 daemon 处于降级状态时拒绝."""

    def __init__(self, code: str, message: str, level: int) -> None:
        super().__init__(message)
        self.code = code
        self.level = level


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
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, headless: bool = True, storage_state_path: str | None = None, event_bus_path: str | None = None) -> None:
        import time as _time
        self.host = host
        self.port = port
        self.started_at = _time.time()
        self.owner = _AsyncOwner(headless=headless, storage_state_path=storage_state_path)
        # T55: 持久化 Event Bus — SSE Last-Event-ID 续传 + 跨 SSE 状态共享
        self.event_bus = EventBus(event_bus_path)
        self.owner.run_coro(self.event_bus.start())
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
        self._capacity_max_contexts = 20  # 与 ControllerPool max_contexts 对齐

    def serve_forever(self) -> None:
        daemon = self
        self.httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(daemon))
        logger.warning("Transparent Browser daemon listening on http://%s:%d", self.host, self.port)
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """T49: 优雅关闭 — 停 http server + 关闭 browser + 删 PID 文件."""
        if self._shutting_down:
            return
        self._shutting_down = True
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
        # 删 PID 文件 (我们自己起的 daemon 才有)
        pid_file = _pid_path(self.port)
        try:
            if pid_file.exists() and pid_file.read_text().splitlines()[:1] == [str(os.getpid())]:
                pid_file.unlink()
        except OSError:
            pass

    # T51: 端点白名单 — 不需要 op_lock 的纯只读 / 元数据查询.
    # 其它端点 (open / click / snapshot / discover / etc.) 都串行化, 避免 controller 状态被覆盖.
    _NO_LOCK_PATHS = frozenset({"/health", "/queue", "/stats", "/capacity", "/metrics"})

    # T56: 降级检查触发点 — 写 op 在 L3+ 被拒, 全 op 在 L4 被拒
    _WRITE_OPS = frozenset({
        "/open", "/click", "/type", "/hover", "/dblclick", "/rightclick",
        "/drag", "/select-option", "/fill-form", "/set-files",
        "/scroll", "/press", "/download", "/back", "/forward", "/reload",
        "/agent/run", "/agent/run/stream", "/discover", "/discover/stream",
    })

    # T56: 降级时仍允许的只读/控制端点 (L4 全拒时除外)
    _DEGRADED_ALLOWED = frozenset({
        "/health", "/queue", "/stats", "/capacity", "/metrics",
        "/admin/degrade", "/admin/restore",
    })

    def _auto_degrade(self) -> None:
        """T56: 基于容量自动升降级 — 不经 Prometheus 回路 (fable §5.7).
        每请求调一次, 0ms 开销, 阈值用 capacity_ratio.
        只升不降 — 降级必须显式 /admin/restore, 防 admin bump 完被自动回落吃掉.
        """
        n = len(self.owner.list_sessions())
        max_ = self._capacity_max_contexts
        ratio = n / max(max_, 1)
        # 升级到 L1 (拒新 session)
        if ratio >= 0.85 and self._degradation_level < 1:
            self._degradation_level = 1
            logger.warning("DegradationController: auto-bumped to L1 (capacity_ratio=%.2f)", ratio)
        elif ratio >= 0.95 and self._degradation_level < 2:
            self._degradation_level = 2
            logger.warning("DegradationController: auto-bumped to L2 (capacity_ratio=%.2f)", ratio)
        # 不再自动降 — admin/restore 显式降到 L0

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

    def _handle(self, req: BaseHTTPRequestHandler, method: str) -> None:
        import time as _time
        parsed = urlparse(req.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        needs_lock = path not in self._NO_LOCK_PATHS
        started_at = _time.time()
        final_status = 200  # 假设成功, 异常分支会改
        final_code = ""     # T52: 失败时的 error.code
        # T56: 自动升降级 — 不抛异常, 只改 _degradation_level
        self._auto_degrade()
        try:
            # T56: 降级阻挡 — 在拿 op_lock 前就拒 (L4 情况连锁都不该争)
            self._enforce_degradation(method, path)
            body = self._read_json(req) if method == "POST" else {}
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
            if path not in ("/discover/stream", "/agent/run/stream"):
                self.metrics.observe("request_duration", {
                    "method": method, "path": path,
                }, elapsed)

    def _read_json(self, req: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(req.headers.get("content-length", "0") or "0")
        if length == 0:
            return {}
        raw = req.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

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
            import time as _time
            page_url = None
            try:
                # T49: 只读 current_page.url — 不要触发 _ensure_page 创建 about:blank
                # (会污染 state — 例如 test_state_no_page_returns_http_400)
                page = self.owner.browser.controller.current_page
                if page is not None and not page.is_closed():
                    page_url = page.url
            except Exception:
                pass
            return {
                "status": "ok",
                "pid": os.getpid(),
                "host": self.host,
                "port": self.port,
                "uptime_seconds": round(_time.time() - self.started_at, 1),
                "page_url": page_url,
            }
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
        if method == "GET" and path == "/capacity":
            sessions = self.owner.list_sessions()
            return {
                "sessions_active": len(sessions),
                "sessions_max": self._capacity_max_contexts,
                "capacity_ratio": round(len(sessions) / max(self._capacity_max_contexts, 1), 3),
                "degradation_level": self._degradation_level,
                "degradation_label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][self._degradation_level],
            }
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
            return {"level": level, "label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][level]}
        if method == "POST" and path == "/admin/restore":
            self._degradation_level = 0
            logger.info("DegradationController: admin restored to L0")
            return {"level": 0, "label": "L0_healthy"}
        # T54: session CRUD — list / create / delete
        if method == "GET" and path == "/sessions":
            sessions = self.owner.list_sessions()
            return {"sessions": sessions, "active_count": len(sessions)}
        if method == "POST" and path == "/sessions":
            name = args.get("name") or f"agent-{len(self.owner.list_sessions()) + 1}"
            try:
                _ = self.owner.get_controller(name)
            except Exception as e:
                raise _SessionError("SESSION_CREATE_FAILED", f"{type(e).__name__}: {e}") from None
            return {"name": name, "created": True, "active": self.owner.list_sessions()}
        if method == "DELETE" and path.startswith("/sessions/"):
            name = path[len("/sessions/"):]
            if not name:
                raise _SessionError("MISSING_PARAM", "session name required after /sessions/")
            if name == self.owner.DEFAULT_SESSION:
                raise _SessionError("CANNOT_DELETE_DEFAULT", "cannot delete default session")
            released = self.owner.release_session(name)
            if not released:
                raise _SessionError("SESSION_NOT_FOUND", f"session {name!r} not found")
            return {"name": name, "released": True, "active": self.owner.list_sessions()}
        if method == "POST" and path == "/open":
            return self.owner.run(self._open(args["url"], args.get("session")))
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
            return self.owner.run(self._run_agent(args))
        # T53: SSE 流式 agent run — 复用 on_step 钩子推 step-by-step
        if method == "POST" and path == "/agent/run/stream":
            if req is None:
                raise ValueError("/agent/run/stream requires req context")
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
            pages = self.owner.browser.get_visited_pages(args.get("domain", ""))
            return {"pages": pages, "count": len(pages)}
        if method == "GET" and path == "/graph":
            url = args.get("url") or self.owner.run(self.owner.browser.controller.get_url())
            return self.owner.browser.get_site_graph(url).to_dict()
        # T30: live site map discovery (vs /graph 走历史库)
        if method == "POST" and path == "/discover":
            return self.owner.run(self._discover(args))
        # T50: 流式版 — SSE (Server-Sent Events), 客户端可用 EventSource 消费
        if method == "GET" and path == "/discover/stream":
            if req is None:
                raise ValueError("/discover/stream requires req context")
            self._stream_discover(req, args)
            return "_SSE_HANDLED"
        if method == "POST" and path == "/find":
            url = args["url"]
            keyword = args["keyword"]
            max_results = int(args.get("max_results", 10))
            return self.owner.run(self.owner.browser.find(url, keyword, max_results=max_results))
        if method == "POST" and path == "/extract-topic":
            url = args["url"]
            keyword = args["keyword"]
            max_chars = int(args.get("max_chars", 4000))
            return self.owner.run(self.owner.browser.extract_topic(url, keyword, max_chars=max_chars))
        if method == "POST" and path == "/note":
            url = args["url"]
            note = args["note"]
            self.owner.browser.store.add_note(url, note)
            return {"saved": True, "url": url}
        if method == "GET" and path == "/stats":
            return self.owner.browser.store.stats()
        if method == "POST" and path == "/run-workflow":
            return self.owner.run(self._run_workflow(args["workflow_file"]))
        if method == "GET" and path == "/notes":
            url = args.get("url", "")
            limit = int(args.get("limit", 50))
            if url:
                rows = self.owner.browser.store.get_notes(url)[:limit]
                return {"count": len(rows), "notes": rows}
            with self.owner.browser.store._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
                notes_list = [dict(r) for r in rows]
            return {"count": len(notes_list), "notes": notes_list}
        raise ValueError(f"unknown endpoint: {method} {path}")

    async def _state(self, session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        return {"url": await ctrl.get_url(), "title": await ctrl.get_title()}

    async def _open(self, url: str, session: str | None = None) -> dict[str, Any]:
        ctrl = await self.owner.aget_controller(session)
        page = await ctrl.open(url)
        snap = await SnapshotEngine(page).capture(base_url=url)
        from semantic_browser.classifier.heuristic import PageClassifier
        cls = PageClassifier().classify(snap)
        return {"url": snap.url, "title": snap.title, "type": cls.page_type}

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
        saved = await self.owner.browser.save_storage_state(path)
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tb-daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--headed", action="store_true", help="show browser window")
    parser.add_argument("--state", help="Playwright storage_state JSON path")
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

    daemon = TransparentBrowserDaemon(args.host, args.port, headless=not args.headed, storage_state_path=args.state)

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
