"""
T36: LLM Provider 抽象层。

不同 LLM 服务的 API 形状不同:
- OpenAI / DeepSeek / Groq / Together:   /chat/completions  (OpenAI 兼容)
- Anthropic:                              /v1/messages       (system 单独字段)
- Google Gemini:                          /v1beta/models/{m}:generateContent
- Ollama:                                 OpenAI 兼容 (默认 http://localhost:11434/v1)

各 provider 实现一个 call() 协议方法 — 接收 messages / model / temperature /
max_tokens / json_mode, 返回 LLMResponse. service.py 负责调度.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from semantic_browser.llm.service import LLMResponse


@runtime_checkable
class LLMProvider(Protocol):
    """最小 Provider 协议 — service.py 只依赖这个接口."""

    name: str

    async def call(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 500,
        json_mode: bool = False,
    ) -> LLMResponse:
        ...


def normalize_messages(
    messages: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """保证 messages 都是 {"role", "content"} dict. 丢掉 None content."""
    out: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content") or ""
        if content == "":
            continue
        out.append({"role": role, "content": content})
    return out


def messages_to_anthropic(
    messages: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """Anthropic API 把 system prompt 单独抽出. 返回 (system, rest_messages)."""
    system = ""
    rest: list[dict[str, str]] = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            rest.append(m)
    return system.strip(), rest


def guess_provider_from_model(model: str) -> str:
    """按 model 名粗略推断 provider — service.py 用在 auto-detect."""
    m = model.lower()
    if any(x in m for x in ("claude", "anthropic")):
        return "anthropic"
    if any(x in m for x in ("gemini", "palm", "bard")):
        return "gemini"
    if ":" in m or "llama" in m or "mistral" in m or "qwen" in m or "phi" in m:
        return "ollama"
    return "openai"


def default_model_for(provider: str, tier: str) -> str:
    """合理的默认 model id — 用户不配 LLM_MODEL_* 时使用."""
    if provider == "anthropic":
        return {
            "cheap": "claude-haiku-4-5",
            "medium": "claude-sonnet-4-6",
            "smart": "claude-sonnet-4-6",
        }.get(tier, "claude-haiku-4-5")
    if provider == "gemini":
        return {
            "cheap": "gemini-2.0-flash",
            "medium": "gemini-2.0-flash",
            "smart": "gemini-2.5-pro",
        }.get(tier, "gemini-2.0-flash")
    if provider == "ollama":
        return {
            "cheap": "qwen2.5:1.5b",
            "medium": "qwen2.5:7b",
            "smart": "qwen2.5:14b",
        }.get(tier, "qwen2.5:7b")
    # openai-compat (DeepSeek/OpenAI/Groq/...)
    return {
        "cheap": "deepseek-chat",
        "medium": "deepseek-chat",
        "smart": "deepseek-chat",
    }.get(tier, "deepseek-chat")
