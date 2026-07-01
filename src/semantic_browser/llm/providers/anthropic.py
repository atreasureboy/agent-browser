"""
T36: Anthropic Messages API provider.

API: POST https://api.anthropic.com/v1/messages
Headers:
  x-api-key: {ANTHROPIC_API_KEY}
  anthropic-version: 2023-06-01
Body:
  { "model": ..., "system": "...", "messages": [...], "max_tokens": N,
    "temperature": X }
Response:
  { "content": [{"type": "text", "text": "..."}], "model": "...",
    "usage": {"input_tokens": N, "output_tokens": N}, ... }

system prompt 单独抽出 — messages 数组里不能有 role=system.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from semantic_browser.llm.types import LLMResponse
from semantic_browser.llm.providers.base import (
    LLMProvider, normalize_messages, messages_to_anthropic,
)

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key)

    async def call(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 500,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self.is_available():
            from semantic_browser.llm.service import LLMUnavailableError
            raise LLMUnavailableError("anthropic: api_key not configured")

        msgs = normalize_messages(messages)
        system, rest = messages_to_anthropic(msgs)
        # Anthropic API 要求 messages 不能全空
        if not rest:
            raise ValueError("Anthropic requires at least one non-system message")

        payload: dict[str, Any] = {
            "model": model,
            "messages": rest,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        # json_mode 不强制 — Anthropic 没 response_format 字段;
        # 我们在 system prompt 里加约束, 让 model 自觉返回 JSON.
        if json_mode:
            json_hint = system + "\n" if system else ""
            payload["system"] = (json_hint + "You MUST respond with valid JSON only, "
                                 "no prose, no markdown code fences.").strip()
            # 把 max_tokens 微调 (Anthropic 对 JSON 严格度低, 需要点空间)
            payload["max_tokens"] = max(payload["max_tokens"], 1024)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/messages",
                headers=headers, json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        # response.content: [{type: "text", text: "..."}]
        content_blocks = data.get("content") or []
        content = "".join(
            b.get("text", "") for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        usage = data.get("usage", {}) or {}
        return LLMResponse(
            content=content,
            model=model,
            tier="",
            usage={
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("input_tokens", 0))
                              + int(usage.get("output_tokens", 0)),
            },
            raw=data,
        )
