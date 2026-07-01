"""
T38: 视觉快照 fallback — 测试 dataclass / JSON 解析 / provider 选择 / 请求 shape.

不真打外部 API — mock httpx 验证每个 provider 的图像 payload shape.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest


# ── dataclass ───────────────────────────────────────────────

def test_vision_snapshot_summary_includes_description():
    from semantic_browser.snapshot.vision import VisionSnapshot, VisionElement
    vs = VisionSnapshot(
        description="Login page",
        elements=[VisionElement(label="Sign In button", kind="button", region="header")],
        model_used="claude-haiku-4-5",
    )
    s = vs.summary()
    assert "Login page" in s
    assert "Sign In button" in s
    assert "[button]" in s
    assert "claude-haiku-4-5" in vs.model_used


def test_vision_snapshot_to_dict_serializable():
    from semantic_browser.snapshot.vision import VisionSnapshot, VisionElement
    vs = VisionSnapshot(
        description="x",
        elements=[VisionElement(label="Search", region="main")],
        model_used="m",
    )
    d = vs.to_dict()
    assert d["description"] == "x"
    assert d["elements"][0]["label"] == "Search"
    # JSON-serializable
    json.dumps(d)


def test_vision_element_defaults():
    from semantic_browser.snapshot.vision import VisionElement
    e = VisionElement(label="something")
    assert e.kind == ""
    assert e.region == ""
    assert e.label == "something"


# ── _parse_json_response ────────────────────────────────────

def test_parse_json_response_plain():
    from semantic_browser.snapshot.vision import _parse_json_response
    out = _parse_json_response('{"a": 1, "b": [2, 3]}')
    assert out == {"a": 1, "b": [2, 3]}


def test_parse_json_response_with_markdown_fence():
    from semantic_browser.snapshot.vision import _parse_json_response
    out = _parse_json_response('Here you go:\n```json\n{"x": "y"}\n```\nDone.')
    assert out == {"x": "y"}


def test_parse_json_response_with_bare_fence():
    from semantic_browser.snapshot.vision import _parse_json_response
    out = _parse_json_response('```\n{"k": "v"}\n```')
    assert out == {"k": "v"}


# ── provider detection ──────────────────────────────────────

def test_build_vision_provider_anthropic_when_key_present(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    from semantic_browser.snapshot.vision import _build_vision_provider
    vp = _build_vision_provider()
    assert vp.name == "anthropic"
    assert vp.api_key == "ak"
    assert "claude" in vp.model


def test_build_vision_provider_gemini_fallback(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    from semantic_browser.snapshot.vision import _build_vision_provider
    vp = _build_vision_provider()
    assert vp.name == "gemini"
    assert "gemini" in vp.model


def test_build_vision_provider_explicit_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.setenv("GEMINI_API_KEY", "gk")
    from semantic_browser.snapshot.vision import _build_vision_provider
    vp = _build_vision_provider(override_provider="gemini", override_model="gemini-2.5-pro")
    assert vp.name == "gemini"
    assert vp.model == "gemini-2.5-pro"


def test_build_vision_provider_no_vision_capable_raises(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from semantic_browser.snapshot.vision import _build_vision_provider
    from semantic_browser.llm.types import LLMUnavailableError
    with pytest.raises(LLMUnavailableError, match="Vision"):
        _build_vision_provider()


# ── mocked HTTP: Anthropic vision ───────────────────────────

class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, captured: dict[str, Any], response: dict):
        self._captured = captured
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, **kwargs):
        self._captured["url"] = url
        self._captured["kwargs"] = kwargs
        return _FakeHTTPResponse(self._response)


@pytest.mark.asyncio
async def test_call_anthropic_vision_sends_image_block(monkeypatch):
    import httpx
    captured = {}
    response = {
        "content": [{"type": "text",
                     "text": '{"description": "Test page", "elements": []}'}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.snapshot.vision import (
        _call_anthropic_vision, VisionProvider,
    )
    vp = VisionProvider(name="anthropic", api_key="ak",
                        base_url="https://api.anthropic.com",
                        model="claude-haiku-4-5")
    png_b64 = base64.b64encode(b"PNG-DATA").decode("ascii")
    resp = await _call_anthropic_vision(vp, png_b64, "describe this")
    assert "Test page" in resp.content
    body = captured["kwargs"]["json"]
    # image block contains base64 source
    contents = body["messages"][0]["content"]
    img_block = next(c for c in contents if c["type"] == "image")
    assert img_block["source"]["data"] == png_b64
    assert img_block["source"]["media_type"] == "image/png"
    # text block follows
    txt_block = next(c for c in contents if c["type"] == "text")
    assert txt_block["text"] == "describe this"
    assert captured["kwargs"]["headers"]["x-api-key"] == "ak"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"


# ── mocked HTTP: Gemini vision ──────────────────────────────

@pytest.mark.asyncio
async def test_call_gemini_vision_sends_inline_data(monkeypatch):
    import httpx
    captured = {}
    response = {
        "candidates": [{
            "content": {"parts": [{"text": '{"description": "Gemini page"}'}],
                         "role": "model"}
        }],
        "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 80},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.snapshot.vision import _call_gemini_vision, VisionProvider
    vp = VisionProvider(name="gemini", api_key="gk",
                        base_url="https://generativelanguage.googleapis.com",
                        model="gemini-2.0-flash")
    resp = await _call_gemini_vision(vp, "fake-b64", "describe")
    assert "Gemini page" in resp.content
    body = captured["kwargs"]["json"]
    parts = body["contents"][0]["parts"]
    img_part = next(p for p in parts if "inline_data" in p)
    assert img_part["inline_data"]["mime_type"] == "image/png"
    assert img_part["inline_data"]["data"] == "fake-b64"
    # generationConfig 强制 JSON
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "key=gk" in captured["url"]


# ── 解析端到端 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_capture_vision_snapshot_parses_anthropic_response(monkeypatch):
    """端到端: mocked screenshot + mocked anthropic 返回 → VisionSnapshot 结构化."""
    import httpx
    captured = {}
    response = {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "description": "Search engine home",
                "elements": [
                    {"label": "Search box", "kind": "input", "region": "main"},
                    {"label": "I'm feeling lucky button", "kind": "button",
                     "region": "main"},
                ],
            }),
        }],
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.snapshot.vision import capture_vision_snapshot
    # 假 controller — screenshot 返回 PNG bytes
    class FakeController:
        async def screenshot(self, full_page=True):
            return b"FAKE-PNG-BYTES"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    vsnap = await capture_vision_snapshot(
        FakeController(), goal="find search box",
        provider="anthropic", model="claude-haiku-4-5",
    )
    assert vsnap.description == "Search engine home"
    assert vsnap.model_used == "claude-haiku-4-5"
    assert len(vsnap.elements) == 2
    assert vsnap.elements[0].label == "Search box"
    assert vsnap.elements[0].kind == "input"
    assert vsnap.elements[1].region == "main"


@pytest.mark.asyncio
async def test_capture_vision_snapshot_falls_back_on_bad_json(monkeypatch):
    """LLM 返回非 JSON → description 退化成整段文本, elements 空."""
    import httpx
    captured = {}
    response = {
        "content": [{"type": "text", "text": "Sorry, this image is unclear."}],
        "usage": {},
    }
    fake = _FakeAsyncClient(captured, response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    from semantic_browser.snapshot.vision import capture_vision_snapshot
    class FakeController:
        async def screenshot(self, full_page=True):
            return b"X"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak")

    vsnap = await capture_vision_snapshot(
        FakeController(), provider="anthropic", model="claude-haiku-4-5",
    )
    assert "Sorry" in vsnap.description
    assert vsnap.elements == []


# ── MCP 工具注册 ───────────────────────────────────────────

def test_mcp_tool_snapshot_vision_registered():
    from semantic_browser.mcp_server.server import TOOL_DEFINITIONS
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert "sb_snapshot_vision" in names


# ── CLI 注册 ──────────────────────────────────────────────

def test_cli_vision_snapshot_command_registered():
    from semantic_browser.client.cli import tb
    assert "vision-snapshot" in tb.commands
