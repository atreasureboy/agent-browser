"""
Query Planner — M3 拆 query → 多步研究计划 + stop_criteria.

动机: 顶级 agent 传一个高层 query 进来 ("找到 GitHub 上 PEP 703 最新讨论, 给 3 个观点"),
我们需要把这个 query 拆成可执行的小计划:
  - primary_target: 主要问题
  - sub_questions: 子问题列表 (后续 relevance filter 用)
  - stop_criteria: 何时算"够"了
  - expected_answer_format: markdown / list / json
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from semantic_browser.llm.service import LLMService, Tier, LLMUnavailableError

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a query planner. Given a high-level user query, decompose it into a research plan.

Respond with JSON only:
{
  "primary_target": "the main thing to find out",
  "sub_questions": ["sub-question 1", "sub-question 2", ...],
  "stop_criteria": "how to know when enough information has been collected",
  "expected_answer_format": "markdown|list|json",
  "keywords": ["keyword1", "keyword2", ...],   // used for relevance scoring
  "candidate_urls": [                              // T71: URL auto-discovery
    "https://example.com/page1",
    "https://docs.example.com/topic"
  ]
}

Rules:
- 2-5 sub_questions max
- sub_questions should be answerable from a single web page
- keywords should be short (1-3 words), used for filtering
- candidate_urls: 1-3 URLs that LIKELY contain the answer.
  * Prefer canonical sources (official docs, primary sites, wikipedia, GitHub README)
  * Only include URLs you have HIGH confidence are relevant
  * Empty array [] if you don't know (top agent can fall back to manual URL)
- Be concise
"""


@dataclass
class QueryPlan:
    """M3 拆出的研究计划."""
    primary_target: str = ""
    sub_questions: list[str] = field(default_factory=list)
    stop_criteria: str = ""
    expected_answer_format: str = "markdown"
    keywords: list[str] = field(default_factory=list)
    candidate_urls: list[str] = field(default_factory=list)  # T71: URL auto-discovery

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_target": self.primary_target,
            "sub_questions": list(self.sub_questions),
            "stop_criteria": self.stop_criteria,
            "expected_answer_format": self.expected_answer_format,
            "keywords": list(self.keywords),
            "candidate_urls": list(self.candidate_urls),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QueryPlan":
        return cls(
            primary_target=d.get("primary_target", "") or "",
            sub_questions=list(d.get("sub_questions", []) or []),
            stop_criteria=d.get("stop_criteria", "") or "",
            expected_answer_format=d.get("expected_answer_format", "markdown") or "markdown",
            keywords=list(d.get("keywords", []) or []),
            candidate_urls=list(d.get("candidate_urls", []) or []),
        )

    @classmethod
    def fallback(cls, query: str) -> "QueryPlan":
        """M3 不可用时的启发式退化: 直接把整个 query 当 primary_target + 拆词作 keywords."""
        words = re.findall(r"[A-Za-z0-9一-鿿]+", query)[:5]
        return cls(
            primary_target=query,
            sub_questions=[query],
            stop_criteria="at least one sub-question answered",
            expected_answer_format="markdown",
            keywords=words,
            candidate_urls=[],  # 无 LLM 无法猜 URL
        )


class QueryPlanner:
    """M3 拆 query."""

    def __init__(self, llm: LLMService, tier: Tier = "cheap"):
        self.llm = llm
        self.tier = tier

    async def plan(self, query: str, *, budget=None) -> QueryPlan:
        """拆 query. budget=None 时不累计. 抛 LLMUnavailableError 如果 LLM 没配."""
        if not self.llm.is_available():
            logger.info("LLM not available, using heuristic plan")
            return QueryPlan.fallback(query)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Query: {query}\n\nReturn the research plan JSON."},
        ]

        try:
            resp = await self.llm.complete_json_with_fallback(
                messages, tier=self.tier, temperature=0.3, max_tokens=500,
            )
            if budget is not None:
                try:
                    budget.add(resp.usage if hasattr(resp, "usage") else {})
                except Exception:
                    pass
            return QueryPlan.from_dict(resp)
        except (LLMUnavailableError, json.JSONDecodeError, Exception) as e:
            # M3 调用失败 / JSON 解析失败 → 退化到启发式 (让上层仍能跑)
            logger.warning("QueryPlanner.plan failed (%s), using fallback", type(e).__name__)
            return QueryPlan.fallback(query)
