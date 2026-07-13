"""T68: Reference client for "model-driven browser semantic layer".

展示了顶层 agent 用 SemanticQuery 的 4 种典型场景:
  1. 单页: 找已知页面特定信息
  2. 多页: 需要翻几个子页面才能完整回答
  3. plan-only: 没有 URL, 只返 plan 让顶层 agent 自己选 URL
  4. cache 命中: 二次同 query 复用结果, 0 token

运行:
  source .env && python examples/semantic_query_demo.py
"""
import asyncio
import os

# 项目会自动读 .env (如果你已经在 env 里 source 过)
from semantic_browser.query import SemanticQuery, run_query


async def scenario_1_single_page():
    """场景 1: 单页 — 已知 URL 找具体信息."""
    print("\n=== Scenario 1: 单页 (已知 URL) ===")
    result = await run_query(
        "Python 3.13 free-threaded mode: what executable + which flag?",
        start_url="https://docs.python.org/3/whatsnew/3.13.html",
        budget=1500,
    )
    print(f"  success={result.success} confidence={result.confidence:.2f}")
    print(f"  tokens={result.tokens_used['used']['total']}/{result.tokens_used['max_total']}")
    print(f"  answer (first 300 chars):")
    for line in result.answer[:300].split("\n"):
        print(f"    {line}")
    print()
    return result


async def scenario_2_plan_only():
    """场景 2: 没有 URL — 只想让 M3 给个研究计划."""
    print("\n=== Scenario 2: plan-only (无 start_url) ===")
    result = await run_query(
        "Find the latest Python GIL removal discussions and PEPs from 2024",
        budget=300,  # plan 单独便宜
    )
    print(f"  success={result.success}")
    print(f"  plan keywords: {result.plan.get('keywords', [])[:8]}")
    print(f"  sub_questions (first 2):")
    for q in result.plan.get("sub_questions", [])[:2]:
        print(f"    - {q}")
    print()
    return result


async def scenario_3_cache_hit():
    """场景 3: 二次同 query 命中 cache, 不消耗 token."""
    print("\n=== Scenario 3: cache hit (同 query 二次调) ===")
    sq = SemanticQuery(budget=1500, max_pages=1)
    try:
        # 第一次: 完整跑
        r1 = await sq.run(
            "Python 3.13 free-threaded mode: what executable + which flag?",
            start_url="https://docs.python.org/3/whatsnew/3.13.html",
        )
        # 第二次: cache hit
        r2 = await sq.run(
            "Python 3.13 free-threaded mode: what executable + which flag?",
            start_url="https://docs.python.org/3/whatsnew/3.13.html",
        )
        print(f"  1st:  cache_hit={r1.tokens_used.get('cache_hit')}")
        print(f"  2nd:  cache_hit={r2.tokens_used.get('cache_hit')}")
        print(f"  answers identical: {r1.answer == r2.answer}")
        print(f"  cache_stats: {sq.cache_stats()}")
    finally:
        await sq.close()
    print()


async def scenario_4_persistent_cache():
    """场景 4: 持久 cache 跨 daemon 重启."""
    print("\n=== Scenario 4: persistent cache (跨进程) ===")
    cache_path = os.path.expanduser("~/.semantic-browser/query_cache_demo.json")

    # 第一次: 跑 + 持久
    sq = SemanticQuery(budget=1500, cache_persist_path=cache_path, max_pages=1)
    try:
        r1 = await sq.run(
            "Python 3.13 free-threaded mode: what executable + which flag?",
            start_url="https://docs.python.org/3/whatsnew/3.13.html",
        )
        print(f"  1st process: cache_hit={r1.tokens_used.get('cache_hit')}, file_exists={os.path.exists(cache_path)}")
    finally:
        await sq.close()

    # 模拟 daemon 重启: 新 SemanticQuery 实例
    sq2 = SemanticQuery(budget=1500, cache_persist_path=cache_path, max_pages=1)
    try:
        r2 = await sq2.run(
            "Python 3.13 free-threaded mode: what executable + which flag?",
            start_url="https://docs.python.org/3/whatsnew/3.13.html",
        )
        print(f"  2nd process (restart): cache_hit={r2.tokens_used.get('cache_hit')}, cache_age_s={r2.tokens_used.get('cache_age_s')}s")
        print(f"  answers identical across restart: {r1.answer == r2.answer}")
    finally:
        await sq2.close()

    # 清理
    if os.path.exists(cache_path):
        os.remove(cache_path)
    print()


async def scenario_5_via_daemon():
    """场景 5: 通过 daemon HTTP 调 (多 agent 共享的工业用法).

    假设 daemon 已经在端口 8765 跑 (`tb daemon start --port 8765`).
    """
    print("\n=== Scenario 5: via daemon HTTP (生产用法) ===")
    import urllib.request, json
    body = json.dumps({
        "query": "Python 3.13 free-threaded mode: what executable + which flag?",
        "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
        "budget": 1500,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8765/v1/query",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    loop = asyncio.get_event_loop()
    def http():
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"_err": str(e)[:200]}
    r = await loop.run_in_executor(None, http)
    if "_err" in r:
        print(f"  (daemon not running, skip): {r['_err']}")
        return
    print(f"  ok={r.get('ok')} success={r['data']['success']}")
    print(f"  tokens={r['data']['tokens_used']['used']['total']}")
    print(f"  sources={r['data']['sources'][:1]}")
    print()


async def main():
    print("=" * 60)
    print("SemanticQuery Demo - Model-Driven Browser Semantic Layer")
    print("=" * 60)
    await scenario_2_plan_only()
    await scenario_1_single_page()
    await scenario_3_cache_hit()
    await scenario_4_persistent_cache()
    await scenario_5_via_daemon()
    print("=" * 60)
    print("All scenarios complete.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
