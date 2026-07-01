# Transparent Browser Architecture

目标：构建一个通用 Agent 浏览器底座，让 Claude Code、Codex、OpenClaw、Cursor、任意 MCP client 都能连接同一个真实浏览器实例，读取网页、理解网页、操作网页。

## 核心定位

Transparent Browser 不是“又一个 MCP server”，也不是“某个 agent 的工具插件”。

它是一个本地长驻浏览器内核服务：

```text
Claude Code / Codex / OpenClaw / Cursor / Custom Agent
        │
        ├── CLI adapter: tb snapshot / tb click e3 / tb read
        ├── MCP adapter: tools/call -> daemon HTTP API
        └── SDK adapter: Python/JS client
                │
        Transparent Browser Daemon
                │
        Browser Core: Chromium/Chrome/Brave/Edge via CDP/Playwright
                │
        Injected Transparent Runtime
                │
        Real Websites
```

## 设计原则

1. **一个真实浏览器，多种 agent adapter**
   - CLI、MCP、OpenClaw tool 都不直接启动浏览器。
   - 它们只连接本地 daemon。

2. **CLI first, MCP second**
   - Coding agents 适合 CLI：短输出、低 token、易调试。
   - MCP 作为兼容层，不作为核心架构。

3. **浏览器状态持久**
   - 跨命令共享 tab/session/cookies/localStorage。
   - 支持 session/profile 隔离。

4. **Agent-readable, human-visible**
   - Agent 获取 semantic snapshot。
   - 人类可看 overlay/ref 标注，知道 agent 看到了什么。

5. **Browser-agnostic**
   - 第一阶段使用 Playwright Chromium。
   - 后续支持 attach existing Chrome/Brave/Edge via CDP。

## Core Daemon

Daemon 负责：

- 启动/连接浏览器
- 管理 profile/session/tab
- 维护当前页面状态
- 执行 open/click/type/scroll/back/forward/screenshot
- 生成 semantic snapshot
- 注入 transparent runtime
- 保存 memory/site graph

本地 API：

```text
GET  /health
GET  /state
POST /open          {url, session?}
GET  /snapshot      {session?, tab?}
GET  /read          {format?}
POST /click         {ref}
POST /type          {ref, text}
POST /scroll        {direction, amount}
POST /press         {key}
POST /back
POST /forward
POST /screenshot    {path?}
POST /state/save    {path?}
POST /state/load    {path}
GET  /graph         {url?}
GET  /history       {domain?}
```

## CLI Adapter

目标命令：

```bash
tb daemon start
tb daemon status
tb open https://example.com
tb snapshot --json
tb read --markdown
tb click e12
tb type e7 "hello"
tb scroll down 800
tb screenshot page.png
tb state save auth.json
tb graph
```

Claude Code / Codex 用 CLI 最自然：

```bash
tb open https://docs.python.org/3/
tb snapshot --compact
tb click e12
tb read --markdown
```

## MCP Adapter

MCP server 不持有浏览器，只转发给 daemon：

```text
MCP tools/call -> HTTP localhost daemon -> browser core
```

好处：

- Claude Code MCP、Cursor MCP、OpenClaw MCP 使用同一个 browser state。
- MCP server 可以随开随关，浏览器不丢。

## Transparent Runtime

注入到页面的 JS runtime：

```js
window.__TB = {
  snapshot(),
  read(),
  controls(),
  links(),
  forms(),
  annotate(),
  clearOverlay(),
  target(ref),
  detectBlockers()
}
```

Runtime 负责：

- 稳定 ref 分配
- DOM 区域识别：main/nav/sidebar/footer/modal
- 可操作元素识别
- 遮挡检测
- overlay 标注
- SPA mutation 后 ref 更新

## 与当前 semantic-browser 的关系

当前 Python 包继续保留，但职责调整：

- `browser/controller.py` -> Browser Core 原型
- `snapshot/engine.py` -> Semantic Snapshot engine
- `extractor/content.py` -> Readability/content extraction
- `memory/store.py` -> Memory backend
- `graph/builder.py` -> Site graph
- `mcp_server/server.py` -> Adapter，后续改为 daemon client
- 新增 `daemon/` -> 长驻核心服务
- 新增 `client/` -> HTTP client
- 新增 `transparent_runtime/` -> injected JS

## 阶段计划

### Stage 1: Local Daemon MVP

- HTTP daemon
- 单 browser / 单 page
- open/snapshot/click/type/scroll/read/state
- CLI client 连接 daemon
- 验证跨命令共享页面状态

### Stage 2: Runtime Injection

- `window.__TB`
- overlay/ref 标注
- blocker detection
- SPA mutation support

### Stage 3: Multi-session/Profile

- session id
- profile path
- attach CDP
- Chrome/Brave/Edge selection

### Stage 4: MCP/OpenClaw Adapters

- MCP adapter 转发 daemon
- OpenClaw plugin/skill
- Claude Code config examples

### Stage 5: Site Intelligence

- automatic site map
- docs/blog/search/login classifiers
- memory-backed incremental crawl
- agent-readable site manual
