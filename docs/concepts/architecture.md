# 架构

三层结构：核心库（Python API）→ daemon runtime（多 agent 共享）→
协议适配器（MCP / CLI）。

```
┌────────────────────────────────────────────────────┐
│ Agent 客户端                                        │
│  MCP client (Claude Desktop…) / tb CLI / HTTP curl  │
└───────┬──────────────┬──────────────────┬──────────┘
        │ stdio/JSON-RPC│ HTTP             │ Python
┌───────▼──────┐ ┌──────▼──────────┐ ┌────▼─────────┐
│ mcp_server/  │ │ daemon/          │ │ engine.py    │
│ MCP 工具暴露  │ │ HTTP runtime     │ │ SemanticBrowser│
│ (可代理到     │ │ session/lease/   │ │ (进程内直用)  │
│  daemon)     │ │ SSE/计量/熔断/降级 │ │              │
└───────┬──────┘ └──────┬───────────┘ └────┬─────────┘
        └──────────┬─────┴──────────────────┘
                   ▼
        ┌──────────────────────────┐
        │ 核心模块                   │
        │ browser/   Playwright 封装 │
        │ snapshot/  语义快照 (+vision)│
        │ classifier/ article/docs/… │
        │ extractor/ 正文/接口提取     │
        │ query/     SemanticQuery M3│
        │ safety/    SSRF/守卫/Stealth│
        │ llm/       provider 抽象    │
        │ memory/ graph/ crawler/    │
        └──────────────────────────┘
```

## daemon 内部

`daemon/server.py` 只剩 HTTP 骨架 + 生命周期（`TransparentBrowserDaemon`）；
路由处理全部拆到 `daemon/routers/` 表驱动分发：

| 模块 | 职责 |
|------|------|
| `routers/_browser.py` | open/click/type/drag/screenshot…（含安全守卫） |
| `routers/_security.py` | T40–T44 安全审计端点 |
| `routers/_sessions.py` | session CRUD + lease + handoff |
| `routers/_query.py` | /v1/query + stream + stats |
| `routers/_agent.py` / `_discover.py` / `_events.py` | agent run / discover / SSE |
| `routers/_admin.py` | admin/drain/degrade/healthz/readyz/capacity |
| `routers/_integrations.py` | /v1/integrations 适配器目录 |

横切机制：

- **op_lock**：浏览器单实例写操作串行化；只读/长任务走并发通道
- **degradation**（L0–L4）+ **drain**（SIGTERM 优雅排空）
- **circuit_breaker**：per-domain 导航失败熔断
- **metering**：LLM token 用量写 SQLite，`/v1/usage*` 暴露
- **snapshots**：storage_state 周期快照，崩溃后 reattach
- **event_bus**：持久事件流，SSE + Last-Event-ID 续传

## browser/ 模块拆分

`BrowserController` = 5 个 mixin 组合（Round 1b 重构）：

| 模块 | 职责 |
|------|------|
| `navigation.py` | open/back/forward/reload/tabs/context |
| `interact.py` | click/type/scroll/drag/heal + `get_ref_label` |
| `debug.py` | console/network/errors/websocket 缓冲 |
| `headers.py` | CSP/HSTS/permissions-policy 解析 |
| `security_tools.py` | T40–T44 审计工具 |
| `_utils.py` | redact/CORS/TLS/hints 共用 helper |

历史设计决策见 [../design-log.md](../design-log.md)。
