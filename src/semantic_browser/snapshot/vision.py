"""
T38: Vision-based snapshot fallback.

当 DOM snapshot 给不出可用信息时 (canvas / WebGL / shadow DOM / 复杂 SPA),
agent 把截图发给 vision-capable LLM — 让模型直接看图描述页面.

支持 provider:
- Anthropic: Claude 3+ (Sonnet/Haiku/Opus 都支持图像)
- Gemini:    gemini-2.0-flash / gemini-2.5-pro (默认 multimodal)
- OpenAI:    gpt-4o (没显式支持 — 默认走 OpenAI provider 时不接 vision)

返回 VisionSnapshot:
  - description: 自由文本 (LLM 描述页面)
  - elements:    [{label, kind?, region}] 结构化列表 — 给 agent 当 ref 替代
  - model_used:  哪个 vision 模型
  - raw:         LLM 的原始响应 (调试用)
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import httpx

from semantic_browser.llm.types import LLMResponse, LLMUnavailableError
from semantic_browser.llm.providers import build_provider, detect_provider

logger = logging.getLogger(__name__)


@dataclass
class VisionElement:
    """Vision 模型识别的页面元素 — 不像 DOM ref 那么精确, 但可用."""
    label: str
    kind: str = ""         # button / link / input / text (LLM 自由分类)
    region: str = ""       # "top-left" / "header" / "main" / "footer" — LLM 标注


@dataclass
class VisionSnapshot:
    """Vision 模型给的页面快照 — DOM snapshot 不可用时的 fallback."""
    description: str
    elements: list[VisionElement] = field(default_factory=list)
    model_used: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = [self.description]
        if self.elements:
            lines.append("")
            lines.append(f"Elements ({len(self.elements)}):")
            for e in self.elements[:30]:
                kind = f" [{e.kind}]" if e.kind else ""
                lines.append(f"  - {e.label}{kind}")
        return "\n".join(lines)


# 默认 vision model — 按 provider
DEFAULT_VISION_MODEL = {
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-2.0-flash",
}


_VISION_PROMPT = """You are looking at a screenshot of a webpage. Describe what you see — focus on UI elements the user can interact with.

Return JSON in this exact shape:
{{
  "description": "Brief overall description of what this page is (1-2 sentences).",
  "elements": [
    {{"label": "Sign In button top-right", "kind": "button", "region": "header"}},
    {{"label": "Search box center", "kind": "input", "region": "main"}}
  ]
}}

Each element should describe a clearly identifiable UI control (button/link/input/text). Use 'region' to mark which screen area (header/main/sidebar/footer/top-left/etc). Skip decorative elements — only list what a user could click or type into.

{goal_hint}"""


@dataclass
class VisionProvider:
    """vision provider — base64 PNG + prompt 直接走对应 API."""
    name: str
    api_key: str
    base_url: str
    model: str
    timeout: float = 60.0


def _build_vision_provider(
    override_provider: Optional[str] = None,
    override_model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 60.0,
) -> VisionProvider:
    """挑一个 vision-capable provider — 优先 anthropic, 否则 gemini."""
    # 显式给 provider → 用它
    name = (override_provider or detect_provider()).lower()
    if name not in ("anthropic", "gemini"):
        # 强制找 vision-capable 的
        # - 看 env 哪个 key 存在: ANTHROPIC_API_KEY / GEMINI_API_KEY
        import os
        if os.getenv("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            name = "gemini"
        else:
            raise LLMUnavailableError(
                f"Vision snapshot needs anthropic or gemini; got provider={name} "
                f"and no ANTHROPIC_API_KEY/GEMINI_API_KEY in env."
            )
    model = override_model or DEFAULT_VISION_MODEL.get(name, "")
    p = build_provider(name, api_key=api_key, base_url=base_url, timeout=timeout)
    return VisionProvider(
        name=name,
        api_key=p.api_key,
        base_url=p.base_url,
        model=model,
        timeout=timeout,
    )


async def _call_anthropic_vision(
    vp: VisionProvider,
    png_b64: str,
    prompt: str,
) -> LLMResponse:
    """直接打 Anthropic /v1/messages — 带 image block."""
    payload = {
        "model": vp.model,
        "max_tokens": 2048,
        "temperature": 0.2,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/png",
                            "data": png_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": vp.api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=vp.timeout) as client:
        resp = await client.post(
            f"{vp.base_url}/v1/messages",
            headers=headers, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    content_blocks = data.get("content") or []
    content = "".join(
        b.get("text", "") for b in content_blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()
    usage = data.get("usage", {}) or {}
    return LLMResponse(
        content=content,
        model=vp.model,
        tier="vision",
        usage={
            "prompt_tokens": int(usage.get("input_tokens", 0)),
            "completion_tokens": int(usage.get("output_tokens", 0)),
            "total_tokens": int(usage.get("input_tokens", 0))
                           + int(usage.get("output_tokens", 0)),
        },
        raw=data,
    )


async def _call_gemini_vision(
    vp: VisionProvider,
    png_b64: str,
    prompt: str,
) -> LLMResponse:
    """直接打 Gemini :generateContent — 带 inlineData."""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/png", "data": png_b64}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }
    url = (
        f"{vp.base_url}/v1beta/models/{vp.model}:generateContent"
        f"?key={vp.api_key}"
    )
    async with httpx.AsyncClient(timeout=vp.timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        content = ""
    usage = data.get("usageMetadata", {}) or {}
    pt = int(usage.get("promptTokenCount", 0))
    ct = int(usage.get("candidatesTokenCount", 0))
    return LLMResponse(
        content=content,
        model=vp.model,
        tier="vision",
        usage={
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
        raw=data,
    )


def _parse_json_response(content: str) -> dict[str, Any]:
    """从 LLM 输出里抠 JSON — 容忍 ```json 包裹."""
    if "```" in content:
        m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
        if m:
            content = m.group(1).strip()
    return json.loads(content)


async def capture_vision_snapshot(
    controller,                       # BrowserController (avoid import cycle)
    goal: str = "",
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    full_page: bool = True,
) -> VisionSnapshot:
    """截图 → vision model → 结构化描述.

    Args:
        controller: 已启动的 BrowserController
        goal: 可选 — 告诉模型"用户在找什么" (LLM 会针对性突出相关元素)
        provider: 强制选 vision provider (默认 auto-detect)
        model:    强制选模型 (默认按 provider 默认 vision model)
        full_page: True = 整页截图 (可能很长), False = 仅 viewport
    """
    png = await controller.screenshot(full_page=full_page)
    png_b64 = base64.b64encode(png).decode("ascii")
    vp = _build_vision_provider(
        override_provider=provider,
        override_model=model,
    )
    goal_hint = f"The user's current goal is: {goal}" if goal else ""
    prompt = _VISION_PROMPT.format(goal_hint=goal_hint)
    if vp.name == "anthropic":
        resp = await _call_anthropic_vision(vp, png_b64, prompt)
    else:
        resp = await _call_gemini_vision(vp, png_b64, prompt)
    try:
        parsed = _parse_json_response(resp.content)
    except Exception as exc:
        logger.warning("vision parse failed: %s; raw: %s", exc, resp.content[:200])
        # 退路 — 没 JSON 就把整段当 description
        parsed = {"description": resp.content, "elements": []}
    elements = [
        VisionElement(
            label=str(e.get("label", "")).strip()[:200],
            kind=str(e.get("kind", "")).strip()[:30],
            region=str(e.get("region", "")).strip()[:30],
        )
        for e in parsed.get("elements", [])
        if isinstance(e, dict) and e.get("label")
    ]
    return VisionSnapshot(
        description=str(parsed.get("description", "")).strip()[:2000],
        elements=elements,
        model_used=vp.model,
        raw=resp.raw,
    )
