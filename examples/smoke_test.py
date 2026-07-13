"""T70.4: Smoke Test — 一次性跑遍 4 种接入方式, 输出 PASS/FAIL 表.

用法:
    source .env && python examples/smoke_test.py
    # 在 daemon 起动时另外起一个:
    tb daemon start --port 18800 --background
    # daemon smoke 会用 18800 端口

退出码: 0 全过 / 1 部分失败 / 2 全失败.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


DAEMON_PORT = int(os.environ.get("SMOKE_PORT", "18800"))
DAEMON_BASE = f"http://127.0.0.1:{DAEMON_PORT}"

QUERIES = [
    ("Python 3.13 free-threading executable name",
     "https://docs.python.org/3/whatsnew/3.13.html"),
    ("Python GIL removal PEP explanation",
     "https://peps.python.org/pep-0703/"),
]


def step(label: str) -> None:
    print(f"\n=== {label} ===")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


async def smoke_python() -> int:
    """1. Python 同步入口."""
    step("1. Python run_query()")
    from semantic_browser.query import run_query

    failures = 0
    for q, url in QUERIES:
        try:
            r = await run_query(q, start_url=url, budget=1500)
            if r.success and r.answer:
                ok(f"{q[:30]}... → {len(r.answer)} chars, tokens={r.tokens_used['used']['total']}")
            else:
                fail(f"{q[:30]}... → success={r.success} error={r.error}")
                failures += 1
        except Exception as e:
            fail(f"{q[:30]}... → {type(e).__name__}: {e}")
            failures += 1
    return failures


async def smoke_cli() -> int:
    """2. CLI sb query."""
    step("2. CLI sb query")
    from click.testing import CliRunner
    from semantic_browser.cli.main import cli

    failures = 0
    q, url = QUERIES[0]
    runner = CliRunner()
    r = runner.invoke(cli, ["query", q, "--start-url", url, "--budget", "1500", "--quiet"])
    if r.exit_code == 0 and len(r.output) > 50:
        ok(f"CLI exit={r.exit_code}, {len(r.output)} chars")
    else:
        fail(f"CLI exit={r.exit_code}: {r.output[:200]}")
        failures += 1
    return failures


async def smoke_daemon() -> int:
    """3. daemon HTTP /v1/query."""
    step(f"3. daemon HTTP /v1/query (port {DAEMON_PORT})")

    import urllib.request

    # 检查 daemon 是否在跑
    try:
        req = urllib.request.Request(f"{DAEMON_BASE}/healthz")
        with urllib.request.urlopen(req, timeout=2) as r:
            r.read()
    except Exception as e:
        fail(f"daemon not running on :{DAEMON_PORT} ({e}). 跳过 daemon smoke.")
        return -1  # -1 = skip (not fail)

    failures = 0
    q, url = QUERIES[0]
    try:
        body = json.dumps({"query": q, "start_url": url, "budget": 1500}).encode()
        req = urllib.request.Request(
            f"{DAEMON_BASE}/v1/query",
            data=body, headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as r:
            t1 = time.time()
            data = json.loads(r.read())
        # T70.9: 响应是 {request_id, answer: {...}}
        outer = data.get("data", {})
        ans = outer.get("answer", {}) if "answer" in outer else outer
        if ans.get("success") and ans.get("answer"):
            req_id = outer.get("request_id", "n/a")
            ok(f"daemon returned {len(ans['answer'])} chars in {t1-t0:.1f}s "
               f"(tokens={ans['tokens_used']['used']['total']} request_id={req_id})")
        else:
            fail(f"daemon returned failure: {data}")
            failures += 1
    except Exception as e:
        fail(f"daemon HTTP: {type(e).__name__}: {e}")
        failures += 1

    # 同样 query 第二次 — 应该有 cache hit
    try:
        body = json.dumps({"query": q, "start_url": url, "budget": 1500}).encode()
        req = urllib.request.Request(
            f"{DAEMON_BASE}/v1/query",
            data=body, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        ans = data.get("data", {})
        if ans.get("tokens_used", {}).get("cache_hit"):
            ok(f"2nd call cache_hit=True age={ans['tokens_used']['cache_age_s']}s")
        else:
            fail(f"2nd call should hit cache, got: {ans.get('tokens_used', {}).get('cache_hit')}")
            failures += 1
    except Exception as e:
        fail(f"daemon 2nd call: {e}")

    return failures


async def smoke_mcp() -> int:
    """4. MCP sb_query."""
    step("4. MCP sb_query + sb_query_stats")
    from semantic_browser.mcp_server.server import MCPServer

    failures = 0
    server = MCPServer.__new__(MCPServer)
    q, url = QUERIES[0]
    try:
        r = await server._call_tool("sb_query", {"query": q, "start_url": url, "budget": 1500})
        if r.get("success"):
            ok(f"sb_query → {len(r.get('answer', ''))} chars")
        else:
            fail(f"sb_query: success=False, error={r.get('error')}")
            failures += 1
    except Exception as e:
        fail(f"sb_query: {type(e).__name__}: {e}")
        failures += 1

    try:
        r = await server._call_tool("sb_query_stats", {})
        if r.get("size") is not None:
            ok(f"sb_query_stats → size={r['size']} hits={r['hits']} misses={r['misses']} hit_rate={r['hit_rate']}")
        else:
            fail(f"sb_query_stats 返回异常: {r}")
    except Exception as e:
        fail(f"sb_query_stats: {type(e).__name__}: {e}")
        failures += 1

    return failures


async def main() -> int:
    print("=" * 60)
    print("SemanticQuery Smoke Test")
    print("=" * 60)

    results = {}
    for name, fn in [
        ("python", smoke_python),
        ("cli", smoke_cli),
        ("daemon", smoke_daemon),
        ("mcp", smoke_mcp),
    ]:
        try:
            failures = await fn()
            results[name] = -1 if failures == -1 else (0 if failures == 0 else 1)
        except Exception as e:
            fail(f"{name} 全错: {type(e).__name__}: {e}")
            results[name] = 2

    print()
    print("=" * 60)
    print("Summary:")
    for name, code in results.items():
        label = {0: "✓ PASS", 1: "✗ FAIL", 2: "✗✗ ERROR", -1: "~ SKIP"}[code]
        print(f"  {name:<10} {label}")
    print("=" * 60)

    if all(c <= 0 for c in results.values()):  # all PASS or SKIP
        return 0 if any(c == 0 for c in results.values()) else 1
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
