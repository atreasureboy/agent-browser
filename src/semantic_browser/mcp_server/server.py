"""
MCP Server — SemanticBrowser 的 Model Context Protocol 适配层。

stdio JSON-RPC 2.0 server with lazy SemanticBrowser startup.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any, Optional

from semantic_browser.engine import SemanticBrowser
from semantic_browser.snapshot.engine import SnapshotEngine

logger = logging.getLogger(__name__)

SERVER_INFO = {"name": "semantic-browser", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {"name": "sb_browse", "description": "打开 URL 并返回完整语义浏览结果。", "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "sb_snapshot", "description": "打开 URL 并返回语义快照。", "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "sb_click", "description": "通过 eN ref 点击元素。", "inputSchema": _schema({"ref": {"type": "string"}}, ["ref"])},
    {"name": "sb_type", "description": "通过 eN ref 输入文本。", "inputSchema": _schema({"ref": {"type": "string"}, "text": {"type": "string"}}, ["ref", "text"])},
    {"name": "sb_scroll", "description": "滚动页面。", "inputSchema": _schema({"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"}})},
    {"name": "sb_back", "description": "浏览器后退。", "inputSchema": _schema({})},
    {"name": "sb_forward", "description": "浏览器前进。", "inputSchema": _schema({})},
    {"name": "sb_screenshot", "description": "截图；可传 path 保存，否则返回 base64。", "inputSchema": _schema({"path": {"type": "string"}})},
    {"name": "sb_press_key", "description": "发送键盘按键。", "inputSchema": _schema({"key": {"type": "string"}}, ["key"])},
    {"name": "sb_graph", "description": "从记忆库构建站点拓扑图。", "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "sb_history", "description": "返回访问历史；可按 domain 过滤。", "inputSchema": _schema({"domain": {"type": "string"}})},
    {"name": "sb_stats", "description": "返回记忆库统计。", "inputSchema": _schema({})},
    # T37: 高级 agent 工具 — 让 MCP 客户端 (Claude Desktop 等) 直接用 agent 能力
    {"name": "sb_agent_run", "description": "LLM-driven autonomous loop: 给个 goal, agent 自主完成.",
     "inputSchema": _schema({"goal": {"type": "string"},
                             "start_url": {"type": "string"},
                             "tier": {"type": "string", "enum": ["cheap", "medium", "smart"]},
                             "max_steps": {"type": "integer"}}, ["goal"])},
    {"name": "sb_agent_plan", "description": "Dry-run: LLM 先出 plan, 不执行. 用户决定后再调 sb_agent_run.",
     "inputSchema": _schema({"goal": {"type": "string"},
                             "start_url": {"type": "string"},
                             "tier": {"type": "string", "enum": ["cheap", "medium", "smart"]}}, ["goal"])},
    {"name": "sb_memory_lookup", "description": "查 goal memory 是否有缓存 (避免重复跑).",
     "inputSchema": _schema({"goal": {"type": "string"}}, ["goal"])},
    {"name": "sb_memory_stats", "description": "Goal memory 统计 (cache 大小/命中率).",
     "inputSchema": _schema({})},
    {"name": "sb_discover", "description": "Live 站点图自动发现 (BFS 爬站点生成导航).",
     "inputSchema": _schema({"start_url": {"type": "string"},
                             "max_pages": {"type": "integer"},
                             "max_depth": {"type": "integer"}}, ["start_url"])},
    {"name": "sb_safety_check", "description": "检查 action 是否危险 (delete/remove 等关键词).",
     "inputSchema": _schema({"action": {"type": "string", "enum": ["open", "click", "type", "drag"]},
                             "text": {"type": "string"},
                             "ref_label": {"type": "string"}}, ["action"])},
    # T39: 信息密度工具 — 默认 normal, deep 模式拿更多细节
    {"name": "sb_snapshot_deep", "description": "Deep snapshot: 表单 metadata + 所有 JS src + outerHTML + 完整 HTML attrs.",
     "inputSchema": _schema({})},
    {"name": "sb_get_response_headers", "description": "按 URL 拿最近一次响应的 HTTP headers (CSP/HSTS/Set-Cookie 等).",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "sb_get_dom_diff", "description": "DOM diff: 报告当前页面 ref 与 before_refs 比的 appeared/disappeared.",
     "inputSchema": _schema({"before_refs": {"type": "string",
                                "description": "逗号分隔的 ref 集合 (之前 snapshot 看到的)"}})},
    {"name": "sb_get_script_source", "description": "Deep 模式: 按 URL 抓 JS 源码 (httpx 绕过 CORS, 50K 上限).",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    # T38: 视觉快照 fallback — DOM snapshot 不可用 (canvas/SPA/shadow DOM) 时用
    {"name": "sb_snapshot_vision", "description": "截图 + vision LLM 描述页面 (Canvas/SPA fallback).",
     "inputSchema": _schema({"goal": {"type": "string"},
                             "provider": {"type": "string", "enum": ["anthropic", "gemini"]},
                             "model": {"type": "string"},
                             "full_page": {"type": "boolean"}})},
    # T40a+f: 客户端存储探针 + 安全头结构化
    {"name": "sb_storage", "description": "T40a: localStorage/sessionStorage 全文 + cookies 字段 (HttpOnly/Secure/SameSite).",
     "inputSchema": _schema({})},
    {"name": "sb_security_headers", "description": "T40f: 按 URL 解析 CSP/HSTS/XFO/Referrer-Policy/COOP/COEP/Set-Cookie 标志.",
     "inputSchema": _schema({"url": {"type": "string"}}, ["url"])},
    {"name": "sb_probe_paths", "description": "T40b: 探测常见隐藏路径 (robots.txt / sitemap.xml / .well-known/* / admin / api).",
     "inputSchema": _schema({"url": {"type": "string"},
                             "categories": {"type": "string",
                                            "description": "逗号分隔; well_known/discovery/admin; 空=全部"}})},
    {"name": "sb_list_frames", "description": "T40e: 列出页面所有 frame (顶层 + iframe) 含 depth/cross-origin/child 结构.",
     "inputSchema": _schema({})},
    {"name": "sb_switch_frame", "description": "T40e: 切换活跃 frame (按 name substring 或 url substring).",
     "inputSchema": _schema({"name_or_url": {"type": "string"}}, ["name_or_url"])},
    {"name": "sb_extract_api_endpoints", "description": "T40g: 从页面 JS 中提取 API endpoints (fetch/axios/XHR 模式).",
     "inputSchema": _schema({})},
    {"name": "sb_extract_js_libraries", "description": "T42b: 识别页面 JS 库 (jQuery/React/Vue/...) + 版本 + 已知 CVE.",
     "inputSchema": _schema({})},
    {"name": "sb_detect_graphql", "description": "T42g: 给定 endpoint URL, 跑 GraphQL introspection query dump schema.",
     "inputSchema": _schema({"endpoint": {"type": "string"}}, ["endpoint"])},
    {"name": "sb_get_websockets", "description": "T40i: 返回当前累积的 WebSocket 连接列表 (wss:// URLs).",
     "inputSchema": _schema({"limit": {"type": "integer"}})},
    # T43
    {"name": "sb_enumerate_subdomains", "description": "T43a: 子域名枚举 (crt.sh + TLS cert SAN).",
     "inputSchema": _schema({"host": {"type": "string"}, "include_tls_san": {"type": "boolean"}}, ["host"])},
    {"name": "sb_extract_secrets_from_js", "description": "T43b: 扫当前页所有 <script src> 找硬编码 secret (AWS/GitHub/Bearer/api_key/私钥).",
     "inputSchema": _schema({})},
    {"name": "sb_detect_waf", "description": "T43c: WAF 指纹 (Cloudflare/Akamai/Imperva/AWS WAF/Fastly/Vercel/Netlify).",
     "inputSchema": _schema({})},
    {"name": "sb_find_open_redirect_sinks", "description": "T43d: 扫链接/form action 找开放重定向/SSRF sink (returnUrl, redirect, next, ...).",
     "inputSchema": _schema({})},
    {"name": "sb_find_disclosure", "description": "T43e: 扫页面找敏感泄露 (email/内网IP/AWS key/GitHub token/私钥/调试堆栈).",
     "inputSchema": _schema({})},
    {"name": "sb_analyze_exposed_files", "description": "T43f: 探常见备份/源码/配置文件 (.git/HEAD, .env, phpinfo, .DS_Store...).",
     "inputSchema": _schema({"base_url": {"type": "string"}})},
    {"name": "sb_discover_api_specs", "description": "T43g: 探 OpenAPI / Swagger 端点 (swagger.json, openapi.json, v3/api-docs).",
     "inputSchema": _schema({"base_url": {"type": "string"}})},
    {"name": "sb_tls_subdomains", "description": "T43h: TLS 证书解析 — issuer / 有效期 / SAN → 子域.",
     "inputSchema": _schema({"host": {"type": "string"}, "port": {"type": "integer"}}, ["host"])},
    {"name": "sb_fingerprint_tech", "description": "T43i: 技术栈指纹 (Server / X-Powered-By / meta generator / 框架 cookie).",
     "inputSchema": _schema({})},
    {"name": "sb_decode_jwts", "description": "T43j: 在 storage/cookie/页面里找 JWT, 解码 header + payload (不验签).",
     "inputSchema": _schema({})},
]


class MCPServer:
    def __init__(self, engine: Optional[SemanticBrowser] = None) -> None:
        self._engine = engine

    async def _ensure_started(self) -> SemanticBrowser:
        if self._engine is None:
            self._engine = SemanticBrowser()
        await self._engine.start()
        return self._engine

    def _ensure_engine(self) -> SemanticBrowser:
        if self._engine is None:
            self._engine = SemanticBrowser()
        return self._engine

    async def handle(self, request: Any) -> Optional[dict[str, Any]]:
        if not isinstance(request, dict):
            return self._error(None, INVALID_REQUEST, "Request must be an object")
        req_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return self._error(req_id, INVALID_REQUEST, 'jsonrpc must be "2.0"')
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(method, str):
            return self._error(req_id, INVALID_REQUEST, "method must be a string")
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._ok(req_id, {"protocolVersion": PROTOCOL_VERSION, "serverInfo": SERVER_INFO, "capabilities": {"tools": {}}})
        if method == "ping":
            return self._ok(req_id, {})
        if method == "tools/list":
            return self._ok(req_id, {"tools": TOOL_DEFINITIONS})
        if method == "tools/call":
            return await self._handle_tool_call(req_id, params)
        return self._error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    async def _handle_tool_call(self, req_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(req_id, INVALID_PARAMS, "params must be an object")
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return self._error(req_id, INVALID_PARAMS, "params.name must be a string")
        if not isinstance(args, dict):
            return self._error(req_id, INVALID_PARAMS, "params.arguments must be an object")
        try:
            result = await self._call_tool(name, args)
        except KeyError as e:
            return self._error(req_id, INVALID_PARAMS, f"Missing required argument: {e.args[0]}")
        except ValueError as e:
            return self._error(req_id, INVALID_PARAMS, str(e))
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return self._error(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
        return self._ok(req_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}], "isError": False})

    async def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "sb_browse":
            engine = await self._ensure_started()
            return (await engine.browse(args["url"])).to_dict()
        if name == "sb_snapshot":
            engine = await self._ensure_started()
            await engine.controller.open(args["url"])
            page = engine.controller.current_page
            if page is None:
                raise RuntimeError("No active page")
            return (await SnapshotEngine(page).capture(base_url=args["url"])).to_dict()
        if name == "sb_click":
            engine = await self._ensure_started()
            ok = await engine.controller.click(args["ref"])
            return {"ref": args["ref"], "success": ok, "url": await engine.controller.get_url()}
        if name == "sb_type":
            engine = await self._ensure_started()
            ok = await engine.controller.type_text(args["ref"], args["text"])
            return {"ref": args["ref"], "success": ok, "text_length": len(args["text"])}
        if name == "sb_scroll":
            engine = await self._ensure_started()
            direction = args.get("direction", "down")
            amount = int(args.get("amount", 500))
            await engine.controller.scroll(direction, amount)
            return {"direction": direction, "amount": amount}
        if name == "sb_back":
            engine = await self._ensure_started()
            await engine.controller.back()
            return {"url": await engine.controller.get_url()}
        if name == "sb_forward":
            engine = await self._ensure_started()
            await engine.controller.forward()
            return {"url": await engine.controller.get_url()}
        if name == "sb_screenshot":
            engine = await self._ensure_started()
            path = args.get("path")
            data = await engine.controller.screenshot(path=path)
            return {"path": path, "bytes": len(data), "base64": None if path else base64.b64encode(data).decode("ascii")}
        if name == "sb_press_key":
            engine = await self._ensure_started()
            await engine.controller.press_key(args["key"])
            return {"key": args["key"]}
        if name == "sb_graph":
            return self._ensure_engine().get_site_graph(args["url"]).to_dict()
        if name == "sb_history":
            domain = args.get("domain", "")
            pages = self._ensure_engine().get_visited_pages(domain)
            return {"pages": pages, "count": len(pages)}
        if name == "sb_stats":
            return self._ensure_engine().get_memory_stats()
        # T37: 高级 agent 工具
        if name == "sb_agent_run":
            from semantic_browser.agent import GoalAgent
            engine = await self._ensure_started()
            agent = GoalAgent(
                engine.controller,
                tier=args.get("tier", "smart"),
                max_steps=int(args.get("max_steps", 20)),
            )
            result = await agent.run(
                goal=args["goal"],
                start_url=args.get("start_url") or None,
            )
            return result.to_dict()
        if name == "sb_agent_plan":
            from semantic_browser.agent import GoalAgent
            engine = await self._ensure_started()
            agent = GoalAgent(
                engine.controller,
                tier=args.get("tier", "smart"),
            )
            return await agent.plan(goal=args["goal"])
        if name == "sb_memory_lookup":
            from semantic_browser.memory.goal_memory import GoalMemory
            mem = GoalMemory()
            hit = mem.lookup(args["goal"])
            return {"hit": hit is not None, "entry": hit}
        if name == "sb_memory_stats":
            from semantic_browser.memory.goal_memory import GoalMemory
            return GoalMemory().stats()
        if name == "sb_discover":
            from semantic_browser.llm import discover, format_sitemap_for_llm
            engine = await self._ensure_started()
            result = await discover(
                engine.controller,
                start_url=args["start_url"],
                max_pages=int(args.get("max_pages", 15)),
                max_depth=int(args.get("max_depth", 2)),
                delay_ms=0,  # MCP 客户端一般不同步等, 0 延迟
            )
            return {
                "pages_visited": result.pages_visited,
                "tree_text": result.tree_text,
                "llm_summary": format_sitemap_for_llm(result),
            }
        if name == "sb_safety_check":
            from semantic_browser.safety import check_action
            check = check_action(
                args["action"],
                {"ref": args.get("ref", ""), "text": args.get("text", "")},
                ref_label=args.get("ref_label"),
            )
            return {
                "needs_confirm": check.needs_confirm,
                "reason": check.reason,
                "risk_level": check.risk_level,
            }
        # T39: 深度信息工具 — 让 agent 按需拿更深细节
        if name == "sb_snapshot_deep":
            engine = await self._ensure_started()
            page = engine.controller.current_page
            if page is None:
                raise RuntimeError("No active page")
            snap = await SnapshotEngine(page).capture(
                base_url=page.url, detail_level="deep",
            )
            return snap.to_dict()
        if name == "sb_get_response_headers":
            engine = await self._ensure_started()
            return await engine.controller.get_response_headers(args["url"])
        if name == "sb_get_dom_diff":
            engine = await self._ensure_started()
            refs_param = args.get("before_refs", "")
            before_refs = set(refs_param.split(",")) if refs_param else set()
            return await engine.controller.get_dom_diff(before_refs)
        if name == "sb_get_script_source":
            engine = await self._ensure_started()
            return {"source": await engine.controller.fetch_script_source(args["url"])}
        if name == "sb_snapshot_vision":
            engine = await self._ensure_started()
            from semantic_browser.snapshot.vision import capture_vision_snapshot
            vsnap = await capture_vision_snapshot(
                engine.controller,
                goal=args.get("goal", ""),
                provider=args.get("provider"),
                model=args.get("model"),
                full_page=bool(args.get("full_page", True)),
            )
            return vsnap.to_dict()
        if name == "sb_storage":
            engine = await self._ensure_started()
            return await engine.controller.get_storage()
        if name == "sb_security_headers":
            engine = await self._ensure_started()
            return await engine.controller.get_security_headers(args["url"])
        if name == "sb_probe_paths":
            engine = await self._ensure_started()
            cats_raw = args.get("categories", "")
            categories = [c for c in cats_raw.split(",") if c] if cats_raw else None
            return await engine.controller.probe_paths(args["url"], categories=categories)
        if name == "sb_list_frames":
            engine = await self._ensure_started()
            return await engine.controller.list_frames()
        if name == "sb_switch_frame":
            engine = await self._ensure_started()
            return await engine.controller.switch_frame(args["name_or_url"])
        if name == "sb_extract_api_endpoints":
            engine = await self._ensure_started()
            return await engine.controller.extract_api_endpoints()
        if name == "sb_extract_js_libraries":
            engine = await self._ensure_started()
            return await engine.controller.extract_js_libraries()
        if name == "sb_detect_graphql":
            engine = await self._ensure_started()
            return await engine.controller.detect_graphql(args["endpoint"])
        if name == "sb_get_websockets":
            engine = await self._ensure_started()
            return engine.controller.get_websockets(limit=int(args.get("limit", 100)))
        # T43
        if name == "sb_enumerate_subdomains":
            engine = await self._ensure_started()
            return await engine.controller.enumerate_subdomains(
                host=args["host"],
                include_tls_san=bool(args.get("include_tls_san", True)),
            )
        if name == "sb_extract_secrets_from_js":
            engine = await self._ensure_started()
            return await engine.controller.extract_secrets_from_js()
        if name == "sb_detect_waf":
            engine = await self._ensure_started()
            return await engine.controller.detect_waf()
        if name == "sb_find_open_redirect_sinks":
            engine = await self._ensure_started()
            return await engine.controller.find_open_redirect_sinks()
        if name == "sb_find_disclosure":
            engine = await self._ensure_started()
            return await engine.controller.find_disclosure()
        if name == "sb_analyze_exposed_files":
            engine = await self._ensure_started()
            return await engine.controller.analyze_exposed_files(
                base_url=args.get("base_url") or None,
            )
        if name == "sb_discover_api_specs":
            engine = await self._ensure_started()
            return await engine.controller.discover_api_specs(
                base_url=args.get("base_url") or None,
            )
        if name == "sb_tls_subdomains":
            engine = await self._ensure_started()
            return await engine.controller.tls_subdomains(
                host=args["host"], port=int(args.get("port", 443)),
            )
        if name == "sb_fingerprint_tech":
            engine = await self._ensure_started()
            return await engine.controller.fingerprint_tech()
        if name == "sb_decode_jwts":
            engine = await self._ensure_started()
            return await engine.controller.decode_jwts()
        raise ValueError(f"Unknown tool: {name}")

    async def run(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        try:
            while True:
                line = await asyncio.to_thread(stdin.readline)
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as e:
                    response = self._error(None, PARSE_ERROR, f"Parse error: {e}")
                else:
                    response = await self.handle(request)
                if response is not None:
                    stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    stdout.flush()
        finally:
            if self._engine is not None:
                await self._engine.close()

    @staticmethod
    def _ok(req_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def amain() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await MCPServer().run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
