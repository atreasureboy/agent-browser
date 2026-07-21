"""
T36: Google Gemini API provider.

API: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}
Body:
  { "contents": [{"role": "user", "parts": [{"text": "..."}]}], "systemInstruction": ...,
    "generationConfig": {"temperature": X, "maxOutputTokens": N},
    "generationConfig.responseMimeType": "application/json" (json_mode) }
Response:
  { "candidates": [{"content": {"parts": [{"text": "..."}], "role": "model"}}],
    "modelVersion": "...", "usageMetadata": { "promptTokenCount": N, ... } }

system prompt 通过 systemInstruction 字段 — 不是 contents[].role=system.
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _to_gemini_contents(
        messages: list[dict[str, str]],
    ) -> tuple[dict | None, list[dict[str, Any]]]:
        """提取 system_instruction + contents 列表.

        Gemini roles: "user" / "model" (没 "system" 没 "assistant").
        """
        system, rest = messages_to_anthropic(messages)  # 同样拆 system
        sys_block = (
            {"parts": [{"text": system}], "role": "system"}
            if system else None
        )
        contents: list[dict[str, Any]] = []
        for m in rest:
            role = "model" if m["role"] in ("assistant", "model") else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        return sys_block, contents

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
            raise LLMUnavailableError("gemini: api_key not configured")

        msgs = normalize_messages(messages)
        sys_instruction, contents = self._to_gemini_contents(msgs)
        if not contents:
            raise ValueError("Gemini requires at least one user message")

        gen_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_mode:
            gen_config["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_config,
        }
        if sys_instruction is not None:
            payload["systemInstruction"] = sys_instruction

        # T116 audit fix: 之前 api_key 拼到 URL ?key=, classify_exception 把
        # 完整 URL 透传给 caller, 4xx 错误时 key 落 log. 改成 x-goog-api-key
        # header (官方 SDK 用的方式), key 不会再出现在 URL / 错误对象 / 日志.
        url = f"{self.base_url}/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": self.api_key}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # candidates[0].content.parts[0].text
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except (KeyError, IndexError):
            content = ""
        usage = data.get("usageMetadata", {}) or {}
        prompt_tokens = int(usage.get("promptTokenCount", 0))
        completion_tokens = int(usage.get("candidatesTokenCount", 0))
        return LLMResponse(
            content=content.strip(),
            model=model,
            tier="",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            raw=data,
        )
