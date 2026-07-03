"""C — MCP 39+ 工具 round-trip 契约测试.

每个工具走一次 happy-path: 合法参数 → tools/call → 解析 content[0].text,
验证:
1. JSON-RPC 2.0 envelope (id 对应 / result 存在)
2. isError=False
3. content[0].type == "text"
4. 解析后 inner 是合法 JSON
5. inner 是 dict (返回结构稳定)
6. tools/list 必须包含该工具名

跳过 5 个 LLM-driven 工具 (sb_agent_run, sb_agent_plan, sb_discover,
sb_snapshot_vision, sb_safety_check) — 它们真调用 GoalAgent / capture_vision_snapshot,
C 测试是契约测试, 不验 LLM 推理; D 测试会单独跑.

daemon-only 工具 (sb_sessions_*, sb_capacity, sb_admin_*, sb_queue, sb_health)
走 _daemon_get/post/delete, monkeypatch 掉 URL 让它直接返回占位结果.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from semantic_browser.mcp_server.server import (
    MCPServer,
    TOOL_DEFINITIONS,
)


# ── 工具元数据: 哪些要测, 用什么 args ──────────────────────

# LLM-driven, C 跳过
_LLM_DRIVEN = frozenset({
    "sb_agent_run", "sb_agent_plan", "sb_discover",
    "sb_snapshot_vision", "sb_safety_check",
})

# 每个非 LLM 工具的最小合法 args (按 inputSchema 必填)
_TOOL_ARGS: dict[str, dict] = {
    # 基础浏览
    "sb_browse": {"url": "data:text/html,<h1>x</h1>"},
    "sb_snapshot": {"url": "data:text/html,<h1>x</h1>"},
    "sb_click": {"ref": "e1"},
    "sb_type": {"ref": "e1", "text": "hello"},
    "sb_scroll": {"direction": "down", "amount": 200},
    "sb_back": {},
    "sb_forward": {},
    "sb_screenshot": {},
    "sb_press_key": {"key": "Enter"},
    "sb_graph": {"url": "data:text/html,x"},
    # 记忆 / 历史
    "sb_history": {},
    "sb_stats": {},
    "sb_memory_lookup": {"goal": "test goal"},
    "sb_memory_stats": {},
    # T39: 深度调试
    "sb_snapshot_deep": {},
    "sb_get_response_headers": {"url": "data:text/html,x"},
    "sb_get_dom_diff": {},
    "sb_get_script_source": {"url": "data:text/html,x"},
    # T40 安全
    "sb_storage": {},
    "sb_security_headers": {"url": "data:text/html,x"},
    "sb_probe_paths": {"url": "data:text/html,x"},
    "sb_list_frames": {},
    "sb_switch_frame": {"name_or_url": "main"},
    "sb_extract_api_endpoints": {},
    "sb_get_websockets": {},
    # T42
    "sb_extract_js_libraries": {},
    "sb_detect_graphql": {"endpoint": "data:text/html,x"},
    # T43
    "sb_enumerate_subdomains": {"host": "example.com"},
    "sb_extract_secrets_from_js": {},
    "sb_detect_waf": {},
    "sb_find_open_redirect_sinks": {},
    "sb_find_disclosure": {},
    "sb_analyze_exposed_files": {},
    "sb_discover_api_specs": {},
    "sb_tls_subdomains": {"host": "example.com"},
    "sb_fingerprint_tech": {},
    "sb_decode_jwts": {},
    # T44
    "sb_dns_records": {"host": "example.com"},
    "sb_wayback_urls": {"url": "data:text/html,x"},
    "sb_find_xss_sinks": {},
    "sb_detect_auth_methods": {},
    "sb_check_csrf_coverage": {},
    "sb_find_idor_urls": {},
    "sb_find_cloud_resources": {},
    "sb_probe_http_methods": {},
    "sb_detect_2fa": {},
    "sb_inventory_external_resources": {},
    "sb_parse_csp": {},
    "sb_check_subdomain_takeover": {},
    # T47
    "sb_a11y_audit": {},
    # T18 调试
    "sb_get_console": {},
    "sb_get_network": {},
    "sb_get_page_errors": {},
    # T57 daemon-only
    "sb_sessions_list": {},
    "sb_sessions_create": {},
    "sb_sessions_delete": {"name": "default"},
    "sb_capacity": {},
    "sb_admin_degrade": {"level": 1},
    "sb_admin_restore": {},
    "sb_queue": {},
    "sb_health": {},
}


# ── Fixtures ──────────────────────────────────────────────


def _build_fake_engine() -> MagicMock:
    """大而全的 engine mock — 所有 controller 方法都返回合理占位 dict.

    MCP 工具 handler 调 controller 上的方法, 不需要真实 Playwright. C 测试
    只关心协议层契约 (response envelope), 不验工具内部 I/O.
    """
    eng = MagicMock(name="engine")
    ctrl = MagicMock(name="controller")
    eng.controller = ctrl
    eng.start = AsyncMock(return_value=None)
    # sb_browse 调用 engine.browse(url) → 返回对象有 to_dict()
    browse_result = MagicMock()
    browse_result.to_dict.return_value = {
        "url": "data:text/html,x",
        "snapshot": {"title": "T"},
        "classification": {"page_type": "article", "confidence": 0.9},
    }
    eng.browse = AsyncMock(return_value=browse_result)
    # 所有 controller 方法 (sync/async 混) — 全部返回合理占位
    for attr in (
        # 基础
        "click", "type_text", "scroll", "back", "forward", "screenshot",
        "press_key", "open", "get_url", "current_page",
        # T39
        "get_response_headers", "get_dom_diff", "fetch_script_source",
        # T40
        "get_storage", "get_security_headers", "probe_paths",
        "list_frames", "switch_frame", "extract_api_endpoints", "get_websockets",
        # T42
        "extract_js_libraries", "detect_graphql",
        # T43
        "enumerate_subdomains", "extract_secrets_from_js", "detect_waf",
        "find_open_redirect_sinks", "find_disclosure", "analyze_exposed_files",
        "discover_api_specs", "tls_subdomains", "fingerprint_tech", "decode_jwts",
        # T44
        "dns_records", "wayback_urls", "find_xss_sinks", "detect_auth_methods",
        "check_csrf_coverage", "find_idor_urls", "find_cloud_resources",
        "probe_http_methods", "detect_2fa", "inventory_external_resources",
        "parse_csp", "check_subdomain_takeover",
        # T47
        "a11y_audit",
        # T18
        "get_console_messages", "get_network_requests", "get_page_errors",
    ):
        mock = AsyncMock(return_value={}) if attr not in (
            "current_page", "get_websockets",
            "get_console_messages", "get_network_requests", "get_page_errors",
        ) else MagicMock(return_value={})
        setattr(ctrl, attr, mock)
    # screenshot 是 binary, 占位用 b"x"
    ctrl.screenshot = AsyncMock(return_value=b"x")
    # controller.open 返回 fake page (sb_snapshot 用)
    page = MagicMock()
    page.url = "data:text/html,x"
    ctrl.open = AsyncMock(return_value=page)
    ctrl.current_page = page
    return eng


def _build_fake_engine_no_controller() -> MagicMock:
    """sb_graph/sb_history/sb_stats 不走 controller, 走 engine 上的方法."""
    eng = MagicMock(name="engine")
    # 不设 controller — 这些方法不会触发它
    from semantic_browser.graph.builder import SiteGraph, GraphNode
    eng.get_site_graph.return_value = SiteGraph(
        domain="x.com",
        nodes={"data:text/html,x": GraphNode(
            url="data:text/html,x", title="T",
            page_type="article", depth=0, visited=True,
        )},
        edges=[],
        root_url="data:text/html,x",
    )
    eng.get_visited_pages.return_value = []
    eng.get_memory_stats.return_value = {
        "pages": 0, "links": 0, "domains": 0, "actions": 0, "sessions": 0,
    }
    return eng


@pytest.fixture
def fake_engine() -> MagicMock:
    return _build_fake_engine()


@pytest.fixture
def fake_engine_no_controller() -> MagicMock:
    return _build_fake_engine_no_controller()


@pytest.fixture
def monkey_daemon(monkeypatch):
    """Monkeypatch _daemon_get/post/delete 让 daemon-only 工具直接返回占位 data.

    真实实现会调 _extract_daemon_result 剥掉 envelope 返回 data 部分; 我们
    monkeypatch 后直接返回 data (因为 handle 还会再包一层 envelope).
    """
    fake_data = {"stub": True}
    monkeypatch.setattr(MCPServer, "_daemon_get", lambda self, path, params=None: fake_data)
    monkeypatch.setattr(MCPServer, "_daemon_post", lambda self, path, args=None: fake_data)
    monkeypatch.setattr(MCPServer, "_daemon_delete", lambda self, path: fake_data)


# ── 参数化列表 ────────────────────────────────────────────

# 走 controller (走 _ensure_started())
_ENGINE_TOOLS = [
    name for name, args in _TOOL_ARGS.items()
    if name not in _LLM_DRIVEN
    and name not in (
        "sb_graph", "sb_history", "sb_stats",
        "sb_memory_lookup", "sb_memory_stats",
        "sb_sessions_list", "sb_sessions_create", "sb_sessions_delete",
        "sb_capacity", "sb_admin_degrade", "sb_admin_restore",
        "sb_queue", "sb_health",
    )
]

# 走 _ensure_engine() 但不碰 controller
_ENGINE_ONLY_TOOLS = [
    "sb_graph", "sb_history", "sb_stats",
]

# 走 _daemon_* 路径
_DAEMON_TOOLS = [
    "sb_sessions_list", "sb_sessions_create", "sb_sessions_delete",
    "sb_capacity", "sb_admin_degrade", "sb_admin_restore",
    "sb_queue", "sb_health",
]

# 走 GoalMemory (无 controller, 无 engine)
_MEMORY_TOOLS = ["sb_memory_lookup", "sb_memory_stats"]


# ── helpers ───────────────────────────────────────────────

async def _call_tool(srv: MCPServer, name: str, args: dict, req_id: int = 1):
    return await srv.handle({
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })


def _assert_envelope_ok(resp: dict, name: str, req_id: int = 1) -> dict:
    """验证 MCP 协议 envelope + 返回 dict 结构稳定. 返回解析后的 inner data."""
    assert resp["jsonrpc"] == "2.0", f"{name}: bad jsonrpc: {resp}"
    assert resp["id"] == req_id, f"{name}: id mismatch: {resp}"
    assert "result" in resp, f"{name}: no result: {resp}"
    result = resp["result"]
    assert result.get("isError") is False, f"{name}: isError=True: {result}"
    assert isinstance(result.get("content"), list), f"{name}: content not list"
    assert len(result["content"]) >= 1, f"{name}: empty content"
    text = result["content"][0]["text"]
    inner = json.loads(text)
    assert isinstance(inner, dict), f"{name}: inner not dict: {inner!r}"
    return inner


# ── tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_ENGINE_TOOLS))
async def test_engine_tool_roundtrip(name, fake_engine, monkeypatch):
    """所有走 engine/controller 的工具 — 一次 happy-path, 验证 envelope."""
    # sb_snapshot 自己造 SnapshotEngine; monkeypatch 掉它的 __init__ / capture
    from semantic_browser.snapshot.engine import SnapshotEngine, PageSnapshot, TextBlock
    async def fake_capture(self, base_url="", **kwargs):
        return PageSnapshot(
            url=base_url, title="T", domain="x.com",
            text_blocks=[TextBlock(tag="h1", text="H")],
        )
    monkeypatch.setattr(SnapshotEngine, "__init__", lambda self, page: None)
    monkeypatch.setattr(SnapshotEngine, "capture", fake_capture)

    # sb_snapshot_deep 走同一个 SnapshotEngine 但 detail_level=deep
    # monkeypatch 已覆盖, 不需额外处理
    srv = MCPServer(engine=fake_engine)
    resp = await _call_tool(srv, name, _TOOL_ARGS[name])
    inner = _assert_envelope_ok(resp, name)
    # inner 必须是 dict — T48 envelope {ok, data, error} 形态
    # 工具直接返回 dict 时 inner 就是那个 dict
    # sb_storage 返回的 dict (通过 controller.get_storage) 会以 ok/data/error 包一层;
    # 这里 mock 让 get_storage 返回 {}, _call_tool 直接返回 {},
    # 真正的 inner 是 {}
    assert isinstance(inner, dict)
    # 内层若带 ok 字段, 应为 True
    if "ok" in inner:
        assert inner["ok"] is True, f"{name}: ok=False inner={inner}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_ENGINE_ONLY_TOOLS))
async def test_engine_only_tool_roundtrip(name):
    """sb_graph / sb_history / sb_stats — 不碰 controller, 走 engine 上方法."""
    eng = _build_fake_engine_no_controller()
    srv = MCPServer(engine=eng)
    resp = await _call_tool(srv, name, _TOOL_ARGS[name])
    inner = _assert_envelope_ok(resp, name)
    assert isinstance(inner, dict)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_DAEMON_TOOLS))
async def test_daemon_tool_roundtrip(name, monkey_daemon):
    """daemon-only 工具 — _daemon_* 被 monkeypatch 成统一 fake response."""
    srv = MCPServer()
    resp = await _call_tool(srv, name, _TOOL_ARGS[name])
    inner = _assert_envelope_ok(resp, name)
    # handle 会再包一层 envelope {ok, data, error}; data 是 stub
    assert inner["ok"] is True, f"{name}: daemon not ok: {inner}"
    assert inner["data"] == {"stub": True}
    assert inner["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_MEMORY_TOOLS))
async def test_memory_tool_roundtrip(name, tmp_path, monkeypatch):
    """sb_memory_lookup / sb_memory_stats — GoalMemory 用临时 HOME 隔离."""
    monkeypatch.setenv("HOME", str(tmp_path))
    srv = MCPServer()
    resp = await _call_tool(srv, name, _TOOL_ARGS[name])
    inner = _assert_envelope_ok(resp, name)
    assert isinstance(inner, dict)


# ── 全局契约 ──────────────────────────────────────────────


class TestToolsListContract:
    """tools/list 必须包含所有已注册的 tool name."""

    def test_all_registered_names_listed(self):
        srv = MCPServer()
        resp = asyncio_run_handle(srv, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })
        names = {t["name"] for t in resp["result"]["tools"]}
        defined_names = {t["name"] for t in TOOL_DEFINITIONS}
        assert names == defined_names, (
            f"tools/list 与 TOOL_DEFINITIONS 失配: "
            f"missing={defined_names - names}, extra={names - defined_names}"
        )

    def test_each_tool_has_input_schema(self):
        for t in TOOL_DEFINITIONS:
            assert "name" in t, f"tool 缺 name: {t}"
            assert "inputSchema" in t, f"tool {t.get('name')} 缺 inputSchema"
            schema = t["inputSchema"]
            assert schema.get("type") == "object", (
                f"{t['name']}: inputSchema.type 应为 object, got {schema.get('type')}"
            )
            assert "properties" in schema, f"{t['name']}: inputSchema 缺 properties"

    def test_no_duplicate_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names)), (
            f"TOOL_DEFINITIONS 重复: "
            f"{[n for n in names if names.count(n) > 1]}"
        )

    def test_test_coverage_matches_definitions(self):
        """C 测试覆盖的 tool 必须 = TOOL_DEFINITIONS - LLM_DRIVEN."""
        tested = (
            set(_ENGINE_TOOLS) | set(_ENGINE_ONLY_TOOLS)
            | set(_DAEMON_TOOLS) | set(_MEMORY_TOOLS)
        )
        registered = {t["name"] for t in TOOL_DEFINITIONS}
        untested = registered - tested - _LLM_DRIVEN
        assert not untested, (
            f"TOOL_DEFINITIONS 里有工具没被 C 测试覆盖: {sorted(untested)}"
        )


def asyncio_run_handle(srv: MCPServer, request: dict) -> dict:
    """同步包装 — TestToolsListContract 用 sync test."""
    import asyncio
    return asyncio.run(srv.handle(request))