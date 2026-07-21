"""
T36: OpenAI 兼容 provider — 覆盖 DeepSeek / OpenAI / Groq / Together / LM Studio / Ollama.

API: POST {base_url}/chat/completions
Auth: Authorization: Bearer {api_key}
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from semantic_browser.llm.types import LLMResponse, LLMUnavailableError  # T114 audit fix: 提到 module-level, 让 empty-choices 分支能用

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """OpenAI 兼容 chat completions 协议 — DeepSeek/Groq/Together/Ollama 都用这个 shape."""

    name = "openai"

    def __init__(self, api_key: str, base_url: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

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
            raise LLMUnavailableError("OpenAI-compat: api_key / base_url not configured")

        msgs = normalize_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers, json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        # T114 audit fix: 之前 data["choices"][0]["message"]["content"] 在
        # choices=[] / choices=[{}] 时 IndexError/KeyError, propagate 成
        # INTERNAL (500). 实际是 provider 返回了空或畸形响应 — 应该视为
        # 临时失败 → raise LLMUnavailableError 让上层 retry / fallback.
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailableError(
                f"openai compat: empty choices in response (model={model})"
            )
        first = choices[0] or {}
        message = first.get("message") or {}
        raw_content = message.get("content")
        content = (raw_content or "").strip() if isinstance(raw_content, str) else ""
        usage = data.get("usage", {}) or {}
        return LLMResponse(
            content=content,
            model=model,
            tier="",  # 上层 service 知道 tier
            usage={
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
            },
            raw=data,
        )


# Ollama 暴露 OpenAI 兼容的 /v1/chat/completions — 直接复用上面类
class OllamaProvider(OpenAICompatProvider):
    name = "ollama"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"

    def __init__(self, api_key: str = "ollama", base_url: str = "",
                 timeout: float = 30.0) -> None:
        super().__init__(
            api_key=api_key or "ollama",
            base_url=base_url or self.DEFAULT_BASE_URL,
            timeout=timeout,
        )
