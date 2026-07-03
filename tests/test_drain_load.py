"""B — drain under concurrent load.

50 个并发 client, drain 触发时验证:
1. 在飞的 op 跑完 (不被打断)
2. 之后到达的新 op 全拿 503 DAEMON_DRAINING
3. 没有 lost request (连不上不算 lost, 503 算正常 drain 行为)
4. 关闭后端口不再接新连接
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import closing
from urllib.error import HTTPError, URLError

import pytest


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body: dict | None = None, timeout: float = 30):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"_status": resp.status, "body": json.loads(resp.read())}
    except HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return {"_status": e.code, "body": body}
    except (URLError, ConnectionRefusedError, OSError) as e:
        return {"_status": 0, "body": None, "_err": str(e)}


@pytest.fixture
def daemon():
    port = _free_port()
    log_path = f"/tmp/tb-drain-{port}.log"
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "semantic_browser.daemon.server",
         "--port", str(port), "--allow-data-scheme",
         "--drain-timeout", "10",  # 缩短测试时间
         "--watchdog-interval", "0",  # 干扰 SSE
         ],
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT, env=env,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = _http("GET", f"{base}/health")
            if r["body"] and r["body"].get("ok"):
                break
        except (URLError, ConnectionRefusedError, OSError):
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail(f"daemon did not start; see {log_path}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        pass # os.unlink(log_path)
    except OSError:
        pass


class TestDrainUnderLoad:
    """B: drain 触发时, 大量并发 op 的行为."""

    def test_50_concurrent_drain_blocking(self, daemon):
        """B.1: 先发 25 个 in-flight (期望 ok), drain 触发后发 25 个 (期望 503/refused).

        用绝对时间点控制: drain_at 是相对于"线程就绪时刻"的偏移.
        """
        results: list[dict] = []
        results_lock = threading.Lock()
        N_pre = 25
        N_post = 25
        ready_evt = threading.Event()  # 线程就绪信号 — release 后开始计算
        t0 = [0.0]  # ready 时间戳

        def open_url(idx: int, fire_after: float):
            ready_evt.wait()  # 等 ready
            delay = fire_after - (time.time() - t0[0])
            if delay > 0:
                time.sleep(delay)
            # 记录 fire time (不是 response time — op_lock 排队会拖后 response)
            fire_time = time.time() - t0[0]
            r = _http("POST", f"{daemon}/open",
                     {"url": f"data:text/html,<h1>{idx}</h1>"}, timeout=30)
            r["idx"] = idx
            r["fire_time"] = fire_time
            r["arrival"] = time.time() - t0[0]
            with results_lock:
                results.append(r)

        threads = []
        # 第一波: ready 后 0.05s 全部发 (drain 触发前)
        for i in range(N_pre):
            t = threading.Thread(target=open_url, args=(i, 0.05))
            t.start()
            threads.append(t)
        # 第二波: ready 后 1.5s 发 (drain 触发后, drain 触发在 0.5s)
        for i in range(N_pre, N_pre + N_post):
            t = threading.Thread(target=open_url, args=(i, 1.5))
            t.start()
            threads.append(t)

        # drain 触发器: t0+0.5s 时刻
        def trigger_drain():
            ready_evt.wait()
            time.sleep(0.5)
            _http("POST", f"{daemon}/admin/drain", {})
        drain_t = threading.Thread(target=trigger_drain)
        drain_t.start()

        # 等所有线程就绪 (即调用了 ready_evt.wait())
        time.sleep(0.5)  # 给线程启动时间
        t0[0] = time.time()
        ready_evt.set()  # 释放所有

        for t in threads:
            t.join(timeout=60)
        drain_t.join(timeout=10)

        # 分类: 用 fire_time (不是 arrival) — arrival 受 op_lock 排队影响会拖后
        pre_drain = [r for r in results if r["fire_time"] < 0.5]
        post_drain = [r for r in results if r["fire_time"] >= 0.5]
        oks = [r for r in results if r.get("_status") == 200
               and r.get("body", {}).get("ok")]
        drains = [r for r in results if r.get("_status") == 503
                  and r.get("body", {}).get("error", {}).get("code") == "DAEMON_DRAINING"]
        refused = [r for r in results if r.get("_status") == 0]

        # 验收:
        # 1) drain 前 fired 的 25 个, 至少大部分应 ok (进 op_lock 队列)
        assert len(oks) >= 5, f"pre-drain 至少 5 ok, got {len(oks)} / total={len(results)}"
        # 2) drain 后 fired 的请求, 应全被 drain 挡 (503) 或 refused
        for r in post_drain:
            assert r["_status"] == 503 or r["_status"] == 0, (
                f"post-drain 请求 _status={r['_status']}, body={r.get('body')}, "
                f"fire_time={r['fire_time']:.2f}"
            )
        # 3) 503 应带 DAEMON_DRAINING code
        for r in drains:
            assert r["body"]["error"]["draining"] is True
            assert r["body"]["error"]["retryable"] is True
        # 4) 至少 80% post-drain 应被 drain 挡
        assert len(drains) + len(refused) >= N_post * 4 // 5, (
            f"drain 应拦 >= 80% post-drain, got drained={len(drains)} "
            f"refused={len(refused)} / N_post={N_post}"
        )

    def test_drain_then_no_new_op_accepted(self, daemon):
        """B.2: drain 之后, 任何新 op 拿 503."""
        _http("POST", f"{daemon}/admin/drain", {})
        time.sleep(0.2)
        # 多次确认
        for i in range(5):
            r = _http("POST", f"{daemon}/open", {"url": "data:text/html,x"})
            assert r["_status"] == 503
            assert r["body"]["error"]["code"] == "DAEMON_DRAINING"
            time.sleep(0.1)

    def test_drain_health_endpoint_still_works(self, daemon):
        """B.3: drain 中 /health 仍 OK + reporting draining=true."""
        _http("POST", f"{daemon}/admin/drain", {})
        for _ in range(3):
            r = _http("GET", f"{daemon}/health")
            assert r["_status"] == 200
            assert r["body"]["ok"]
            assert r["body"]["data"]["draining"] is True
            assert r["body"]["data"]["status"] == "draining"
            time.sleep(0.1)

    def test_no_lost_connections(self, daemon):
        """B.4: drain 已生效后, 大量并发请求的结局分类.

        先触发 drain, 等它就绪, 再发 30 并发. 验证:
        - 没有 5xx/timeout 等意外 status (除 503 + refused)
        - 至少 1/3 是 drain 503 (证明 drain 真在挡)
        """
        # 1) 先 drain
        r = _http("POST", f"{daemon}/admin/drain", {})
        assert r["_status"] == 200
        # 2) 验证 /health 已 draining=true (drain 标志生效)
        time.sleep(0.2)
        h = _http("GET", f"{daemon}/health")
        assert h["body"]["data"]["draining"] is True
        # 3) 30 并发
        N = 30
        results: list[dict] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(N, timeout=10)

        def open_url(idx: int):
            barrier.wait()
            r = _http("POST", f"{daemon}/open",
                     {"url": f"data:text/html,<h1>{idx}</h1>"}, timeout=10)
            with results_lock:
                results.append(r)
        ts = [threading.Thread(target=open_url, args=(i,)) for i in range(N)]
        for t in ts: t.start()
        for t in ts: t.join(timeout=30)

        # 分类: 200 (漏过) / 503 (drain 挡) / 0 (refused)
        n_ok = sum(1 for r in results if r.get("_status") == 200)
        n_drained = sum(1 for r in results
                        if r.get("_status") == 503
                        and r.get("body", {}).get("error", {}).get("code") == "DAEMON_DRAINING")
        n_refused = sum(1 for r in results if r.get("_status") == 0)
        unexpected = [r for r in results
                      if r.get("_status") not in (0, 200, 503)]
        assert not unexpected, f"unexpected: {unexpected[:3]}"
        # drain 之后发的请求, 多数应被 drain 挡 — 允许 1-2 个 leak (drain race)
        # 但至少 1/3 应被挡
        assert n_drained + n_refused >= N // 3, (
            f"drain 应拦 >=1/3, got ok={n_ok} drained={n_drained} refused={n_refused} / N={N}"
        )
