"""T70.3: Token Savings Benchmark — 量化 SemanticQuery 的 token 经济.

运行多次同 query (有 cache vs 无 cache), 输出对比表 + 节省的秒数.

跑法 (有 LLM key 时):
    source .env && python examples/benchmark_savings.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from semantic_browser.query import SemanticQuery


BENCHMARK_QUERIES = [
    {
        "query": "Python 3.13 free-threading executable name and what it does",
        "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
        "label": "Python 3.13 release notes",
    },
    {
        "query": "Briefly: Python GIL removal PEP",
        "start_url": "https://peps.python.org/pep-0703/",
        "label": "PEP 703 page",
    },
    {
        "query": "Find Python 3.13 just-in-time compiler enabled flag",
        "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
        "label": "Same docs page (different query)",
    },
]


async def main():
    print("=" * 78)
    print("Token Savings Benchmark — SemanticQuery")
    print("=" * 78)
    print()
    print(f"{'Query':<35} {'Cache':<8} {'Tokens':<10} {'Time (s)':<10} {'Cached'}")
    print("-" * 78)

    # cold cache: fresh instance, no persist_path
    for case in BENCHMARK_QUERIES:
        sq = SemanticQuery(budget=1500, max_pages=1, cache_enabled=True)
        try:
            t0 = time.time()
            r = await sq.run(case["query"], start_url=case["start_url"])
            t1 = time.time()
            used = r.tokens_used.get("used", {})
            print(f"{case['label']:<35} {'cold':<8} {used.get('total', 0):<10} "
                  f"{(t1-t0):<10.1f} {r.tokens_used.get('cache_hit', False)}")
        finally:
            await sq.close()

    # warm cache: shared instance, 3 次 hot call
    sq = SemanticQuery(budget=1500, max_pages=1, cache_enabled=True)
    try:
        for case in BENCHMARK_QUERIES:
            t0 = time.time()
            r = await sq.run(case["query"], start_url=case["start_url"])
            t1 = time.time()
            used = r.tokens_used.get("used", {})
            print(f"{case['label']:<35} {'warm':<8} {used.get('total', 0):<10} "
                  f"{(t1-t0):<10.1f} {r.tokens_used.get('cache_hit', False)}")
    finally:
        await sq.close()

    # Stats summary
    print()
    print("Cache stats:")
    print(f"  {sq.cache_stats()}")

    # Compute summary
    if sq._cache_hits + sq._cache_misses > 0:
        total = sq._cache_hits + sq._cache_misses
        hit_pct = sq._cache_hits / total * 100
        print()
        print("=" * 78)
        print(f"Hit rate: {hit_pct:.1f}% ({sq._cache_hits} hits, {sq._cache_misses} misses)")
        print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
