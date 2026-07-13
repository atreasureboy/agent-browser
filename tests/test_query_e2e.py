"""T69: tests/test_query_e2e.py — SemanticQuery 真实 e2e (可在 CI 跑).

跳过条件: 没有 ANTHROPIC_API_KEY / OPENAI_API_KEY 时 skip, 不破坏 CI 流程.
跑法 (有 LLM key 时):
    pytest tests/test_query_e2e.py -v
"""
from __future__ import annotations

import os
import asyncio

import pytest


LLM_AVAILABLE = bool(
    os.getenv("ANTHROPIC_AUTH_TOKEN")
    or os.getenv("ANTHROPIC_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("LLM_API_KEY")
)


@pytest.mark.skipif(not LLM_AVAILABLE, reason="no LLM API key in env")
class TestSemanticQueryE2E:
    """真实 Chromium + M3 完整跑通 — 慢测试, 默认 skip."""

    @pytest.mark.asyncio
    async def test_e2e_python_doc_page(self):
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=1500, max_pages=1)
        try:
            result = await sq.run(
                "Python 3.13 free-threading executable name",
                start_url="https://docs.python.org/3/whatsnew/3.13.html",
            )
            assert result.success
            assert result.confidence > 0.5
            assert "python3.13t" in result.answer.lower(), (
                f"expected executable name in answer, got: {result.answer[:200]}"
            )
            assert result.tokens_used["used"]["total"] > 0
            assert result.sources  # 至少 1 个 source
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_e2e_cache_hit(self):
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=1500, max_pages=1)
        try:
            q = "Python 3.13.1 release date"
            url = "https://docs.python.org/3/whatsnew/3.13.html"
            r1 = await sq.run(q, start_url=url)
            r2 = await sq.run(q, start_url=url)
            # 第二次应该 cache hit
            assert r2.tokens_used.get("cache_hit") is True
            assert r1.answer == r2.answer
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_e2e_plan_only(self):
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=300)
        try:
            r = await sq.run("Find latest Python GIL removal news 2024")
            assert r.success
            assert "primary_target" in r.plan
            assert len(r.plan["keywords"]) > 0
            assert "Plan" in r.answer or "Sub-questions" in r.answer
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_e2e_token_budget_hard_limit(self):
        """极小预算时应该 budget_exceeded 但不崩溃."""
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=50)  # 几乎不够任何 M3 调用
        try:
            r = await sq.run("any query", start_url="https://example.com/")
            # 可能 success (heuristic fallback) 或 budget_exceeded — 都不崩
            assert isinstance(r.success, bool)
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_e2e_multi_page_navigation(self):
        """T68 multi-page follow-link: M3 智能 break 不一定翻满 max_pages."""
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2000, max_pages=3, sufficiency_threshold=0.95)
        try:
            r = await sq.run(
                "Briefly: Python 3.13 free-threading executable name",
                start_url="https://docs.python.org/3/whatsnew/3.13.html",
            )
            # Python 3.13 release notes 是综合页, page 1 就够, M3 应智能 stop
            assert r.success
            # 任一 pages 都有 answer (M3 break 早于 max_pages 也 OK)
            assert r.answer
            assert r.tokens_used["used"]["total"] <= 2000
            # steps 应含 plan_done / browse_done / relevance_done / synth_done
            phases = [s.get('phase') for s in r.steps]
            assert 'plan_done' in phases
            assert 'browse_done' in phases
            assert 'synth_done' in phases
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_e2e_concurrent_queries(self):
        """T70.5: 同 instance 并发 2 个 query (cache 应隔离 — 不同 query key)."""
        import asyncio
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2500, max_pages=1)
        try:
            r1, r2 = await asyncio.gather(
                sq.run("Python 3.13 free-threading executable",
                       start_url="https://docs.python.org/3/whatsnew/3.13.html"),
                sq.run("Python GIL PEP 703 brief intro",
                       start_url="https://peps.python.org/pep-0703/"),
            )
            # 两个 result 都应 success
            assert r1.success
            assert r2.success
            # source 不应混淆
            assert "docs.python.org" in r1.sources[0]
            assert "peps.python.org" in r2.sources[0]
            # tokens 都累计
            assert r1.tokens_used["used"]["total"] > 0
            assert r2.tokens_used["used"]["total"] > 0
        finally:
            await sq.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
