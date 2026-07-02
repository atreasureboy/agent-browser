"""
MCP Server 测试 — 不启动浏览器。

测 JSON-RPC 协议层: 请求校验、错误码、tool dispatch 表。
Tool 内部通过 mock controller / mock engine 走通。
"""
from __future__ import annotations

import json

import pytest


def _unwrap(inner):
    """T48: 工具响应包了 Result envelope, 返回 data 部分."""
    if isinstance(inner, dict) and "ok" in inner and "data" in inner:
        return inner["data"]
    return inner



from semantic_browser.mcp_server.server import (
    MCPServer,
    TOOL_DEFINITIONS,
    SERVER_INFO,
    PROTOCOL_VERSION,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
)


# ── 协议层 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMCPProtocol:
    async def test_initialize(self):
        srv = MCPServer()
        resp = await srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert resp["result"]["serverInfo"] == SERVER_INFO
        assert "tools" in resp["result"]["capabilities"]

    async def test_ping(self):
        srv = MCPServer()
        resp = await srv.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert resp["id"] == 2
        assert resp["result"] == {}

    async def test_tools_list_returns_all(self):
        srv = MCPServer()
        resp = await srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        assert names == [t["name"] for t in TOOL_DEFINITIONS]
        # 必有这五个
        for required in ("sb_browse", "sb_snapshot", "sb_click", "sb_type", "sb_scroll"):
            assert required in names

    async def test_method_not_found(self):
        srv = MCPServer()
        resp = await srv.handle({"jsonrpc": "2.0", "id": 4, "method": "nonsense"})
        assert resp["error"]["code"] == METHOD_NOT_FOUND
        assert resp["error"]["message"] == "Method not found: nonsense"

    async def test_invalid_jsonrpc_version(self):
        srv = MCPServer()
        resp = await srv.handle({"id": 5, "method": "ping"})
        assert resp["error"]["code"] == INVALID_REQUEST

    async def test_non_dict_request(self):
        srv = MCPServer()
        resp = await srv.handle("not a dict")
        assert resp["error"]["code"] == INVALID_REQUEST

    async def test_non_string_method(self):
        srv = MCPServer()
        resp = await srv.handle({"jsonrpc": "2.0", "id": 6, "method": 123})
        assert resp["error"]["code"] == INVALID_REQUEST

    async def test_notification_returns_none(self):
        srv = MCPServer()
        # 通知型方法 (没有 id, 以 notifications/ 开头)
        resp = await srv.handle({"jsonrpc": "2.0", "method": "notifications/something"})
        assert resp is None


# ── Tool dispatch ─────────────────────────────────────────────

class _FakeEngine:
    """满足所有 _call_tool 路径的最小替身。"""
    def __init__(self):
        self.controller = _FakeController()
        self.browse_calls = []
        self._started = False

    async def start(self):
        self._started = True

    async def close(self):
        self._started = False

    async def browse(self, url, extract_content=True):
        from semantic_browser.engine import BrowseResult
        from semantic_browser.snapshot.engine import PageSnapshot, TextBlock
        from semantic_browser.classifier.heuristic import ClassificationResult
        self.browse_calls.append(url)
        return BrowseResult(
            url=url,
            snapshot=PageSnapshot(
                url=url, title="T", domain="x.com",
                text_blocks=[TextBlock(tag="h1", text="Hello")],
            ),
            classification=ClassificationResult(page_type="article", confidence=0.9, reason="", signals=[]),
            article=None, interfaces=None, elapsed=0.1,
        )

    def get_site_graph(self, root_url):
        from semantic_browser.graph.builder import SiteGraph, GraphNode
        return SiteGraph(
            domain="x.com",
            nodes={root_url: GraphNode(url=root_url, title="Home", page_type="article", depth=0, visited=True)},
            edges=[],
            root_url=root_url,
        )

    def get_visited_pages(self, domain=""):
        return []

    def get_memory_stats(self):
        return {"pages": 0, "links": 0, "domains": 0, "actions": 0, "sessions": 0}


class _FakeController:
    def __init__(self):
        self.url = "https://x.com/"
        self.clicked = []
        self.typed = []
        self.scrolled = []
        self._page = _FakePage()

    async def click(self, ref):
        self.clicked.append(ref)
        return True

    async def type_text(self, ref, text):
        self.typed.append((ref, text))
        return True

    async def scroll(self, direction, amount):
        self.scrolled.append((direction, amount))

    async def back(self):
        self.url = "about:blank"

    async def forward(self):
        self.url = "https://x.com/fwd"

    async def screenshot(self, path=None):
        return b"\x89PNG\r\n\x1a\n" + b"fake"

    async def press_key(self, key):
        pass

    async def open(self, url):
        self.url = url
        self._page = _FakePage(url=url)

    async def get_url(self):
        return self.url

    @property
    def current_page(self):
        return self._page


class _FakePage:
    def __init__(self, url="https://x.com/"):
        self.url = url


@pytest.mark.asyncio
class TestMCPToolDispatch:
    async def test_sb_browse_calls_engine(self, monkeypatch):
        from semantic_browser.snapshot.engine import SnapshotEngine
        async def fake_capture(self, base_url=""):
            from semantic_browser.snapshot.engine import PageSnapshot, TextBlock
            return PageSnapshot(url="https://x.com/", title="T", domain="x.com",
                                 text_blocks=[TextBlock(tag="h1", text="H")])
        monkeypatch.setattr(SnapshotEngine, "__init__", lambda self, page: None)
        monkeypatch.setattr(SnapshotEngine, "capture", fake_capture)

        srv = MCPServer(engine=_FakeEngine())
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "sb_browse", "arguments": {"url": "https://x.com"}},
        })
        assert resp["id"] == 10
        assert resp["result"]["isError"] is False
        assert resp["result"]["content"][0]["type"] == "text"
        # T48: 解析内层 JSON envelope, data 字段是工具输出
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["classification"]["page_type"] == "article"

    async def test_sb_snapshot(self, monkeypatch):
        from semantic_browser.snapshot.engine import SnapshotEngine
        async def fake_capture(self, base_url=""):
            from semantic_browser.snapshot.engine import PageSnapshot, TextBlock
            return PageSnapshot(url="https://x.com/", title="T", domain="x.com",
                                 text_blocks=[TextBlock(tag="h1", text="H")])
        monkeypatch.setattr(SnapshotEngine, "__init__", lambda self, page: None)
        monkeypatch.setattr(SnapshotEngine, "capture", fake_capture)

        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "sb_snapshot", "arguments": {"url": "https://x.com"}},
        })
        assert resp["result"]["isError"] is False
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert "text_blocks" in _unwrap(inner)

    async def test_sb_click(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"name": "sb_click", "arguments": {"ref": "e3"}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["success"] is True
        assert _unwrap(inner)["ref"] == "e3"
        assert engine.controller.clicked == ["e3"]

    async def test_sb_type(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 13, "method": "tools/call",
            "params": {"name": "sb_type", "arguments": {"ref": "e3", "text": "hi"}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["success"] is True
        assert _unwrap(inner)["text_length"] == 2

    async def test_sb_scroll(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 14, "method": "tools/call",
            "params": {"name": "sb_scroll", "arguments": {"direction": "down", "amount": 200}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["direction"] == "down"
        assert _unwrap(inner)["amount"] == 200

    async def test_sb_back(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 15, "method": "tools/call",
            "params": {"name": "sb_back", "arguments": {}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["url"] == "about:blank"

    async def test_sb_forward(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 16, "method": "tools/call",
            "params": {"name": "sb_forward", "arguments": {}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["url"] == "https://x.com/fwd"

    async def test_sb_screenshot_to_base64(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 17, "method": "tools/call",
            "params": {"name": "sb_screenshot", "arguments": {}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["bytes"] > 0
        assert _unwrap(inner)["base64"]  # non-empty base64 string

    async def test_sb_press_key(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 18, "method": "tools/call",
            "params": {"name": "sb_press_key", "arguments": {"key": "Enter"}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["key"] == "Enter"

    async def test_sb_graph(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 19, "method": "tools/call",
            "params": {"name": "sb_graph", "arguments": {"url": "https://x.com"}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert _unwrap(inner)["domain"] == "x.com"

    async def test_sb_history(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "sb_history", "arguments": {}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert "pages" in _unwrap(inner)
        assert "count" in _unwrap(inner)

    async def test_sb_stats(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {"name": "sb_stats", "arguments": {}},
        })
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert "pages" in _unwrap(inner)

    async def test_unknown_tool_returns_invalid_params(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 22, "method": "tools/call",
            "params": {"name": "sb_nope", "arguments": {}},
        })
        # T48: tool 错误包成 Result envelope, MCP 层 isError=true
        assert resp["result"]["isError"] is True
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert inner["ok"] is False
        assert inner["error"]["code"] == "MISSING_PARAM"

    async def test_missing_required_argument_returns_invalid_params(self):
        engine = _FakeEngine()
        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 23, "method": "tools/call",
            "params": {"name": "sb_click", "arguments": {}},
        })
        # T48: KeyError("ref") → classify_exception → MISSING_PARAM
        assert resp["result"]["isError"] is True
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert inner["error"]["code"] == "MISSING_PARAM"

    async def test_params_not_dict_returns_invalid_params(self):
        # params 层校验在 tool dispatch 之前 — 仍是 MCP 协议层 INVALID_PARAMS
        srv = MCPServer(engine=_FakeEngine())
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 24, "method": "tools/call",
            "params": "not an object",
        })
        assert resp["error"]["code"] == INVALID_PARAMS

    async def test_name_not_string_returns_invalid_params(self):
        srv = MCPServer(engine=_FakeEngine())
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 25, "method": "tools/call",
            "params": {"name": 123, "arguments": {}},
        })
        assert resp["error"]["code"] == INVALID_PARAMS

    async def test_internal_error_returns_internal_error_code(self, monkeypatch):
        engine = _FakeEngine()
        async def boom(*a, **kw): raise RuntimeError("explode")
        monkeypatch.setattr(engine, "browse", boom)

        srv = MCPServer(engine=engine)
        resp = await srv.handle({
            "jsonrpc": "2.0", "id": 26, "method": "tools/call",
            "params": {"name": "sb_browse", "arguments": {"url": "https://x.com"}},
        })
        # T48: 工具异常 → Result envelope, MCP isError=true
        assert resp["result"]["isError"] is True
        inner = json.loads(resp["result"]["content"][0]["text"])
        assert inner["error"]["code"] == "INTERNAL"
        assert "explode" in inner["error"]["message"]


# ── _error / _ok helpers ──────────────────────────────────────

class TestMCPRenderHelpers:
    def test_ok_envelope(self):
        r = MCPServer._ok(7, {"x": 1})
        assert r == {"jsonrpc": "2.0", "id": 7, "result": {"x": 1}}

    def test_error_envelope(self):
        r = MCPServer._error(8, -32601, "no")
        assert r == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32601, "message": "no"}}