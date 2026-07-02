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
