"""T36: Provider registry + factory."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from semantic_browser.llm.types import LLMResponse, LLMUnavailableError
from semantic_browser.llm.providers.base import (
    LLMProvider, normalize_messages, messages_to_anthropic,
    guess_provider_from_model, default_model_for,
)
from semantic_browser.llm.providers.openai_compat import OpenAICompatProvider, OllamaProvider
from semantic_browser.llm.providers.anthropic import AnthropicProvider
from semantic_browser.llm.providers.gemini import GeminiProvider

logger = logging.getLogger(__name__)

PROVIDER_NAMES = ("openai", "anthropic", "gemini", "ollama")


def detect_provider(env: Optional[dict[str, str]] = None) -> str:
    """Auto-detect provider from env.

    优先级:
      1. LLM_PROVIDER 显式
      2. 看哪个 API key 存在 (LLM_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY)
      3. fallback openai-compat (默认 DeepSeek)
    """
    env = env or os.environ
    explicit = env.get("LLM_PROVIDER", "").strip().lower()
    if explicit in PROVIDER_NAMES:
        return explicit
    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        return "gemini"
    if env.get("LLM_BASE_URL", "").lower().endswith(":11434/v1"):
        return "ollama"
    return "openai"


def build_provider(
    name: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 30.0,
) -> LLMProvider:
    """工厂 — 按 name 构建 provider. 缺省值从 env 拿.

    注意: 显式传的 api_key (哪怕是空字符串) 会盖掉 env. 测试想关闭 provider
    时传 api_key="" 而不是 None.
    """
    name = name or detect_provider()
    name = name.lower()

    if name == "openai":
        return OpenAICompatProvider(
            api_key=(api_key if api_key is not None
                     else (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", ""))),
            base_url=(base_url if base_url is not None
                      else (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL",
                          "https://api.deepseek.com/v1"))),
            timeout=timeout,
        )
    if name == "anthropic":
        return AnthropicProvider(
            api_key=(api_key if api_key is not None
                     else (os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY", ""))),
            base_url=(base_url if base_url is not None
                      else (os.getenv("LLM_BASE_URL") or "https://api.anthropic.com")),
            timeout=timeout,
        )
    if name == "gemini":
        return GeminiProvider(
            api_key=(api_key if api_key is not None
                     else (os.getenv("LLM_API_KEY")
                           or os.getenv("GEMINI_API_KEY")
                           or os.getenv("GOOGLE_API_KEY", ""))),
            base_url=(base_url if base_url is not None
                      else (os.getenv("LLM_BASE_URL")
                            or "https://generativelanguage.googleapis.com")),
            timeout=timeout,
        )
    if name == "ollama":
        return OllamaProvider(
            api_key=(api_key if api_key is not None
                     else os.getenv("LLM_API_KEY", "")),
            base_url=(base_url if base_url is not None
                      else os.getenv("LLM_BASE_URL", "")),
            timeout=timeout,
        )
    raise ValueError(f"Unknown LLM provider: {name}")


__all__ = [
    "LLMProvider",
    "OpenAICompatProvider",
    "OllamaProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "build_provider",
    "detect_provider",
    "guess_provider_from_model",
    "default_model_for",
    "normalize_messages",
    "PROVIDER_NAMES",
]
