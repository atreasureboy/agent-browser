"""
Relevance Filter — M3 给每个 page section 打相关性分 (0-1),
留下有用的, 丢掉无用的, 给 synthesizer 喂精炼素材.

动机: 一个 SPA 页面可能有 100+ sections, 跟 query 相关的只有 5 个.
M3 cheap 模型跑一次 relevance ranking, 比把 50KB 内容全部推到 synthesizer 烧 token 省得多.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from semantic_browser.llm.service import LLMService, Tier

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a relevance scorer. Given a user query and a list of page sections
(each with index, heading, and short excerpt), score each section's relevance to the query.

Respond with JSON only:
{
  "scores": [
    {"i": 0, "score": 0.85, "why": "directly addresses query"},
    {"i": 1, "score": 0.10, "why": "navigation menu, irrelevant"}
  ],
  "overall": 0.65,    // overall page relevance to query, 0-1
  "useful": true      // false if no section is relevant
}

Rules:
- Score 0.0 to 1.0 per section. 0.0 = completely irrelevant. 1.0 = exact answer
- "overall" is your confidence the page contains enough info to (partially) answer the query
- Return scores for ALL sections in order
- Be strict: nav, footer, ads, sidebar score 0.0
"""


@dataclass
class SectionInput:
    """喂给 relevance filter 的单个 section."""
    index: int
    heading: str
    excerpt: str  # 简短摘录 (≤ 200 chars)
    link_href: str = ""  # 如果这个 section 是链接

    def to_dict(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "heading": self.heading,
            "excerpt": self.excerpt,
            "href": self.link_href,
        }


@dataclass
class RelevanceResult:
    scored: list[tuple[int, float]] = field(default_factory=list)  # [(section_idx, score)]
    overall: float = 0.0
    useful: bool = False
    raw_response: dict = field(default_factory=dict)

    def kept(self, threshold: float = 0.3) -> list[int]:
        """score >= threshold 的 section index 列表, 按 score 降序."""
        return [i for i, s in sorted(self.scored, key=lambda x: -x[1]) if s >= threshold]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored": [{"i": i, "score": s} for i, s in self.scored],
            "overall": self.overall,
            "useful": self.useful,
        }


class RelevanceFilter:
    """M3 cheap relevance scorer."""

    def __init__(self, llm: LLMService, tier: Tier = "cheap", threshold: float = 0.3):
        self.llm = llm
        self.tier = tier
        self.threshold = threshold

    async def score(
        self,
        query: str,
        sections: list[SectionInput],
        *,
        budget=None,
    ) -> RelevanceResult:
        """M3 给 sections 打分.

        sections 太多 (>30) 时切片, 因为单次 prompt 装不下.
        budget 累计 LLM usage (可选).
        """
        if not sections:
            return RelevanceResult(useful=False)

        if not self.llm.is_available():
            logger.info("LLM not available; falling back to keyword match")
            return self._keyword_fallback(query, sections)

        # 分批, 避免 prompt 爆炸
        BATCH = 25
        batches = [sections[i:i + BATCH] for i in range(0, len(sections), BATCH)]

        all_scored: list[tuple[int, float]] = []
        overall_max = 0.0
        any_useful = False

        for batch in batches:
            payload = [s.to_dict() for s in batch]
            user_prompt = (
                f"Query: {query}\n\n"
                f"Sections ({len(batch)} items):\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                f"Return relevance JSON."
            )
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            try:
                resp = await self.llm.complete_json_with_fallback(
                    messages, tier=self.tier, temperature=0.1, max_tokens=1500,
                )
                if budget is not None:
                    try:
                        budget.add(getattr(resp, "usage", {}) or {})
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Relevance M3 call failed: %s; keyword fallback", e)
                # 这一批退到 keyword fallback, 不影响其它批
                for s in batch:
                    score = self._keyword_score(query, s.excerpt + " " + s.heading)
                    all_scored.append((s.index, score))
                    if score >= self.threshold:
                        any_useful = True
                    overall_max = max(overall_max, score)
                continue

            # 正常返回
            scores_list = resp.get("scores", []) or []
            for entry in scores_list:
                idx_local = int(entry.get("i", -1))
                score = float(entry.get("score", 0.0))
                if 0 <= idx_local < len(batch):
                    global_idx = batch[idx_local].index
                    all_scored.append((global_idx, max(0.0, min(1.0, score))))
                    overall_max = max(overall_max, score)
                    if score >= self.threshold:
                        any_useful = True

        # M3 报告的 overall (取最后一批 response 里的; 因为它反映全页总体)
        # 多批时不依赖 overall, 用 all_scored max
        result = RelevanceResult(
            scored=all_scored,
            overall=overall_max,
            useful=any_useful,
        )
        return result

    def _keyword_fallback(self, query: str, sections: list[SectionInput]) -> RelevanceResult:
        """无 LLM 时的关键词打分."""
        scored = []
        any_useful = False
        overall_max = 0.0
        for s in sections:
            score = self._keyword_score(query, s.heading + " " + s.excerpt)
            scored.append((s.index, score))
            if score >= self.threshold:
                any_useful = True
            overall_max = max(overall_max, score)
        return RelevanceResult(scored=scored, overall=overall_max, useful=any_useful)

    @staticmethod
    def _keyword_score(query: str, text: str) -> float:
        """极简 keyword 命中打分: 每个 query token 命中 text 得 0.2, 满 1.0."""
        q_tokens = {t.lower() for t in re.findall(r"\w+", query) if len(t) >= 2}
        if not q_tokens:
            return 0.0
        text_lower = text.lower()
        hits = sum(1 for t in q_tokens if t in text_lower)
        return min(1.0, hits / max(3, len(q_tokens)))
