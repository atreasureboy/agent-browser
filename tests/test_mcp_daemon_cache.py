"""T95: MCP sb_query 跨进程 cache 共享 — 真 daemon 进程 + 真 HTTP 代理."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request


def _http(method: str, url: str, body: dict | None = None, timeout: int = 60) -> dict:
    """Call daemon HTTP."""
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def test_mcp_sb_query_uses_daemon_cache(monkeypatch, tmp_path):
    """T95: 起真 daemon → 起 MCP server 配 daemon_url → 调 sb_query MCP →
    验证 2nd 调用 (同 query) 通过 daemon 共享 cache 命中."""
    import socket

    # 1. 起 daemon subprocess (端口 18950)
    daemon_port = 18950
    print(f"Starting daemon on port {daemon_port}...")
    cache_file = str(tmp_path / "shared_cache.json")
    daemon_proc = subprocess.Popen(
        [sys.executable, "-m", "semantic_browser.client.cli",
         "daemon", "start", "--port", str(daemon_port), "--background"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    try:
        # 等 daemon 就绪
        for _ in range(30):
            try:
                r = _http("GET", f"http://127.0.0.1:{daemon_port}/healthz")
                if r.get("ok"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("daemon not ready in 15s")

        # 2. 起 MCP server — 通过 daemon_url 共享 daemon cache
        print(f"Daemon ready. Starting MCP server...")
        # 先用真 query 通过 daemon 写入 cache (URL1)
        q = "T95 cross-process test"
        url = "https://docs.python.org/3/whatsnew/3.13.html"
        r1 = _http("POST", f"http://127.0.0.1:{daemon_port}/v1/query",
                   {"query": q, "start_url": url, "budget": 1500})
        assert r1.get("ok"), f"first query failed: {r1}"
        first_tokens = r1["data"]["answer"]["tokens_used"]["used"]["total"]
        print(f"  1st query via daemon: tokens={first_tokens}")

        # 等几秒让 cache 写盘
        time.sleep(1)

        # 3. 现在用 MCPServer 调 sb_query (通过 daemon_url proxy)
        # MCPServer 内部会经 daemon HTTP 调用 /v1/query
        print(f"Testing MCP sb_query via daemon HTTP proxy...")
        monkeypatch.setenv("SEMANTIC_BROWSER_DAEMON_URL", f"http://127.0.0.1:{daemon_port}")
        from semantic_browser.mcp_server.server import MCPServer
        mcp = MCPServer()

        async def call_mcp():
            result = await mcp._call_tool("sb_query", {
                "query": q, "start_url": url, "budget": 1500,
            })
            return result

        result = asyncio.run(call_mcp())
        assert result.get("success"), f"MCP sb_query failed: {result}"
        mcp_tokens = result.get("tokens_used", {}).get("used", {}).get("total", 0)
        cache_hit = result.get("tokens_used", {}).get("cache_hit", False)
        print(f"  2nd query via MCP: tokens={mcp_tokens}, cache_hit={cache_hit}")
        # 期望 cache_hit=True (daemon 已 cache)
        assert cache_hit, f"MCP should hit daemon cache, got {result}"
        # answer 应该跟 1st 一样 (daemon cache 返回)
        assert result["answer"] == r1["data"]["answer"]["answer"], (
            "MCP 拿到的 answer 应该跟 daemon 直接拿的一致 (共享 cache)"
        )
        print("✓ MCP sb_query uses daemon cache (cross-process shared)")

        # 4. 检查 daemon 的 query_log 包含这次调用
        time.sleep(0.5)
        rs = _http("GET", f"http://127.0.0.1:{daemon_port}/v1/query/stats")
        assert rs.get("ok"), f"stats failed: {rs}"
        cache_stats = rs["data"]["cache"]
        log_summary = rs["data"]["query_log_summary"]
        print(f"  daemon cache stats: hits={cache_stats['hits']} misses={cache_stats['misses']} log_size={log_summary['total_logged']}")
        # log size 应该 >= 2 (1st query + 2nd query via MCP)
        assert log_summary["total_logged"] >= 2, f"log should have 2+ entries: {log_summary}"
        print("✓ daemon log records MCP calls too")

    finally:
        daemon_proc.terminate()
        daemon_proc.wait(timeout=5)
