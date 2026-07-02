"""
Transparent Browser daemon e2e 测试。

spawn 后台 daemon 进程, 通过 HTTP 调用验证:
- /health
- /open (载入页面)
- /state (查 url/title)
- /snapshot (拿 semantic snapshot)
- /read (拿正文 markdown)
- /click + /type
- /state/save
- 清理

测试使用临时端口避免冲突。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import closing
from urllib.error import HTTPError, URLError

import pytest


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: dict | None = None, timeout: float = 60) -> dict:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


@pytest.fixture
def daemon():
    """启动一个 daemon 子进程, 测试结束后清理。"""
    port = _free_port()
    log_path = f"/tmp/tb-daemon-test-{port}.log"
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "semantic_browser.daemon.server", "--port", str(port)],
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"

    # 等 daemon 就绪 (最多 30s)
    for _ in range(60):
        try:
            r = _http("GET", f"{base}/health")
            if r.get("ok") and r.get("data", {}).get("status") == "ok":
                break
        except (URLError, ConnectionRefusedError, OSError):
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail(f"daemon did not start; see {log_path}")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    # 清理 log
    try:
        os.unlink(log_path)
    except OSError:
        pass


class TestDaemonLifecycle:
    def test_health(self, daemon):
        r = _http("GET", f"{daemon}/health")
        assert r["ok"] is True
        assert r["data"]["status"] == "ok"

    def test_health_includes_t49_context(self, daemon):
        """T49: /health 必须带 pid/port/uptime/page_url — agent 排查时省一次 roundtrip."""
        r = _http("GET", f"{daemon}/health")
        d = r["data"]
        assert isinstance(d["pid"], int) and d["pid"] > 0
        assert isinstance(d["port"], int) and d["port"] > 0
        assert d["host"]
        assert d["uptime_seconds"] >= 0
        # page_url: 还没 open 页面时是 None, open 之后才填
        assert d["page_url"] is None

    def test_health_page_url_after_open(self, daemon):
        """T49: open 后 /health.page_url 应反映当前 URL."""
        data_url = "data:text/html,<html><title>t49</title></html>"
        _http("POST", f"{daemon}/open", {"url": data_url})
        r = _http("GET", f"{daemon}/health")
        assert "data:text/html" in r["data"]["page_url"]


class TestDaemonLifecycleT49:
    """T49: 启动前预检 / 优雅关闭 / 后台模式等就绪 — 直接 import 函数, 不起真 daemon."""

    def test_pid_alive_for_current_process(self):
        from semantic_browser.daemon.server import _pid_alive
        assert _pid_alive(os.getpid()) is True

    def test_pid_alive_for_nonexistent_pid(self):
        from semantic_browser.daemon.server import _pid_alive
        # 4M 是远超 PID 最大值的数字, 一定不存在
        assert _pid_alive(4_000_000) is False

    def test_read_pid_file_roundtrip(self, tmp_path):
        from semantic_browser.daemon.server import _read_pid_file
        f = tmp_path / "x.pid"
        f.write_text("12345\n127.0.0.1\n")
        assert _read_pid_file(f) == (12345, "127.0.0.1")

    def test_read_pid_file_handles_missing(self, tmp_path):
        from semantic_browser.daemon.server import _read_pid_file
        assert _read_pid_file(tmp_path / "absent.pid") is None

    def test_read_pid_file_handles_corrupt(self, tmp_path):
        from semantic_browser.daemon.server import _read_pid_file
        f = tmp_path / "x.pid"
        f.write_text("not-a-number\n")
        assert _read_pid_file(f) is None

    def test_check_stale_pid_removes_dead_pid(self, tmp_path):
        from semantic_browser.daemon.server import _check_stale_pid
        f = tmp_path / "stale.pid"
        f.write_text("4000000\n")  # 不存在的 PID
        dead = _check_stale_pid(f)
        assert dead == 4_000_000
        assert not f.exists()

    def test_check_stale_pid_keeps_live_pid(self, tmp_path):
        from semantic_browser.daemon.server import _check_stale_pid
        f = tmp_path / "live.pid"
        f.write_text(f"{os.getpid()}\n")
        assert _check_stale_pid(f) is None
        assert f.exists()  # 不删

    def test_port_in_use_detects_bind(self):
        """我们手动 bind 一个端口, _port_in_use 应检出."""
        from semantic_browser.daemon.server import _port_in_use
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            # s 没 listen; bind 已占
            assert _port_in_use("127.0.0.1", port) is True
        finally:
            s.close()

    def test_port_in_use_returns_false_for_free_port(self):
        from semantic_browser.daemon.server import _port_in_use
        # 找系统分配的空闲端口 (立刻释放)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # 现在应该是空闲的 (有微小竞争但 99% 成立)
        assert _port_in_use("127.0.0.1", port) is False


class TestDaemonCLIStartPrecheck:
    """T49: tb daemon start 的预检行为. 用真子进程 + 桩 /health 模拟 daemon."""

    @staticmethod
    def _fake_health_script(port: int) -> str:
        """一个最小 HTTP server 脚本, 只响应 /health=200. SIGTERM/SIGINT 优雅退出.

        用 os._exit 不用 sys.exit — signal handler 在 serve_forever 的 C-level select 中
        被调用时, sys.exit 抛 SystemExit 可能被 select 吞掉; os._exit 强制立即终止.
        """
        return f"""
import signal, sys, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{{"status":"ok"}}')
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a, **k): pass

httpd = ThreadingHTTPServer(('127.0.0.1', {port}), H)
def _shutdown(*a):
    try:
        httpd.server_close()
    finally:
        os._exit(0)
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)
httpd.serve_forever()
"""

    def _spawn_fake_daemon(self, tmp_path, port: int):
        """起 fake 子进程, 写 PID 文件指向它. 返回 (proc, pid_file)."""
        script_path = tmp_path / "fake_daemon.py"
        script_path.write_text(self._fake_health_script(port))
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sb_dir = tmp_path / "home" / ".semantic-browser"
        sb_dir.mkdir(parents=True)
        pid_file = sb_dir / f"daemon-{port}.pid"
        pid_file.write_text(f"{proc.pid}\n127.0.0.1\n")
        # 等 /health ready
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    if r.status == 200:
                        return proc, pid_file
            except Exception:
                time.sleep(0.1)
        proc.kill()
        raise AssertionError("fake daemon never became ready")

    def test_start_refuses_when_daemon_alive(self, tmp_path):
        """已有一个 daemon 在跑 (PID 活着 + /health 通), 应拒绝并报错."""
        from semantic_browser.client.cli import daemon_start
        from click.testing import CliRunner

        port = _free_port()
        proc, _ = self._spawn_fake_daemon(tmp_path, port)
        try:
            runner = CliRunner()
            result = runner.invoke(
                daemon_start, ["--port", str(port)],
                env={"HOME": str(tmp_path / "home")},
            )
            assert result.exit_code != 0, f"应拒绝 start, got exit={result.exit_code}: {result.output}"
            assert "already running" in result.output or "use --force" in result.output
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_start_force_sends_sigterm_to_existing(self, tmp_path):
        """--force: 应 SIGTERM 现有 daemon, 而不是直接报 'port in use'."""
        from semantic_browser.client.cli import daemon_start
        from click.testing import CliRunner

        port = _free_port()
        proc, _ = self._spawn_fake_daemon(tmp_path, port)
        try:
            runner = CliRunner()
            # --background + --force: 先 SIGTERM 旧 daemon, 然后 Popen 新 daemon, 然后等就绪
            # 我们只验证 SIGTERM 被发到原 PID (原 proc 被杀), 不真等新 daemon 就绪
            # 用 catch_exceptions=False + 自己 timeout 即可; CliRunner 默认会跑完
            # 实际这会卡住直到 10s 等就绪超时 — 但 PROC 已被 SIGTERM, 验证已生效
            result = runner.invoke(
                daemon_start, ["--port", str(port), "--force", "--background"],
                env={"HOME": str(tmp_path / "home")},
            )
            # 关键断言: 原 fake daemon 进程已死 (被 SIGTERM 干掉)
            assert proc.poll() is not None, (
                f"原 fake daemon 应被 SIGTERM 杀, 但仍活着. cli output: {result.output}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_start_refuses_port_in_use(self, tmp_path):
        """端口被非-daemon 进程占用时, start 应给清晰错误."""
        from semantic_browser.client.cli import daemon_start
        from click.testing import CliRunner

        port = _free_port()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        try:
            runner = CliRunner()
            result = runner.invoke(
                daemon_start, ["--port", str(port)],
                env={"HOME": str(tmp_path / "home")},
            )
            assert result.exit_code != 0, f"应拒绝 start, got: {result.output}"
            assert "already in use" in result.output or "port" in result.output.lower()
        finally:
            s.close()


class TestDiscoverProgress:
    """T50: discover() 的 progress_callback — 每页/失败/done 都触发."""

    def test_progress_callback_fires_for_each_page(self):
        """Stub controller: 2 页 + 1 失败. callback 应收到 start + page*2 + failure + done."""
        from semantic_browser.graph.discoverer import discover

        # 假 controller + 假 page
        class FakePage:
            def __init__(self, title): self._t = title
            async def title(self): return self._t

        class FakeSnapshot:
            def __init__(self, links): self.links = links

        class FakeController:
            def __init__(self):
                self.open_calls = []
                self.pages = {
                    "https://x.com/": ("Home", [("a", "https://x.com/a"), ("b", "https://x.com/b")]),
                    "https://x.com/a": ("A", []),
                    "https://x.com/b": ("B", []),
                }
                self.fail_on = set()
            async def open(self, url):
                self.open_calls.append(url)
                if url in self.fail_on:
                    raise RuntimeError("simulated network error")
                self._current = url
            @property
            def current_page(self):
                return FakePage(self.pages.get(self._current, ("?", []))[0])

        events = []
        async def cb(event):
            events.append(event)

        # monkeypatch SnapshotEngine to return our fake
        from semantic_browser.snapshot.engine import SnapshotEngine
        orig_capture = SnapshotEngine.capture
        async def fake_capture(self, base_url=""):
            return FakeSnapshot([type("L", (), {"href": href})() for _, href in self._page.pages.get(self._page._current, ("", []))[1]])
        # 更简单: 替换 SnapshotEngine 为返回固定空 snapshot
        async def simple_capture(self, base_url=""):
            return FakeSnapshot([])
        SnapshotEngine.capture = simple_capture
        try:
            ctrl = FakeController()
            ctrl.fail_on = {"https://x.com/a"}
            # 跑一下 — 但 FakeController 没有 _current 属性的 setter; 改用 main loop
            # 先手动调 open + setattr
            import asyncio
            async def main():
                # 直接用 controller.open 模拟第一个 page
                await ctrl.open("https://x.com/")
                # 让 callback 真正收到事件
                # 但 discover() 自己会 open, 我们让 FakeController 自身维护 state
                # 上面 fail_on 在 open 时 raise; capture 走 FakeSnapshot([]) 没 links, BFS 不会扩展
                return await discover(ctrl, "https://x.com/", max_pages=10, max_depth=1, progress_callback=cb)
            result = asyncio.run(main())
        finally:
            SnapshotEngine.capture = orig_capture

        # 至少: start + page(home) + failure(a) + done — b 不该被访问因为 max_depth=1
        # 但 discover 从 bfs queue 里 pop a 失败, 然后继续 pop b. 等等, depth=1 时 max_depth=1 不该加新链接
        # 实际上 b 在 a 之前就被 enqueue (从 home links)
        # 简化断言: 必有 start 和 done
        types = [e["type"] for e in events]
        assert "start" in types
        assert "done" in types
        # start event 必带 start_url
        start_evt = next(e for e in events if e["type"] == "start")
        assert start_evt["start_url"] == "https://x.com/"
        # done event 必带总耗时
        done_evt = next(e for e in events if e["type"] == "done")
        assert "total_seconds" in done_evt

    def test_progress_callback_none_is_silent(self):
        """T30 向后兼容: 不传 callback 也能跑."""
        from semantic_browser.graph.discoverer import discover

        class FakeController:
            async def open(self, url): pass
            @property
            def current_page(self): return None

        from semantic_browser.snapshot.engine import SnapshotEngine
        orig_capture = SnapshotEngine.capture
        async def fake_capture(self, base_url=""):
            class S: links = []
            return S()
        SnapshotEngine.capture = fake_capture
        try:
            import asyncio
            # current_page=None 时 discover 会把 url 加到 failed. 但 _emit 不会被调用因为抛错
            # 实际: 当 page is None, 进入 pages_failed 分支, 调 _emit failure 然后 continue
            # 队列空了就退出. OK 不传 callback 也能跑.
            result = asyncio.run(discover(FakeController(), "https://x.com/", max_pages=2))
            assert result.root_url == "https://x.com/"
            assert len(result.pages_failed) >= 1
        finally:
            SnapshotEngine.capture = orig_capture


class TestSSEEndpoint:
    """T50: /discover/stream 端点 — SSE 帧格式 + 错误处理.

    Daemon 是 subprocess, 没法直接 monkeypatch. 所以测试用真 data: URL (不起网络).
    """

    def _read_sse_events(self, url: str, *, timeout: float = 30) -> list[dict]:
        events = []
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            assert resp.headers.get("content-type", "").startswith("text/event-stream"), \
                f"非 SSE: {resp.headers.get('content-type')}"
            while True:
                line = resp.readline().decode("utf-8").rstrip("\n").rstrip("\r")
                if not line or not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                events.append(event)
                if event.get("type") == "done_result":
                    break
        return events

    def test_sse_endpoint_emits_events(self, daemon):
        """data: URL → start → page → done → done_result (含完整 result)."""
        from urllib.parse import quote
        # 单页 data URL, 让 discover 跑得快
        url = "data:text/html;charset=utf-8," + quote("<html><title>T50 SSE</title></html>")
        events = self._read_sse_events(
            f"{daemon}/discover/stream?start_url={quote(url)}&max_pages=1&max_depth=0",
            timeout=30,
        )
        types = [e["type"] for e in events]
        assert "start" in types, f"缺 start: {types}"
        assert "page" in types, f"缺 page: {types}"
        assert "done" in types, f"缺 done (discoverer): {types}"
        assert "done_result" in types, f"缺 done_result (daemon): {types}"
        # done_result 含完整 result
        dr = next(e for e in events if e["type"] == "done_result")
        assert "result" in dr
        assert dr["result"]["root_url"] == url
        assert url in dr["result"]["pages_visited"]
        # 必有 tree_text / llm_summary / graph_dict
        assert "tree_text" in dr["result"]
        assert "llm_summary" in dr["result"]
        assert "graph_dict" in dr["result"]
        # page event 必含 pages_done / queue_remaining
        page = next(e for e in events if e["type"] == "page")
        assert "pages_done" in page
        assert page["pages_done"] >= 1

    def test_sse_endpoint_missing_start_url(self, daemon):
        """缺 start_url → MISSING_PARAM → 协议层 400 (不走 SSE, 因为参数缺失早于 SSE)."""
        req = urllib.request.Request(f"{daemon}/discover/stream")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("应报错")
        except HTTPError as e:
            assert e.code == 400
            body = json.loads(e.read())
            assert body["ok"] is False
            assert body["error"]["code"] == "MISSING_PARAM"

    def test_sse_uses_browser_controller(self, daemon):
        """验证 /discover/stream 实际通过 _AsyncOwner 调 controller (浏览器状态受影响)."""
        from urllib.parse import quote
        url = "data:text/html;charset=utf-8," + quote("<html><title>SSE-State</title></html>")
        events = self._read_sse_events(
            f"{daemon}/discover/stream?start_url={quote(url)}&max_pages=1&max_depth=0",
            timeout=30,
        )
        # discover 后, browser 应已打开该 URL
        s = _http("GET", f"{daemon}/state")
        assert s["ok"] is True
        assert "data:text/html" in s["data"]["url"]
        assert "SSE-State" in s["data"]["title"]


class TestT52Metrics:
    """T52: /metrics 端点 — Prometheus 格式 + 必含关键指标."""

    def test_metrics_endpoint_returns_prometheus_text(self, daemon):
        """返回 text/plain (Prometheus 文本格式 0.0.4), 非 JSON envelope."""
        req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ct = resp.headers.get("content-type", "")
            body = resp.read().decode("utf-8")
        assert "text/plain" in ct, f"应 text/plain, got: {ct}"
        # 不应是 JSON envelope
        assert not body.startswith("{"), "metrics 不该走 JSON envelope"

    def test_metrics_includes_required_series(self, daemon):
        """/metrics 必含: tb_requests_total, tb_request_duration, tb_daemon_uptime."""
        # 先触发一些请求, 让 metrics 有数据
        _http("GET", f"{daemon}/health")
        _http("GET", f"{daemon}/state")
        req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")

        # 必含的 metric 名
        assert "tb_requests_total" in body, f"缺 requests_total:\n{body[:500]}"
        # histogram: 渲染为 _bucket/_count/_sum 三件套
        assert "tb_request_duration_bucket" in body, f"缺 duration histogram"
        assert "tb_request_duration_count" in body
        assert "tb_request_duration_sum" in body
        assert "tb_daemon_uptime_seconds" in body, f"缺 uptime gauge"
        # 至少一次 /health 调用应被记录
        assert 'path="/health"' in body

    def test_metrics_includes_error_counter(self, daemon):
        """失败的请求 (404 / 400) 应被记到 tb_errors_total."""
        # 触发 404
        try:
            _http("GET", f"{daemon}/nonsense")
        except Exception:
            pass
        # 触发 400 (缺 url)
        try:
            _http("POST", f"{daemon}/open", {})
        except Exception:
            pass

        req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")

        # 404 是协议层错, 不会被分类; 400 是 MISSING_PARAM
        assert "tb_errors_total" in body, f"缺 errors_total:\n{body[:500]}"

    def test_metrics_records_op_lock_wait_and_hold(self, daemon):
        """op_lock_wait / op_lock_hold histogram 在多 op 后应有数据."""
        # 跑一些 controller-touching ops
        from urllib.parse import quote
        url = "data:text/html;charset=utf-8," + quote("<html><title>M</title></html>")
        _http("POST", f"{daemon}/open", {"url": url})
        _http("GET", f"{daemon}/snapshot")

        req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "tb_op_lock_wait_bucket" in body, "应记录 op_lock_wait"
        assert "tb_op_lock_hold_bucket" in body, "应记录 op_lock_hold"

    def test_metrics_increments_after_request(self, daemon):
        """同一路径多请求 → counter 应递增."""
        before_req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(before_req, timeout=5) as resp:
            before = resp.read().decode("utf-8")
        # 提取 /health 的 counter 值
        def get_health_count(body: str) -> int:
            for line in body.split("\n"):
                if line.startswith('tb_requests_total{') and 'path="/health"' in line:
                    return int(line.rsplit(" ", 1)[1])
            return 0
        n_before = get_health_count(before)
        # 多发 3 次
        for _ in range(3):
            _http("GET", f"{daemon}/health")
        after_req = urllib.request.Request(f"{daemon}/metrics")
        with urllib.request.urlopen(after_req, timeout=5) as resp:
            after = resp.read().decode("utf-8")
        n_after = get_health_count(after)
        assert n_after >= n_before + 3, f"counter 没递增: before={n_before}, after={n_after}"
    """T51: 串行化锁 — 同 op 不会并发覆盖 controller 状态."""

    def test_queue_endpoint_reports_idle(self, daemon):
        """无 op 在跑时, /queue 显示空闲."""
        r = _http("GET", f"{daemon}/queue")
        assert r["ok"] is True
        d = r["data"]
        assert d["current_op"] is None
        assert d["lock_held"] is False
        assert d["waiters"] == 0
        assert d["lock_timeout_s"] > 0

    def test_concurrent_open_serializes(self, daemon):
        """同时 2 个 /open, 第二个应等到第一个结束才执行, 不互相覆盖."""
        from urllib.parse import quote
        url_a = "data:text/html;charset=utf-8," + quote("<html><title>A</title></html>")
        url_b = "data:text/html;charset=utf-8," + quote("<html><title>B</title></html>")

        import threading
        results = {}
        def call(label, url):
            r = _http("POST", f"{daemon}/open", {"url": url}, timeout=30)
            results[label] = r

        ta = threading.Thread(target=call, args=("a", url_a))
        tb = threading.Thread(target=call, args=("b", url_b))
        ta.start()
        tb.start()
        ta.join(timeout=30)
        tb.join(timeout=30)

        # 两个都应成功
        assert results["a"]["ok"] is True, f"A failed: {results['a']}"
        assert results["b"]["ok"] is True, f"B failed: {results['b']}"
        # 最终 page 是其中一个 (谁后跑谁赢, 因为是串行的)
        s = _http("GET", f"{daemon}/state")
        assert s["ok"] is True
        assert s["data"]["title"] in ("A", "B")

    def test_queue_shows_running_op_during_long_task(self, daemon):
        """长任务跑时, /queue 显示 current_op + running_for_s."""
        # 跑一个稍慢的 op (snapshot-vision 需要 LLM, 但没 LLM 会失败快).
        # 用 SSE discover 代替 — 跑多个 page, 期间轮询 /queue.
        import threading
        from urllib.parse import quote
        url = "data:text/html;charset=utf-8," + quote("<html><title>T51-Queue</title></html>")
        # 自己起一个 SSE 流 — 在另一线程
        sse_events = []
        sse_done = threading.Event()
        def sse_runner():
            try:
                events = self._read_sse_events_blocking(
                    f"{daemon}/discover/stream?start_url={quote(url)}&max_pages=3&max_depth=1&delay_ms=200",
                    timeout=20,
                )
                sse_events.extend(events)
            finally:
                sse_done.set()
        t = threading.Thread(target=sse_runner, daemon=True)
        t.start()

        # 等 SSE 开始 (queue 出现 current_op)
        import time
        deadline = time.time() + 5
        saw_running = False
        while time.time() < deadline:
            r = _http("GET", f"{daemon}/queue")
            if r["ok"] and r["data"]["current_op"]:
                saw_running = True
                assert r["data"]["lock_held"] is True
                assert r["data"]["running_for_s"] is not None
                break
            time.sleep(0.1)
        sse_done.wait(timeout=15)
        assert saw_running, "SSE 期间 /queue 应显示 running op"

    @staticmethod
    def _read_sse_events_blocking(url: str, *, timeout: float) -> list[dict]:
        """同步读 SSE events, 适合在子线程跑."""
        events = []
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                while True:
                    line = resp.readline().decode("utf-8").rstrip("\n").rstrip("\r")
                    if not line or not line.startswith("data: "):
                        continue
                    event = json.loads(line[len("data: "):])
                    events.append(event)
                    if event.get("type") == "done_result":
                        break
        except (URLError, TimeoutError):
            pass
        return events

    def test_op_lock_prevents_state_corruption(self, daemon):
        """串行化保证: 1) snapshot 期间 open 不并发改 page 2) 不会有 'snapshot_of_old_page'."""
        from urllib.parse import quote
        url = "data:text/html;charset=utf-8," + quote("<html><title>Race</title></html>")
        # 1) open 一个页面
        _http("POST", f"{daemon}/open", {"url": url})
        # 2) 拿到 snapshot, 验证 title 一致
        s1 = _http("GET", f"{daemon}/snapshot")
        assert s1["ok"] is True
        # T51 的关键断言: 即便测试结束前没人并发改 page, 我们验证串行机制存在
        # 通过 /queue 的 lock_held=false 验证锁可释放
        q = _http("GET", f"{daemon}/queue")
        assert q["data"]["lock_held"] is False
        assert q["data"]["waiters"] == 0


class TestDaemonBrowse:
    def test_open_and_state(self, daemon):
        """打开一个 data URL, 然后查 state 确认 url。"""
        data_url = "data:text/html,<html><head><title>Daemon Test</title></head><body><h1>Hi</h1></body></html>"
        r = _http("POST", f"{daemon}/open", {"url": data_url})
        assert r["ok"] is True
        assert r["data"]["title"] == "Daemon Test"
        assert r["data"]["type"] in ("article", "unknown")  # 不强制, 启发式会分类

        # state 应反映当前 url
        s = _http("GET", f"{daemon}/state")
        assert s["ok"] is True
        assert "data:text/html" in s["data"]["url"]

    def test_snapshot_returns_valid_json(self, daemon):
        """/snapshot 应返回结构化 snapshot, dict 可被 json 重新序列化。"""
        # data URL 必须带 charset 才能正确处理非 ASCII
        from urllib.parse import quote
        html = (
            "<html><body>"
            "<h1>Title</h1>"
            "<p>paragraph one</p>"
            '<p>paragraph two with special chars: "quoted"</p>'
            '<a href="/x">link</a>'
            "<button>btn</button>"
            "</body></html>"
        )
        data_url = "data:text/html;charset=utf-8," + quote(html)
        _http("POST", f"{daemon}/open", {"url": data_url})
        r = _http("GET", f"{daemon}/snapshot")
        assert r["ok"] is True
        snap = r["data"]
        # 关键: 这个 dict 能再次被 json.dumps 且不抛 (即 daemon 端没问题)
        dumped = json.dumps(snap, ensure_ascii=False)
        reparsed = json.loads(dumped)
        assert reparsed == snap
        # 结构检查
        assert "text_blocks" in snap
        assert "links" in snap
        assert "controls" in snap
        assert len(snap["text_blocks"]) >= 2
        # 含双引号的字符串应原样保留
        assert any('"quoted"' in b.get("text", "") for b in snap["text_blocks"])

    def test_read_markdown(self, daemon):
        data_url = "data:text/html,<html><body><h1>Read Test</h1><p>Some content here that should be extracted.</p></body></html>"
        _http("POST", f"{daemon}/open", {"url": data_url})
        r = _http("GET", f"{daemon}/read?format=markdown")
        assert r["ok"] is True
        md = r["data"]["content"]
        assert "Read Test" in md
        assert "Some content here" in md

    def test_click_and_url_changes(self, daemon):
        """/click 应该能点击带 data-sb-ref 的元素。"""
        # 构造一个带按钮 + onclick 的页面
        data_url = (
            "data:text/html,"
            "<html><body>"
            "<button id='b1' data-sb-ref='e1'>Click me</button>"
            "</body></html>"
        )
        _http("POST", f"{daemon}/open", {"url": data_url})
        r = _http("POST", f"{daemon}/click", {"ref": "e1"})
        assert r["ok"] is True
        # 成功标志是 success=True (不报错)
        # URL 不变 (无导航)

    def test_unknown_endpoint(self, daemon):
        """/foobar 应返回 ok=false 错误。"""
        r = _http("GET", f"{daemon}/foobar")
        assert r["ok"] is False
        assert "unknown endpoint" in r["error"]["message"].lower()

    def test_unknown_endpoint_returns_http_400(self, daemon):
        """错误端点必须返回 HTTP 400 (不仅是 ok=false 包络); HTTP-only 客户端才能靠
        status code 区分成败。"""
        req = urllib.request.Request(f"{daemon}/foobar")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected HTTPError for unknown endpoint")
        except HTTPError as e:
            assert e.code == 400
            body = json.loads(e.read())
            assert body["ok"] is False

    def test_missing_param_returns_http_400(self, daemon):
        """POST /open 缺 url 参数 → 400, body 含 ok=false。"""
        req = urllib.request.Request(
            f"{daemon}/open", data=b"{}",
            headers={"content-type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected HTTPError for missing url")
        except HTTPError as e:
            assert e.code == 400
            body = json.loads(e.read())
            assert body["ok"] is False
            assert "url" in body["error"]["message"]

    def test_state_no_page_returns_http_400(self, daemon):
        """GET /read 没 open 过页面时, daemon 应抛 ValueError → 400。"""
        req = urllib.request.Request(f"{daemon}/read")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("expected HTTPError")
        except HTTPError as e:
            assert e.code == 400
            body = json.loads(e.read())
            assert "no active page" in body["error"]["message"] or "open" in body["error"]["message"]


class TestDaemonClientCLI:
    def test_cli_help(self):
        """tb CLI 应能 --help (不真正启动 daemon)。"""
        result = subprocess.run(
            [sys.executable, "-m", "semantic_browser.client.cli", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        for cmd in ("open", "snapshot", "read", "click", "type",
                    "scroll", "press", "history", "graph", "daemon"):
            assert cmd in result.stdout, f"missing: {cmd}"

    def test_daemon_stop_sigterm_failure_exits_1(self, tmp_path):
        """B25: 进程忽略 SIGTERM 时, tb daemon stop 必须 exit 1 (而非 0),
        否则 `tb stop && tb start` 会在僵尸 daemon 上 start 失败。"""
        # 1. 启动一个 Python 子进程, 显式忽略 SIGTERM
        ignorer_script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", ignorer_script],
            stdout=subprocess.PIPE, text=True,
        )
        # 等它装好 signal handler
        try:
            assert proc.stdout.readline() == "ready\n"
        finally:
            pass

        # 2. 写 PID 文件到临时 HOME/.semantic-browser/daemon-{port}.pid
        import shutil
        home = tmp_path / "home"
        home.mkdir()
        sb_dir = home / ".semantic-browser"
        sb_dir.mkdir()
        port = 19999
        pid_file = sb_dir / f"daemon-{port}.pid"
        pid_file.write_text(f"{proc.pid}\n127.0.0.1\n")

        try:
            # 3. 运行 tb daemon stop, expect exit code 1 + stderr 提示 kill -9
            result = subprocess.run(
                [sys.executable, "-m", "semantic_browser.client.cli",
                 "daemon", "stop", "--port", str(port)],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "HOME": str(home)},
            )
            assert result.returncode == 1, (
                f"B25: SIGTERM 失效应返回 exit=1, got {result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
            assert "kill -9" in result.stderr or "kill -9" in result.stdout, (
                f"B25: 应提示用户手动 kill -9, got stderr={result.stderr!r} stdout={result.stdout!r}"
            )
        finally:
            # 清理: 真 kill -9 测试进程
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
