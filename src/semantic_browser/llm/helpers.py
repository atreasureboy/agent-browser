"""
T24: tier-2 智能辅助 — 用便宜模型干 "需要点智能但不贵" 的活.

这些 helper 解决之前 agent 反馈的几个痛点:
  - #1 snapshot 太大炸 token  → slice_refs_for_goal
  - #4 ref 不稳定              → find_ref_by_label
  - #5 文本提取不结构化        → extract_fields
  - 长文摘要                  → summarize_text

默认全部走 tier="cheap" — 用户可指定 "medium"/"smart" 升级.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from semantic_browser.llm.service import LLMService, Tier
from semantic_browser.snapshot.engine import PageSnapshot

logger = logging.getLogger(__name__)


_SLICE_SYSTEM = """You are a web snapshot ranker. Given a user's goal and a list of interactive elements (refs),
pick the most useful ones for achieving that goal.

Respond with JSON only:
{
  "useful_refs": ["e5", "e12", ...],   // ordered by usefulness, most useful first
  "reason": "short justification"
}

Rules:
- Order by how directly each ref helps achieve the goal
- Skip refs that are clearly irrelevant (footer links, social share, ads)
- Return at most max_refs entries
- Empty list is OK if nothing is relevant
"""


async def slice_refs_for_goal(
    snapshot: PageSnapshot,
    goal: str,
    *,
    max_refs: int = 15,
    llm: LLMService,
    tier: Tier = "cheap",
) -> list[str]:
    """T24: 给 goal + snapshot → top-K 最有用的 ref 列表.

    解决 snapshot 太大炸 LLM context 的问题:
      原本: 500 个 ref 全塞 prompt → token 爆
      现在: 只塞 goal-relevant top-K → token 节约 30x

    Returns ordered list of ref strings (most useful first).
    Empty list if LLM 不可用或返回空.
    """
    # 序列化所有 ref (按 link + control 合并)
    refs_data = []
    for c in snapshot.links:
        refs_data.append({
            "ref": c.ref, "kind": "link",
            "label": (c.text or c.href or "")[:80],
        })
    for c in snapshot.controls:
        refs_data.append({
            "ref": c.ref, "kind": c.kind,
            "label": (c.label or c.placeholder or "")[:80],
        })
    if not refs_data:
        return []
    # 构造 prompt
    user_prompt = f"""Goal: {goal}

Page URL: {snapshot.url}
Page title: {snapshot.title}

Interactive elements ({len(refs_data)} total):
{json.dumps(refs_data, ensure_ascii=False)}

Pick top-{max_refs} most useful refs for the goal. JSON only."""
    try:
        result = await llm.complete_json(
            messages=[
                {"role": "system", "content": _SLICE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            tier=tier,
            temperature=0.1,
            max_tokens=400,
        )
        useful = result.get("useful_refs", [])
        if not isinstance(useful, list):
            return []
        # 验证 ref 确实存在 (防御 prompt injection / 模型乱说)
        valid = {r["ref"] for r in refs_data}
        return [r for r in useful if r in valid][:max_refs]
    except Exception as e:
        logger.warning("slice_refs_for_goal failed: %s", e)
        return []


_SUMMARIZE_SYSTEM = """You are a text summarizer. Given a long text, produce a concise summary.
Keep key facts, names, numbers, dates. Drop boilerplate. Target the requested length.
Respond with JSON only: {"summary": "..."}"""


async def summarize_text(
    text: str,
    *,
    max_chars: int = 500,
    llm: LLMService,
    tier: Tier = "cheap",
) -> str:
    """T24: 长文摘要 → 短文 (默认 500 字符). 解决 LLM context 爆炸."""
    if not text.strip():
        return ""
    if len(text) <= max_chars:
        return text
    user_prompt = f"""Text to summarize ({len(text)} chars, target {max_chars} chars):

{text}

JSON only."""
    try:
        result = await llm.complete_json(
            messages=[
                {"role": "system", "content": _SUMMARIZE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            tier=tier,
            temperature=0.2,
            max_tokens=max_chars * 2,
        )
        return result.get("summary", "")
    except Exception as e:
        logger.warning("summarize_text failed: %s", e)
        return text[:max_chars]  # fallback: 硬截


_EXTRACT_SYSTEM = """You extract structured fields from text.
Given a schema (field names + types) and a text, return matching values.

Respond with JSON only: {"field_name": value, ...}
- If a field is not found, use null
- Type coercion: numbers as JSON numbers, booleans as JSON booleans, strings as JSON strings
- Be conservative: only extract what is clearly present
"""


async def extract_fields(
    text: str,
    schema: dict[str, str],
    *,
    llm: LLMService,
    tier: Tier = "cheap",
) -> dict[str, Any]:
    """T24: 结构化字段抽取.

    Args:
        text: 源文本 (e.g. 页面 markdown)
        schema: 字段名 → 类型描述, e.g. {"name": "str", "price": "float"}

    Returns:
        字段名 → 值. 缺失字段为 None.
    """
    user_prompt = f"""Schema: {json.dumps(schema, ensure_ascii=False)}

Text:
{text[:6000]}

Extract fields. JSON only."""
    try:
        result = await llm.complete_json(
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            tier=tier,
            temperature=0.0,
            max_tokens=600,
        )
        # 确保 schema 字段都在返回里 (缺失填 None)
        return {k: result.get(k) for k in schema.keys()}
    except Exception as e:
        logger.warning("extract_fields failed: %s", e)
        return {k: None for k in schema.keys()}


_FIND_REF_SYSTEM = """You find the ref of an element by its semantic description.
Given a list of refs with their labels/kinds, return the best matching ref.

Respond with JSON only: {"ref": "eN"} or {"ref": null} if no match.
"""


async def find_ref_by_label(
    snapshot: PageSnapshot,
    description: str,
    *,
    llm: LLMService,
    tier: Tier = "cheap",
) -> Optional[str]:
    """T24: ref 跨刷新不稳定 (e1 重新编号). 用语义描述找 ref.

    用法: 用户说 "登录按钮在哪", 调 find_ref_by_label(snapshot, "登录按钮").
    Returns ref 字符串 or None.
    """
    refs_data = []
    for c in snapshot.links:
        refs_data.append({
            "ref": c.ref, "kind": "link",
            "label": (c.text or c.href or "")[:80],
        })
    for c in snapshot.controls:
        refs_data.append({
            "ref": c.ref, "kind": c.kind,
            "label": (c.label or c.placeholder or "")[:80],
        })
    if not refs_data:
        return None
    user_prompt = f"""Element description: {description}

Available refs:
{json.dumps(refs_data, ensure_ascii=False)}

Best matching ref? JSON only."""
    try:
        result = await llm.complete_json(
            messages=[
                {"role": "system", "content": _FIND_REF_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            tier=tier,
            temperature=0.0,
            max_tokens=100,
        )
        ref = result.get("ref")
        valid = {r["ref"] for r in refs_data}
        return ref if ref in valid else None
    except Exception as e:
        logger.warning("find_ref_by_label failed: %s", e)
        return None


def build_smart_snapshot_excerpt(
    snapshot: PageSnapshot,
    useful_refs: list[str],
) -> str:
    """构造给 LLM 看的 snapshot 摘要: URL + title + 仅 useful refs.

    GoalAgent 用这个替代原本的 "flat list of all refs" → 显著省 token.
    """
    useful_set = set(useful_refs)
    lines = []
    for c in snapshot.links:
        if c.ref in useful_set:
            label = (c.text or c.href or "")[:80]
            lines.append(f"  - {c.ref} link: {label}")
    for c in snapshot.controls:
        if c.ref in useful_set:
            label = (c.label or c.placeholder or "")[:80]
            lines.append(f"  - {c.ref} {c.kind}: {label}")
    header = f"URL: {snapshot.url}\nTitle: {snapshot.title}"
    if not lines:
        body = "(no relevant refs found)"
    else:
        body = f"Relevant refs ({len(lines)} shown):\n" + "\n".join(lines)
    return header + "\n\n" + body