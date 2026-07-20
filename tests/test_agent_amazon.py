"""T103: Agent 实测 Amazon (e-commerce test).

验证工具在真实 e-commerce 站上 (Amazon 抗 bot / books.toscrape 友好) 能正常工作.

跑:
- pytest tests/test_agent_amazon.py -v
- 实测 daemons 起: tb daemon start --port 18801 --background
- 用 /v1/query 端点 (HTTP + JSON)
"""
from __future__ import annotations

import json
import time
import urllib.request


DAEMON_BASE = "http://127.0.0.1:18801"


def call(query_text: str, start_url: str | None = None, budget: int = 1500) -> dict:
    body = json.dumps({
        "query": query_text,
        "start_url": start_url,
        "budget": budget,
        "max_pages": 1,
    }).encode()
    req = urllib.request.Request(
        f"{DAEMON_BASE}/v1/query", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["data"]["answer"]


def test_books_toscrape_works():
    """T103: books.toscrape.com (无 anti-bot) — 工具应能正常抓 books 和 prices."""
    ans = call("find books with prices", start_url="https://books.toscrape.com/", budget=1500)
    assert ans["success"], f"query failed: {ans.get('error', 'unknown')}"
    assert len(ans["sources"]) > 0, f"no sources: {ans}"
    # 工具应能 extract 至少一个 book title
    assert "books" in ans["answer"].lower() or any(
        word in ans["answer"].lower() for word in ["sapiens", "history", "tales", "coming"]
    ), f"no books in answer: {ans['answer'][:200]}"
    print(f"  ✓ books.toscrape: {len(ans['answer'])} chars, {len(ans['sources'])} sources")


def test_amazon_search_anti_bot_acknowledged():
    """T103: Amazon 抗 bot — 工具能调但取不到内容 (sources=[], confidence=0).

    这个测试不要求成功 — 记录 Amazon 实测结果.
    Amazon 用 fingerprint/anti-bot 挡 Playwright 默认无 stealth. 实际想攻需要
    playwright-stealth (但 ToS 也禁). 这个测试标 skip 让 CI 快速通过, 但
    手动跑时记录真实行为.
    """
    import pytest
    if not _is_daemon_up():
        pytest.skip("daemon not running on 18801 — start with: tb daemon start --port 18801 --background")
    t0 = time.time()
    ans = call("iPhone 15 listings with prices", start_url="https://www.amazon.com/s?k=iphone+15", budget=1500)
    elapsed = time.time() - t0
    # Amazon 抗 bot 的话, 应 sources=[] 或 confidence=0
    # 不强制 fail (因为这是 Amazon 行为, 不是工具 bug)
    if not ans["sources"] and ans["confidence"] == 0.0:
        print(f"  ⚠ Amazon 抗 bot (sources=[]): {elapsed:.1f}s")
    else:
        print(f"  ✓ Amazon 实际可访问 ({elapsed:.1f}s, sources={ans['sources']})")


def _is_daemon_up() -> bool:
    try:
        with urllib.request.urlopen(f"{DAEMON_BASE}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False
