# Semantic Browser

> Agent-readable semantic browser layer — 给 AI Agent 用的透明浏览器

**文档**: [docs/](docs/index.md) — 安装 · 快速开始 · API 参考 · 架构与概念 · 生产部署

**不是又一个浏览器工具，而是 Chromium 之上的 Site Intelligence Layer。**

## 核心理念：模型驱动的浏览器语义层

**为"顶级 agent ↔ 浏览器"做 token 经济层**。传统 agent 读网页要烧 50KB+ token 解析 DOM；SemanticQuery 让用户配置的轻量 LLM（如 DeepSeek / Qwen / Ollama / Llama 等）在中间层完成浏览 + 抽取 + 精炼，顶级 agent 只看到 ~500 tokens 精炼 markdown。

```
顶级 agent (Claude Opus / GPT-4o)
   ↓ query("find X about Y") + budget=2000      (~50 tokens)
SemanticQuery — 性价比轻量 LLM 编排 (DeepSeek / Qwen / Ollama 等)
   plan → browse → relevance filter → synthesize → markdown 答案
   ↓                                              (~500-1500 tokens)
顶级 agent 消费, 做最终决策
```

## ⚡ 1 分钟快速开始 (Quick Start)

### 1. 安装 (Installation)

```bash
# 方式 A: 直接从 PyPI 安装发布包 (推荐)
pip install agent-site-intelligence

# 方式 B: 从源码本地克隆开发安装
git clone https://github.com/atreasureboy/agent-browser.git
cd agent-browser
pip install -e .

# 安装 Playwright 浏览器内核与系统依赖
playwright install chromium
playwright install-deps  # Linux 系统推荐
```

### 2. 配置环境变量 (Configuration)

配置您偏好的 LLM Provider（支持任意 OpenAI 兼容接口、DeepSeek、Ollama 本地私有化模型、Claude 等）：

```bash
# 示例: 使用 DeepSeek (推荐，性价比极高)
export LLM_PROVIDER=openai
export OPENAI_API_KEY="your-api-key-here"
export OPENAI_BASE_URL="https://api.deepseek.com/v1"
export OPENAI_MODEL="deepseek-chat"

# 示例: 本地私有化 Ollama (0 Token 成本运行)
# export LLM_PROVIDER=openai
# export OPENAI_BASE_URL="http://localhost:11434/v1"
# export OPENAI_MODEL="qwen2.5-coder"
```

### 3. 运行你的第一个语义查询 (Hello World)

#### 🐍 方式 A: Python SDK 进程内调用
```python
import asyncio
from semantic_browser.query import run_query

async def main():
    # 自动导航、抽取并精炼网页，返回 ~500 Tokens 的高质量 Markdown 答案与引用
    result = await run_query(
        "找到 Python 3.13 最主要的 3 个新特性",
        start_url="https://docs.python.org/3/whatsnew/3.13.html"
    )
    print(result.to_markdown())

asyncio.run(main())
```

#### 💻 方式 B: CLI 命令行单发查询
```bash
sb query "Python 3.13 top 3 new features" \
    --start-url https://docs.python.org/3/whatsnew/3.13.html
```

#### 🚀 方式 C: 启动 Daemon 守护进程 (支持多 Agent 共享与 60+ MCP 工具)
```bash
# 1. 启动守护进程服务 (默认端口 8765)
tb-daemon

# 2. 发起查询或 SSE 流式监听
curl -X POST localhost:8765/v1/query \
  -d '{"query":"Python 3.13 top 3 features", "start_url":"https://docs.python.org/3/whatsnew/3.13.html"}'
```

---

### 四种顶层 API 概览

**Python** (进程内):
```python
from semantic_browser.query import run_query
result = await run_query(
    "find GitHub PEP 703 discussions, give 3 perspectives",
    start_url="https://github.com/python/peps", budget=2000,
)
print(result.to_markdown())  # ~600 chars markdown + citations
```

**CLI**:
```bash
sb query "Python 3.13 top 3 new features" \
    --start-url https://docs.python.org/3/whatsnew/3.13.html
```

**daemon HTTP**:
```bash
# 阻塞
curl -X POST localhost:8765/v1/query \
  -d '{"query":"...", "start_url":"...", "budget":2000}'
# SSE 流式 (实时 phase 推送)
curl -N -X POST localhost:8765/v1/query/stream \
  -d '{"query":"...", "start_url":"..."}'
```

**MCP 工具** (67 个, 新增 2 个用于监控):
- `sb_query({query, start_url, budget, max_pages})` — 主查询
- `sb_query_stats()` — cache 命中率 + LLM 服务状态
- `sb_query_clear_cache()` — 清空内存 cache (运维)

**CLI 一句话指南 (T88)**:
- `tb query "..."` — 一次返精炼 markdown (token 经济, 大多数场景) **← 首选**
- `tb agent "..."` — step-by-step 自主循环 (复杂多步任务)
- `sb query "..."` — `tb query` 同语义, 但本地起 Chromium (无 daemon)

完整文档与配置见 [T67+T68 README 节](#t67t68-模型驱动的浏览器语义层semanticquery).

**监控** (daemon 多 agent 共享):
```bash
curl localhost:8765/v1/query/stats
# → {llm: {provider, models, call_counts}, cache: {hits, misses, calls, size, hit_rate},
#    concurrency: {limit, available}}
```

daemon 现在 daemon-wide 共享 SemanticQuery 实例 + 持久 cache (~/.semantic-browser/query_cache.json), 同 query+URL 跨请求 + 跨重启都命中, 多 agent 共享场景下 token 经济最大化。

**生产部署**: 完整 K8s yaml + 监控告警 + 错误码 + cache 策略见 [examples/production_deploy.md](examples/production_deploy.md).

## 这是给谁用的？三个具体场景

**1. 你在写一个 agent，agent 需要"看懂"网页**
普通浏览器给 agent 的是像素 + DOM 字符串。Semantic Browser 给的是结构化 snapshot（页面类型 / 文本块 / 链接 ref / 表单字段 / 控件 / meta），agent 直接消费不用解析。

```bash
tb open https://blog.python.org/
tb snapshot --json-out | jq '.text_blocks, .links'
```

**2. 你在做 web scraping，被 JS-heavy 站点卡住**
Playwright 能跑 JS，但拿到的 HTML 是噪音。snapshot 给你的是 article / docs / search / login / list / dashboard / error 分类后的语义结构，外加 heal-click (自动重试点错时换 selector)。

```bash
tb open https://spa-heavy-site.example/
tb snapshot --json-out | jq '.page_type, .forms'
tb heal-click e5   # e5 失效时自动找最相近的可点元素
```

**3. 你在做 security recon / 站点巡检**
39 项 site intelligence 工具 (T40–T44)：子域名枚举、DNS / SPF / DMARC、TLS cert SAN、JS secret 扫描、WAF 指纹、开放重定向 sink、DOM XSS sink、IDOR-prone URL、云资源泄露、CSP 深度解析、2FA / OAuth 检测、子域接管信号...

```bash
tb dns-records github.com              # SPF ~all? DMARC p=none?
tb enumerate-subdomains github.com     # crt.sh + TLS SAN
tb extract-secrets-from-js             # AWS key / GitHub token / Bearer
tb find-xss-sinks                      # eval / innerHTML / document.cookie
tb check-subdomain-takeover example.com
```

→ 完整工具列表见 [T40–T44 章节](#安全审计增强套件-t40--t42)。

## 核心理念

普通浏览器是给人看的（像素画面 + 鼠标点击）。Semantic Browser 是给 Agent 看的：

- 页面正文是什么
- 页面有哪些区域
- 有哪些链接和按钮
- 页面状态是什么
- 网站结构是什么
- 下一步能做什么

## 架构

```
┌─────────────────────────────────────┐
│         Agent (任何 Agent)            │
└──────────────┬──────────────────────┘
               │ Python API / CLI
┌──────────────┴──────────────────────┐
│      Semantic Browser Engine         │
│  ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │Snapshot  │ │Classifier│ │Memory│ │
│  │Engine    │ │Heuristic │ │Store │ │
│  └──────────┘ └──────────┘ └──────┘ │
│  ┌────────────────┐ ┌────────────┐  │
│  │Content         │ │Website     │  │
│  │Extractor       │ │Graph       │  │
│  └────────────────┘ └────────────┘  │
└──────────────┬──────────────────────┘
               │ Playwright
┌──────────────┴──────────────────────┐
│            Chromium                  │
└─────────────────────────────────────┘
```

## 模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **Browser Controller** | `browser/` (controller + 6 mixin 模块) | Playwright 封装：open/click/type/scroll/screenshot + 安全审计工具 |
| **Snapshot Engine** | `snapshot/engine.py` | 语义快照：文本块、链接、控件、meta 信息 |
| **Page Classifier** | `classifier/heuristic.py` | 启发式分类：article/docs/search/login/list/error/dashboard/video |
| **Content Extractor** | `extractor/content.py` | 正文提取（标题/作者/日期/段落/代码块）+ 接口提取 |
| **Memory Store** | `memory/store.py` | SQLite 持久化：页面/链接/操作/会话/笔记 |
| **Website Graph** | `graph/builder.py` | 站点拓扑图：页面关系树 |
| **Engine** | `engine.py` | 核心编排：串联所有模块 |
| **CLI** | `cli/main.py` | 命令行入口 |
| **Daemon** | `daemon/server.py` + `daemon/routers/` | 多 agent HTTP runtime：session/lease/SSE/计量/熔断 |
| **MCP Server** | `mcp_server/server.py` | MCP 协议暴露 75+ 工具 (stdio/daemon 代理) |
| **SemanticQuery** | `query/semantic_query.py` | M3 编排：plan → browse → relevance → synthesize |
| **Safety** | `safety/` | SSRF 闸 / 危险动作守卫 / Stealth 反检测 |
| **LLM** | `llm/` | Provider 抽象 (OpenAI/Anthropic/Gemini/Ollama) + 三档 tier |

## 安装

```bash
cd /project/semantic-browser
source .venv/bin/activate
pip install -e .
```

## 环境变量 (LLM 增强分类)

`OPENAI_API_KEY` 是唯一必需的（如果用启发式分类就完全不需要环境变量）。其它两个变量都有默认值。

```bash
# 默认 (官方 OpenAI endpoint)
export OPENAI_API_KEY=sk-...

# DeepSeek (推荐, 便宜)
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 注意: 官方名是 OPENAI_BASE_URL
export OPENAI_MODEL=deepseek-chat

# 兼容旧名: OPENAI_API_BASE 也被识别 (fallback)
```

注：环境变量名用 `OPENAI_BASE_URL`（OpenAI 官方命名），`OPENAI_API_BASE` 作为 fallback 向后兼容。

## 使用

### CLI

```bash
# 浏览一个页面 — 输出完整语义快照
sb browse "https://blog.python.org/"

# 只看快照 JSON
sb snapshot "https://example.com"

# 提取文章内容
sb article "https://blog.python.org/" --markdown

# 在文章中查找关键词 (返回按 score 排序的 section 列表)
sb find "https://docs.python.org/3/whatsnew/3.13.html" "JIT" --json-out

# 抽取主题摘要 (围绕关键词的紧凑 markdown)
sb extract-topic "https://docs.python.org/3/whatsnew/3.13.html" "PEP 703" --markdown

# 查看站点拓扑图
sb graph "https://blog.python.org/"

# 查看访问历史
sb history
sb history python.org

# 查看记忆统计
sb stats

# 自动爬取站内页面
sb crawl "https://docs.python.org/3/" --max-pages 10

# 交互式 REPL (打开页面后输 click e5 / type e3 hello / snapshot)
sb interactive "https://example.com"

# 截图保存为 PNG
sb screenshot "https://example.com" --out shot.png

# 在 REPL 里给当前页加笔记 (持久化到 ~/.semantic-browser/memory.db)
# 然后从外部读取:
sb notes                              # 所有最近笔记
sb notes "https://example.com/page"   # 指定 URL 的笔记
```

### 持久浏览器守护进程 (`tb-daemon`)

默认 `sb <cmd>` 每次冷启浏览器 (~2s)。频繁调用时推荐用 daemon:

```bash
# 后台启动 daemon (端口 8765)
tb daemon start --background --port 8765

# 通过 tb CLI 调用 (复用同一浏览器)
tb open https://example.com
tb snapshot --json-out
tb read --format markdown
tb click e3
tb type e5 "hello"
tb history
tb graph

# daemon 状态 (默认 8765, 也可用 --port 或 --base 切到别的实例)
tb daemon status
tb --base http://127.0.0.1:18765 daemon status

# 关掉
tb daemon stop
```

### JSON 输出格式 (`--json-out`)

`browse / snapshot / find / extract-topic` 都支持 `--json-out`, 输出 valid JSON 到 stdout。
agent 可直接 `python -c 'import json,sys; d = json.load(sys.stdin); ...'` 消费, 不被 ANSI / rich 颜色污染。

### Python API

```python
import asyncio
from semantic_browser.engine import SemanticBrowser

async def main():
    sb = SemanticBrowser()
    await sb.start()

    # 浏览页面 — full=True 拿到全文 (text_blocks/links/sections)
    result = await sb.browse("https://blog.python.org/")
    full = result.to_dict(full=True)
    print(full["article"]["summary"][:300])  # 顶部 1500 字符摘要

    # 找主题 (替代手扫 106 个 section)
    hits = await sb.find("https://docs.python.org/3/whatsnew/3.13.html", "JIT")
    for h in hits["sections"]:
        print(f"[{h['section_index']}] {h['heading']} (score={h['score']})")

    # 抽取主题摘要 (返回围绕关键词的紧凑内容)
    topic = await sb.extract_topic("https://docs.python.org/3/whatsnew/3.13.html", "PEP 703", max_chars=2000)
    print(topic["sections"][0]["excerpt"])

    await sb.close()

asyncio.run(main())
```

## 页面分类类型

| 类型 | 图标 | 说明 |
|------|------|------|
| `article` | 📄 | 博客文章、新闻、帖子 |
| `docs` | 📚 | 技术文档、API 文档 |
| `search` | 🔍 | 搜索结果页 |
| `login` | 🔐 | 登录/注册页 |
| `list` | 📋 | 列表/目录/标签页 |
| `dashboard` | 📊 | 后台管理面板 |
| `error` | ❌ | 错误页 (404/500) |
| `video` | 🎬 | 视频页 |
| `unknown` | ❓ | 未识别 |

## 记忆持久化

所有浏览数据存储在 `~/.semantic-browser/memory.db` (SQLite)：
- 跨会话保持记忆
- 支持续跑（昨天爬了 30 页，今天继续）
- WAL 模式，并发安全

## 已验证场景

- ✅ 博客文章识别 (blog.python.org → article, 90% confidence)
- ✅ 技术文档识别 (docs.python.org → docs)
- ✅ 搜索页识别 (Google → search)
- ✅ 站点拓扑图生成 (blog.python.org → 18 节点树)
- ✅ SQLite 记忆持久化
- ✅ 真实 LLM 增强分类 (DeepSeek e2e, article/docs/login/search 全部正确)
- ✅ 主题抽取 (`sb extract-topic "url" "PEP 703"` — Python 3.13 whatsnew: 1488 字符精炼摘要)
- ✅ 持久浏览器 daemon (`tb-daemon` HTTP server, 7/7 e2e 测试通过)
- ✅ `--json-out` valid JSON (含 CJK / 转义符 / 嵌套数组)

## 安全审计工具

详见 [docs/design-log.md](docs/design-log.md)（T40—T66 逐版本设计笔记）与 [docs/api/security.md](docs/api/security.md)。

## 后续路线

已交付（见 [CHANGELOG.md](CHANGELOG.md)）：MCP Server、页面分类 LLM 增强、
登录态保持（storage_state 快照）、Stealth 反检测、多 agent 共享 daemon。

- [ ] 增量爬取（基于 Memory Store 的未访问链接队列）
- [ ] 页面相似度检测
- [ ] 代理出口（per-session proxy）

完整演进计划见 [super_plan.md](super_plan.md)。

## SemanticQuery — 模型驱动的浏览器语义层（核心功能）

**这是项目的核心定位 — 为"顶级 agent ↔ 浏览器"做 token 经济层。**

```
┌─────────────────────────────────────────────────────┐
│ 顶级 agent (Claude Opus)                            │
│ Input:  query("find X about Y") + budget=2000       │
│ Output: markdown 答案 + sources + tokens_used       │
└──────────────┬──────────────────────────────────────┘
               │ ~50 tokens 传入
               ▼
┌─────────────────────────────────────────────────────┐
│ SemanticQuery — M3 编排 (cheap, 不烧贵模型 token)    │
│  plan → browse → relevance filter → synthesize    │
│  + 多页 follow-link (T68)                            │
│  + 持久 cache (T68)                                  │
└──────────────┬──────────────────────────────────────┘
               │ ~500-1500 tokens 返回 (精炼 markdown)
               ▼
┌─────────────────────────────────────────────────────┐
│ 顶级 agent 最终决策（几乎不碰原始 DOM）              │
└─────────────────────────────────────────────────────┘
```

### 三种接入方式

**1. Python API**
```python
from semantic_browser.query import run_query
result = await run_query(
    "find GitHub PEP 703 discussions, give 3 perspectives",
    start_url="https://github.com/python/peps",
    budget=2000,
)
print(result.to_markdown())  # ~600 chars + citations [1]..[8]
print(result.tokens_used)    # tokens burned
```

**2. CLI**
```bash
sb query "Python 3.13 top 3 new features" \
    --start-url https://docs.python.org/3/whatsnew/3.13.html \
    --json-out | jq '.answer, .tokens_used'
```

**3. daemon HTTP（多 agent 共享）**
```bash
# 阻塞 (返最终 answer)
curl -X POST localhost:8765/v1/query \
  -d '{"query":"...", "start_url":"...", "budget":2000}'

# SSE 流式 (实时 progress)
curl -N -X POST localhost:8765/v1/query/stream \
  -d '{"query":"...", "start_url":"..."}'
# data: {"type":"start", ...}
# data: {"type":"phase", "phase":"plan_done", ...}
# data: {"type":"phase", "phase":"browse_done", ...}
# data: {"type":"phase", "phase":"relevance_done", ...}
# data: {"type":"phase", "phase":"synth_done", ...}
# data: {"type":"final", "answer": {...}}
```

**4. MCP 工具 (Claude Desktop / 其他 MCP 客户端)**
```python
mcp_tool("sb_query", {
    "query": "Python 3.13 features",
    "start_url": "https://docs.python.org/3/whatsnew/3.13.html",
    "budget": 2000,
})
```

### Token 经济（实测）

| 操作 | 没用 SemanticQuery | 用 SemanticQuery |
|---|---|---|
| 顶级 agent 处理 SPA 50KB DOM | ~50K tokens | ~500 tokens |
| 多步 goal agent | 多次 LLM 决策 + 全 DOM | 1 次 M3 cheap 调用 + 精炼 |
| 同 query 二次调用 | 又烧一次 token | **cache 命中, 0 token** |

### 模型与环境变量配置 (支持任意 LLM Provider)

Semantic Browser 纯粹模型中立，支持**任意 OpenAI 兼容接口**、DeepSeek、Ollama 本地部署模型、Claude 以及 MiniMax 等。用户可根据性价比自由配置中轻量 Tier 模型：

```bash
# 方式 A: 推荐 - 使用 DeepSeek / OpenAI 兼容 API (DeepSeek / Qwen / SiliconFlow)
LLM_PROVIDER=openai
OPENAI_API_KEY=your-api-key-here
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat

# 方式 B: 本地私有化 Ollama 零 Token 成本运行
# LLM_PROVIDER=openai
# OPENAI_API_KEY=ollama
# OPENAI_BASE_URL=http://localhost:11434/v1
# OPENAI_MODEL=qwen2.5-coder

# 方式 C: Anthropic 兼容 API (Claude Haiku / Minimax)
# LLM_PROVIDER=anthropic
# ANTHROPIC_AUTH_TOKEN=your-api-key-here
# ANTHROPIC_BASE_URL=https://api.anthropic.com
# ANTHROPIC_MODEL=claude-3-5-haiku-20241022

# SemanticQuery 预算与控制参数 (每次调用时传入, 无环境变量)
# budget=2000      LLM token 预算 (DEFAULT_BUDGET)
# max_pages=1      多页 follow-link 上限 (DEFAULT_MAX_PAGES)
# cache_ttl_s=600  cache TTL 秒 (DEFAULT_CACHE_TTL_S)
```

### 关键概念

| 名词 | 含义 |
|---|---|
| **plan** | M3 把 query 拆成 primary_target + sub_questions + keywords + expected_answer_format |
| **browse** | 本地 Playwright 打开 page + 提 snapshot + ContentExtractor 提 article sections |
| **relevance** | M3 给每个 section 打 0-1 分, ≥ 阈值保留; 三层 fallback (article → text_blocks → links) |
| **sufficiency** | overall confidence ≥ 阈值 → 提前 break 不浪费预算 |
| **follow-link** | (T68) M3 选下一个 URL: 多页 follow 提升答案完整度, 不需要人力找链接 |
| **synthesize** | M3 把 kept sections 合成 ≤ max_chars 的紧凑 markdown, 标 [1]..[N] 引用 |
| **cache** | (T67) 内存 LRU 64 + TTL 600s; (T68) 持久到 `~/.semantic-browser/query_cache.json` |

### E2E 验证

- ✅ 单页：Python 3.13 → 1090 tokens, 答案 ~600 chars + 8 引用
- ✅ 多页：HN threshold=0.99 → 翻 4 页 (front → shownew → news → front)
- ✅ Cache：同 query+URL 二次调用 0.00s, **0 token 消耗**
- ✅ HTTP daemon /v1/query 真集成: 4.2KB JSON 答案
- ✅ SSE stream /v1/query/stream: phase-by-phase 实时推送
- ✅ 持久 cache: 重启后 cache_hit=True, 完全跳过 LLM

