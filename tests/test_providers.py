"""
T36: LLM provider 多路由 — OpenAI/Anthropic/Gemini/Ollama 的请求 shape 验证.

每个 provider 都用 monkeypatch + fake httpx 类 — 不实际调 API, 只验证:
  - URL 正确
  - headers 正确 (api_key 位置因 provider 而异)
  - request body shape 正确 (messages 拆 system 等因 provider 而异)
  - response 解析正确 (choices vs content vs candidates)
"""

from __future__ import annotations

import json
from typing import Any

import pytest


# ── base helpers ────────────────────────────────────────────

def test_normalize_messages_drops_empty():
    from semantic_browser.llm.providers.base import normalize_messages
    out = normalize_messages([
        {"role": "user", "content": "hi"},
        {"role": "user", "content": ""},         # drop
        {"role": "assistant", "content": "yo"},
        {"role": "system", "content": "sys"},    # keep
        {},
    ])
    assert len(out) == 3
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    assert out[2]["role"] == "system"


def test_messages_to_anthropic_splits_system():
    from semantic_browser.llm.providers.base import messages_to_anthropic
    sys, rest = messages_to_anthropic([
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "no JSON"},
        {"role": "assistant", "content": "..."},
    ])
    assert "be brief" in sys
    assert "no JSON" in sys
    assert len(rest) == 2
    assert all(m["role"] != "system" for m in rest)


def test_guess_provider_from_model():
    from semantic_browser.llm.providers.base import guess_provider_from_model
    assert guess_provider_from_model("claude-3-5-sonnet") == "anthropic"
    assert guess_provider_from_model("gemini-2.0-flash") == "gemini"
    assert guess_provider_from_model("llama3.1:8b") == "ollama"
    assert guess_provider_from_model("qwen2.5:7b") == "ollama"
    assert guess_provider_from_model("deepseek-chat") == "openai"
    assert guess_provider_from_model("gpt-4o") == "openai"


def test_default_model_for_each_provider():
    from semantic_browser.llm.providers.base import default_model_for
    assert "claude" in default_model_for("anthropic", "smart").lower()
    assert "gemini" in default_model_for("gemini", "smart").lower()
    assert default_model_for("ollama", "cheap").endswith(":1.5b") or \
           default_model_for("ollama", "cheap") == "qwen2.5:1.5b"
    assert default_model_for("openai", "cheap") == "deepseek-chat"


# ── detect_provider ─────────────────────────────────────────

def test_detect_provider_explicit(monkeypatch):
    from semantic_browser.llm.providers import detect_provider
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert detect_provider() == "anthropic"


def test_detect_provider_from_anthropic_key(monkeypatch):
    from semantic_browser.llm.providers import detect_provider
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert detect_provider() == "anthropic"


def test_detect_provider_from_gemini_key(monkeypatch):
    from semantic_browser.llm.providers import detect_provider
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    assert detect_provider() == "gemini"


def test_detect_provider_ollama_via_base_url(monkeypatch):
    from semantic_browser.llm.providers import detect_provider
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    assert detect_provider() == "ollama"


def test_detect_provider_fallback_openai(monkeypatch):
    from semantic_browser.llm.providers import detect_provider
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert detect_provider() == "openai"


# ── build_provider ──────────────────────────────────────────

def test_build_provider_anthropic_explicit_key():
    from semantic_browser.llm.providers import build_provider, AnthropicProvider
    p = build_provider("anthropic", api_key="explicit")
    assert isinstance(p, AnthropicProvider)
    assert p.api_key == "explicit"


def test_build_provider_gemini_explicit_key():
    from semantic_browser.llm.providers import build_provider, GeminiProvider
    p = build_provider("gemini", api_key="gkey")
    assert isinstance(p, GeminiProvider)
    assert p.api_key == "gkey"


def test_build_provider_ollama_default_base():
    from semantic_browser.llm.providers import build_provider, OllamaProvider
    p = build_provider("ollama")
    assert isinstance(p, OllamaProvider)
    assert p.base_url == "http://localhost:11434/v1"


def test_build_provider_unknown_raises():
    from semantic_browser.llm.providers import build_provider
    with pytest.raises(ValueError, match="Unknown"):
        build_provider("bogus")


def test_build_provider_openai_explicit_empty_overrides_env(monkeypatch):
    """T36 关键不变量: 显式 api_key='' 应该盖掉 env (测试要用它 disable provider)."""
    from semantic_browser.llm.providers import build_provider
    monkeypatch.setenv("OPENAI_API_KEY", "real-key")
    p = build_provider("openai", api_key="", base_url="")
    assert p.api_key == ""      # 不是 env 的 "real-key"
    assert not p.is_available()


# ── OpenAICompatProvider (mocked HTTP) ──────────────────────

class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, captured: dict[str, Any], response_payload: dict):
        self._captured = captured
        self._response = response_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, **kwargs):
        self._captured["url"] = url
        self._captured["headers"] = kwargs.get("headers")
        self._captured["json"] = kwargs.get("json")
        return _FakeHTTPResponse(self._response)


@pytest.mark.asyncio
async def test_openai_compat_provider_call_shape(monkeypatch):
    import httpx
    captured = {}
    response = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.llm.providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(api_key="k", base_url="http://x/v1")
    resp = await p.call(
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "hi"}],
        model="m", temperature=0.5, max_tokens=100, json_mode=True,
    )
    assert resp.content == "hello"
    assert resp.model == "m"
    assert captured["url"] == "http://x/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    body = captured["json"]
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}
    # system + user 都进 messages (OpenAI 支持)
    assert {m["role"] for m in body["messages"]} == {"system", "user"}


# ── AnthropicProvider (mocked HTTP) ─────────────────────────

@pytest.mark.asyncio
async def test_anthropic_provider_call_shape(monkeypatch):
    import httpx
    captured = {}
    response = {
        "content": [{"type": "text", "text": "anthropic says hi"}],
        "model": "claude-3-5-sonnet",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.llm.providers.anthropic import AnthropicProvider
    p = AnthropicProvider(api_key="aKey")
    resp = await p.call(
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "hi"}],
        model="claude-3-5-sonnet", temperature=0.5, max_tokens=100,
    )
    assert resp.content == "anthropic says hi"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "aKey"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["json"]
    # system 单独抽出
    assert body["system"] == "be brief"
    # messages 不含 system
    assert all(m["role"] != "system" for m in body["messages"])
    assert body["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_anthropic_provider_json_mode_injects_hint(monkeypatch):
    """Anthropic 没 response_format — json_mode 走 system hint."""
    import httpx
    captured = {}
    response = {"content": [{"type": "text", "text": "{}"}], "usage": {}}
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.llm.providers.anthropic import AnthropicProvider
    p = AnthropicProvider(api_key="k")
    await p.call(
        [{"role": "user", "content": "give json"}],
        model="claude-3-5-sonnet", json_mode=True, max_tokens=300,
    )
    body = captured["json"]
    assert "JSON" in body.get("system", "")
    assert body["max_tokens"] >= 1024   # 调高给空间


@pytest.mark.asyncio
async def test_anthropic_provider_requires_non_system_message(monkeypatch):
    """纯 system prompt → 报错."""
    from semantic_browser.llm.providers.anthropic import AnthropicProvider
    p = AnthropicProvider(api_key="k")
    with pytest.raises(ValueError, match="at least one"):
        await p.call([{"role": "system", "content": "just system"}], model="m")


# ── GeminiProvider (mocked HTTP) ────────────────────────────

@pytest.mark.asyncio
async def test_gemini_provider_call_shape(monkeypatch):
    import httpx
    captured = {}
    response = {
        "candidates": [{
            "content": {"parts": [{"text": "gemini hi"}], "role": "model"}
        }],
        "modelVersion": "gemini-2.0-flash",
        "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 9},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.llm.providers.gemini import GeminiProvider
    p = GeminiProvider(api_key="gkey")
    resp = await p.call(
        [{"role": "system", "content": "be brief"},
         {"role": "user", "content": "hi"}],
        model="gemini-2.0-flash", temperature=0.3, max_tokens=200, json_mode=True,
    )
    assert resp.content == "gemini hi"
    assert "?" in captured["url"]
    assert "key=gkey" in captured["url"]
    assert "/v1beta/models/gemini-2.0-flash:generateContent" in captured["url"]
    body = captured["json"]
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["contents"][0]["role"] == "user"
    assert body["generationConfig"]["temperature"] == 0.3
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    # 用法计数
    assert resp.usage["prompt_tokens"] == 8
    assert resp.usage["completion_tokens"] == 9


# ── OllamaProvider (subclass of OpenAI-compat) ──────────────

def test_ollama_provider_uses_default_localhost_base():
    from semantic_browser.llm.providers.openai_compat import OllamaProvider
    p = OllamaProvider()
    assert p.base_url == "http://localhost:11434/v1"
    assert p.api_key == "ollama"


# ── LLMService 集成 (provider 路由) ─────────────────────────

def test_llm_service_selects_provider_explicit():
    from semantic_browser.llm import LLMService
    from semantic_browser.llm.providers import AnthropicProvider
    svc = LLMService(provider="anthropic", api_key="x")
    assert isinstance(svc.provider, AnthropicProvider)
    assert svc.provider_name == "anthropic"
    assert "claude" in svc.model_for("smart").lower()


def test_llm_service_gemini_default_models():
    from semantic_browser.llm import LLMService
    svc = LLMService(provider="gemini", api_key="x")
    assert "gemini" in svc.model_for("cheap").lower()
    assert "gemini" in svc.model_for("smart").lower()


def test_llm_service_stats_includes_provider():
    from semantic_browser.llm import LLMService
    svc = LLMService(provider="anthropic", api_key="x")
    stats = svc.stats()
    assert stats["provider"] == "anthropic"
    assert "models" in stats
    assert "call_counts" in stats


def test_llm_service_unavailable_message_mentions_provider():
    from semantic_browser.llm import LLMService
    svc = LLMService(provider="gemini", api_key="", base_url="")
    assert svc.is_available() is False
    import asyncio
    from semantic_browser.llm import LLMUnavailableError
    with pytest.raises(LLMUnavailableError) as exc_info:
        asyncio.run(svc.complete(
            [{"role": "user", "content": "x"}],
            tier="cheap",
        ))
    assert "gemini" in str(exc_info.value).lower()
