"""
Synthesizer — M3 把多个 related sections 合成最终 markdown 答案.

动机: relevance filter 留下的 section 还是分散的. 顶层 agent 不想要原始 sections,
它想要一段紧凑 markdown. synthesizer 是 M3 唯一产出"顶层 agent 看见的东西"的地方.

输入: relevance-filter 留下的 sections + sources
输出: ≤ max_chars 的紧凑 markdown 答案 + 引用 [1]/[2]
"""
from __future__ import annotations

import logging
from typing import Any

from semantic_browser.llm.service import LLMService, Tier

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a synthesis agent. Given a user query and a set of relevant excerpts
collected from web pages, produce a concise final answer.

Output rules:
- Markdown format (default) or as user requested
- Use ONLY information from the provided excerpts; do NOT add external knowledge
- Cite sources inline as [1], [2], etc. (corresponds to Sources list, 1-indexed)
- Stay under the max_chars budget
- If the excerpts are insufficient to fully answer, say so explicitly at the start:
  "Note: information may be incomplete"
- Be concise. Prefer bullet points over long paragraphs when content is list-like
"""


class Synthesizer:
    """Final-step synthesizer (M3 cheap)."""

    def __init__(self, llm: LLMService, tier: Tier = "cheap"):
        self.llm = llm
        self.tier = tier

    async def synthesize(
        self,
        query: str,
        excerpts: list[dict[str, Any]],
        sources: list[str],
        *,
        max_chars: int = 2000,
        answer_format: str = "markdown",
        budget=None,
    ) -> str:
        """excerpts: [{heading, text, source_idx}, ...]
        sources:  [url1, url2, ...]
        返回 markdown 答案.
        """
        if not excerpts:
            return "_(No relevant content found in any visited pages.)_"

        if not self.llm.is_available():
            # LLM 不可用, 用模板式拼接 (token 也很省)
            return self._template_synthesize(query, excerpts, sources, max_chars)

        # 把 sources 编号; 给 M3 看
        sources_block = "\n".join(f"[{i + 1}] {url}" for i, url in enumerate(sources))
        excerpts_block_lines = []
        for i, ex in enumerate(excerpts):
            src_idx = ex.get("source_idx", 1)
            heading = (ex.get("heading") or "").strip()[:120]
            text = (ex.get("text") or "").strip()[: max_chars // 2]
            excerpts_block_lines.append(
                f"<excerpt n={i + 1} source=[{src_idx}]>\n"
                f"  heading: {heading}\n"
                f"  text: {text}\n"
                f"</excerpt>"
            )
        excerpts_block = "\n\n".join(excerpts_block_lines)

        user_prompt = (
            f"Query: {query}\n\n"
            f"Answer format: {answer_format}\n"
            f"Max chars: {max_chars}\n\n"
            f"Excerpts ({len(excerpts)}):\n{excerpts_block}\n\n"
            f"Sources:\n{sources_block}\n\n"
            f"Synthesize a concise answer."
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            resp = await self.llm.complete_with_fallback(
                messages, tier=self.tier, temperature=0.2, max_tokens=max_chars // 2,
            )
            if budget is not None:
                try:
                    budget.add(getattr(resp, "usage", {}) or {})
                except Exception:
                    pass
            content = (resp.content or "").strip()
            if not content:
                return self._template_synthesize(query, excerpts, sources, max_chars)
            return content
        except Exception as e:
            logger.warning("Synthesizer M3 call failed: %s; template fallback", e)
            return self._template_synthesize(query, excerpts, sources, max_chars)

    @staticmethod
    def _template_synthesize(
        query: str,
        excerpts: list[dict[str, Any]],
        sources: list[str],
        max_chars: int,
    ) -> str:
        """LLM 不可用 / 调用失败时的极简模板."""
        lines = [f"# {query}", ""]
        chars = len("\n".join(lines))
        for i, ex in enumerate(excerpts):
            heading = (ex.get("heading") or "Excerpt").strip()
            text = (ex.get("text") or "").strip()
            src_idx = ex.get("source_idx", 1)
            block = f"## {heading} [{src_idx}]\n\n{text}\n"
            if chars + len(block) > max_chars:
                # 截断
                remaining = max_chars - chars
                if remaining > 50:
                    block = block[:remaining].rstrip() + "…"
                    lines.append(block)
                break
            lines.append(block)
            chars += len(block)
        if sources:
            lines.append("\n## Sources")
            for i, url in enumerate(sources):
                lines.append(f"[{i + 1}] {url}")
        return "\n".join(lines)
