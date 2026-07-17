"""T79: Answer correctness — 用 known-good queries 验证答案内容真实正确.

不是验证格式正确 (有 answer / sources), 而是验证:
1. 答案包含期望的关键字 (e.g. "python3.13t")
2. 答案包含至少 1 个 [N] 引用 (M3 标记 source)
3. confidence ≥ 0.5
4. source URL 包含目标 URL

跑法 (有 LLM key):
    pytest tests/test_query_correctness.py -v -W ignore

跑法 (无 key, 跳过):
    pytest tests/test_query_correctness.py -v -W ignore  # 自动 skip
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest


LLM_AVAILABLE = bool(
    os.getenv("ANTHROPIC_AUTH_TOKEN")
    or os.getenv("ANTHROPIC_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("LLM_API_KEY")
)


# Golden queries — 期望答案应含这些关键字 + 来源 URL
GOLDEN = [
    {
        "query": "Python 3.13 free-threading executable name",
        "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
        "must_contain": ["python3.13t"],  # executable name
        "must_have_citation": True,
        "min_confidence": 0.7,
        "label": "Python 3.13 free-threading",
    },
    {
        "query": "What is PEP 703 about",
        "start_url": "https://peps.python.org/pep-0703/",
        "must_contain": ["GIL", "PEP 703"],
        "must_have_citation": True,
        "min_confidence": 0.7,
        "label": "PEP 703 GIL",
    },
    {
        "query": "Python 3.13 release date and main features",
        "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
        "must_contain": ["3.13", "October"],  # release date is October 7, 2024
        "must_have_citation": True,
        "min_confidence": 0.5,  # release date might not be in early sections
        "label": "Python 3.13 release",
    },
]


@pytest.mark.skipif(not LLM_AVAILABLE, reason="no LLM API key in env")
class TestAnswerCorrectness:
    """真实 M3 + Chromium: 验证答案内容真实正确 (不只是格式)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", GOLDEN, ids=[g["label"] for g in GOLDEN])
    async def test_answer_contains_expected_keywords(self, case):
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2000, max_pages=1)
        try:
            r = await sq.run(case["query"], start_url=case["start_url"])
            assert r.success, f"query failed: {r.error}"
            answer_lower = r.answer.lower()
            for keyword in case["must_contain"]:
                assert keyword.lower() in answer_lower, (
                    f"answer missing keyword '{keyword}': {r.answer[:300]}"
                )
        finally:
            await sq.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", GOLDEN, ids=[g["label"] for g in GOLDEN])
    async def test_answer_has_citation(self, case):
        """answer 应至少含 1 个 [N] 引用."""
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2000, max_pages=1)
        try:
            r = await sq.run(case["query"], start_url=case["start_url"])
            assert r.success
            # answer 中应有 [N] 引用 (M3 标记 source)
            citations = re.findall(r'\[(\d+)\]', r.answer)
            assert len(citations) >= 1, f"no [N] citation in answer: {r.answer[:300]}"
        finally:
            await sq.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", GOLDEN, ids=[g["label"] for g in GOLDEN])
    async def test_answer_confidence_above_threshold(self, case):
        """M3 自评 confidence 应 ≥ min_confidence (证明答案真置信)."""
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2000, max_pages=1)
        try:
            r = await sq.run(case["query"], start_url=case["start_url"])
            assert r.success
            assert r.confidence >= case["min_confidence"], (
                f"confidence {r.confidence} below {case['min_confidence']}: {r.answer[:200]}"
            )
        finally:
            await sq.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", GOLDEN, ids=[g["label"] for g in GOLDEN])
    async def test_answer_source_contains_start_url(self, case):
        """sources 应含原始 URL (证明确实抓了目标页面)."""
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(budget=2000, max_pages=1)
        try:
            r = await sq.run(case["query"], start_url=case["start_url"])
            assert r.success
            # source URL 应含 start_url 的 host (可能是原 URL 或 redirect 后 URL)
            start_host = case["start_url"].split("//")[-1].split("/")[0]
            assert any(start_host in s for s in r.sources), (
                f"no source from {start_host} in {r.sources}"
            )
        finally:
            await sq.close()
