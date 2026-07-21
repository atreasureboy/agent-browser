"""
Link Selector — M3 decides next page to visit.

动机: 多页 query 时, M3 在看完当前页 + relevance 之后, 决定哪个链接最值得继续看.
返回 next URL, 或 'stop' 表示"够了, 别再看了".

比 crawler (BFS) 智能: 不是按 URL 字典序, 而是按 query 相关性.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from semantic_browser.llm.service import LLMService, Tier
from semantic_browser.llm.json_utils import loads_json_strip_fence as _loads_json_strip_fence
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You decide whether to keep digging in a query research session.

Given:
- User query
- Current page URL and title
- List of candidate next URLs (with text excerpts)

Respond with JSON only:
{
  "next_url": "https://... | null",   // null means STOP (enough info already)
  "reason": "brief justification"
}

Rules:
- Return null if:
  * current page likely has enough information
  * no candidate URL seems promising
  * jumping to another page risks losing context (e.g. external domain unrelated)
- Otherwise pick the ONE most useful URL (the one most likely to contain specific evidence
  the current page is missing)
- Prefer same-domain links
- Links to login, signup, settings, "About us", social media usually score low
"""


@dataclass
class CandidateLink:
    """给 link selector 的候选链接."""
    url: str
    text: str       # 链接文本/标题
    excerpt: str    # 周围的文本 (1-2 句), 帮助 LLM 判断相关性
    score_hint: float = 0.0  # 从 relevance filter 拿来的预分数, 0-1

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "text": self.text, "excerpt": self.excerpt}


class LinkSelector:
    """M3 cheap decide-next-page selector."""

    def __init__(self, llm: LLMService, tier: Tier = "cheap"):
        self.llm = llm
        self.tier = tier

    async def pick_next(
        self,
        query: str,
        current_url: str,
        current_title: str,
        candidates: list[CandidateLink],
        *,
        budget=None,
    ) -> str | None:
        """返回 next URL 或 None (=STOP)."""
        if not candidates:
            return None
        # LLM 不可用 (None 或 not available): 用 score_hint 最高的链接
        llm_available = self.llm is not None and self.llm.is_available()
        if not llm_available:
            sorted_cands = sorted(candidates, key=lambda c: c.score_hint, reverse=True)
            if not sorted_cands:
                return None
            return sorted_cands[0].url if sorted_cands[0].score_hint >= 0.3 else None

        cand_payload = [c.to_dict() for c in candidates[:25]]  # 上限 25
        user_prompt = (
            f"Query: {query}\n\n"
            f"Current page: {current_url} (title: {current_title})\n\n"
            f"Candidate next URLs ({len(cand_payload)}):\n"
            f"{json.dumps(cand_payload, ensure_ascii=False)}\n\n"
            f"Return next_url JSON."
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            # T112 audit fix: 同 planner/relevance — complete_json 返 dict 没
            # .usage. 改走底层 complete_with_fallback 拿真 LLMResponse.
            llm_resp = await self.llm.complete_with_fallback(
                messages, tier=self.tier, temperature=0.2, max_tokens=300,
                json_mode=True,
            )
            if budget is not None:
                try:
                    budget.add(getattr(llm_resp, "usage", None) or {})
                except Exception:
                    pass
            resp = _loads_json_strip_fence(llm_resp.content)
            next_url = resp.get("next_url")
            if not next_url or not isinstance(next_url, str):
                return None
            if next_url.lower() in ("null", "none", "stop", ""):
                return None
            return next_url
        except Exception as e:
            logger.warning("LinkSelector M3 call failed: %s; top-score fallback", e)
            sorted_cands = sorted(candidates, key=lambda c: c.score_hint, reverse=True)
            return sorted_cands[0].url if sorted_cands else None


def candidates_from_snapshot(
    snapshot, max_internal: int = 30, score_hints: dict[str, float] | None = None,
) -> list[CandidateLink]:
    """从 PageSnapshot 提 CandidateLink 列表.

    score_hints: optional dict url → score 0-1 (from relevance filter),
                 用来给 M3 提示哪些已经看过且相关
    """
    score_hints = score_hints or {}
    out: list[CandidateLink] = []
    if not snapshot or not getattr(snapshot, "links", None):
        return out
    seen: set[str] = set()
    internal_count = 0
    for ln in snapshot.links:
        href = (getattr(ln, "href", "") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if href in seen:
            continue
        seen.add(href)
        text = (getattr(ln, "text", "") or "").strip()[:120]
        is_internal = getattr(ln, "internal", True)
        if is_internal:
            if internal_count >= max_internal:
                continue
            internal_count += 1
        out.append(CandidateLink(
            url=href,
            text=text,
            excerpt=text,  # 简单起见, 同 text
            score_hint=score_hints.get(href, 0.0),
        ))
    return out
