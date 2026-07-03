# Semantic Browser

> Agent-readable semantic browser layer — 给 AI Agent 用的透明浏览器

**不是又一个浏览器工具，而是 Chromium 之上的 Site Intelligence Layer。**

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
| **Browser Controller** | `browser/controller.py` | Playwright 封装：open/click/type/scroll/screenshot |
| **Snapshot Engine** | `snapshot/engine.py` | 语义快照：文本块、链接、控件、meta 信息 |
| **Page Classifier** | `classifier/heuristic.py` | 启发式分类：article/docs/search/login/list/error/dashboard/video |
| **Content Extractor** | `extractor/content.py` | 正文提取（标题/作者/日期/段落/代码块）+ 接口提取 |
| **Memory Store** | `memory/store.py` | SQLite 持久化：页面/链接/操作/会话/笔记 |
| **Website Graph** | `graph/builder.py` | 站点拓扑图：页面关系树 |
| **Engine** | `engine.py` | 核心编排：串联所有模块 |
| **CLI** | `cli/main.py` | 命令行入口 |

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

## 安全审计增强套件 (T40 + T42)

给 agent / 安全审计工具用的站点情报扩展。所有工具都同时通过 MCP / CLI / daemon 三层暴露：

```bash
# T40a: 客户端存储探针 (local/session/cookies)
tb dump-storage
# T40b: 隐藏路径探针 (well_known/discovery/admin)
# T42f: 加上 debug/actuator 类 (/actuator/* /phpinfo /swagger ...)
tb probe-paths https://example.com --categories well_known,discovery,admin,debug
# T40c: HTML 注释提取 (含 shadow root)
# T40d: URL 参数解析 (链接 + form action)
# T40e: Frame inventory (depth/cross-origin/child_count)
tb list-frames
# T40f: CSP/HSTS/XFO 结构化
# T42c: 加上 CORS 风险评估 (high/medium/low/none)
tb security-headers https://example.com
# T40g: 从 JS 提取 API endpoints (fetch/axios/XHR)
tb extract-api-endpoints
# T40h: Shadow DOM 穿透 (snapshot 递归)
# T42d: SRI coverage + mixed content
# T40i: WebSocket 连接监控
tb websockets
# T42a: snapshot 含 hidden form 字段 + form 分类 (login/search/upload/...)
# T42b: 识别 JS 库版本 + 已知 CVE (jQuery 3.4.0 → CVE-2020-11022/11023)
tb extract-js-libraries
# T42g: GraphQL introspection (dump schema types/queries)
tb detect-graphql https://api.example.com/graphql
```

T42 补的是 pen-tester 视角盲点 (CSRF token 抓不到 / JS lib CVE 不识别 / CORS misconfig 不报警 / SRI 不检查 / soft-404 不识别 / debug 端点不探 / GraphQL schema 不 dump / 上传字段不标) — 实测在 GitHub 上成功抓到 `authenticity_token` CSRF token.

## T43 — Pen-tester 第二轮盲点 (10 项)

用户第一轮"全修"之后再次以 agent 身份对真实站点跑全套 T40+T42, 又发现 10 个缺失能力:

```bash
# T43a: 子域名枚举 — crt.sh (Certificate Transparency) + TLS cert SAN
tb enumerate-subdomains github.com
# T43b: JS 源码硬编码 secret 扫描 (AWS key / GitHub token / Bearer / api_key / 私钥)
tb extract-secrets-from-js
# T43c: WAF 指纹 (Cloudflare / Akamai / Imperva / AWS WAF / Fastly / Vercel / Netlify / Sucuri)
tb detect-waf
# T43d: 开放重定向 / SSRF sink 检测 (returnUrl, redirect, next, url, callback, ...)
tb find-open-redirect-sinks
# T43e: 敏感信息泄露 (email / 内网 IP / AWS key / GitHub token / 私钥 / 调试堆栈 / TODO)
tb find-disclosure
# T43f: 备份/源码/配置文件暴露分析 (.git/HEAD / .env / phpinfo / .DS_Store)
#       注: .env 解析只列 key 不列 value, 避免误报出真密码
tb analyze-exposed-files
# T43g: OpenAPI / Swagger 自动发现 + 解析 (paths / methods / by_method)
tb discover-api-specs
# T43h: TLS 证书解析 — issuer / 有效期 / SAN → 子域
tb tls-subdomains github.com
# T43i: 技术栈指纹 (Server / X-Powered-By / meta generator / 框架 cookie)
tb fingerprint-tech
# T43j: JWT 探测 + payload 解码 (在 storage/cookie/页面里找, 不验签)
tb decode-jwts
```

覆盖的是 pen-tester recon 阶段最常用的能力: 子域扫描、敏感泄露、技术栈识别、secret 抓取。所有 10 项同时通过 MCP / CLI / daemon 三层暴露。

## T44 — Pen-tester 第三轮盲点 (12 项)

T43 之后用户再次以 agent 身份对 github.com/login + example.com 跑全套, 又发现 12 个缺失能力:

```bash
# T44a: DNS 记录 (A/AAAA/MX/NS/TXT-SPF/DMARC) — DoH (dns.google) 避开 dig 依赖
#        自动解读: SPF ~all 软失败 / DMARC p=none 监控模式 / 缺 DMARC
tb dns-records github.com
# T44b: Wayback Machine 历史 URL — 旧端点/旧 secret 常没清理
tb wayback-urls https://example.com
# T44c: DOM XSS sinks (eval / innerHTML / document.write / Function / setTimeout 字符串)
tb find-xss-sinks
# T44d: CAPTCHA + OAuth provider + WebAuthn/2FA 联合检测
#        reCAPTCHA / hCaptcha / Turnstile / FunCaptcha + Google/GitHub/FB/Apple/MS OAuth
tb detect-auth-methods
# T44e: CSRF 覆盖率 — 对当前页每个 form 检查 token 字段 (T42a 抓 token, 但没检查每个 form 都有)
tb check-csrf-coverage
# T44f: IDOR-prone URLs (/user/N, /order/N, /api/v1/users/N ...)
tb find-idor-urls
# T44g: 云资源泄露 (S3 / Azure Blob / GCP / Heroku / Firebase / CloudFront)
#        实测在 github.com 抓到 github-cloud.s3.amazonaws.com
tb find-cloud-resources
# T44h: HTTP methods (OPTIONS + Allow header) — 找 PUT/DELETE/PATCH/TRACE 入口
tb probe-http-methods
# T44i: 2FA / MFA 专门检测 (WebAuthn / TOTP / SMS / backup code / Duo)
tb detect-2fa
# T44j: 外部资源清单 (外链域名 / 跨域脚本 / iframe / 跨域 form) — 供应链 / trust boundary 分析
tb inventory-external-resources
# T44k: CSP 头深度解析 — 拆 directive + 标危险配置 (unsafe-inline / unsafe-eval / * / data:)
tb parse-csp
# T44l: 子域接管信号 — 查 CNAME 跟易被接管服务签名比对 (S3/Heroku/Azure/CloudFront/GitHub Pages ...)
tb check-subdomain-takeover example.com
```

**关键安全洞察 (实测):**
- `tb dns-records github.com` → 报告 "SPF ends with ~all (softfail) — 伪造邮件更易通过"
- `tb dns-records example.com` → "DMARC p=reject — 完全拒绝不合规邮件 (最好)"
- `tb find-xss-sinks` 在 github.com 抓到 5 处 `document.cookie` 读取 + 3 处 `innerHTML` 赋值
- `tb find-cloud-resources` 在 github.com 抓到 `github-cloud.s3.amazonaws.com`

## T47 — 可访问性审计 (axe-core 集成)

axe-core 4.10.2 已 vendored 进包 (`src/semantic_browser/assets/axe.min.js`, MPL 2.0, ~540KB), offline 就能跑 WCAG 2.1 A/AA 审计, 不依赖 CDN:

```bash
# 必须先 tb open 一个页面 (axe 在页面上下文跑)
tb open https://example.com/
tb a11y-audit --json-out | jq '.summary, .violations[:3]'

# 自定义标准 / 每个 violation 保留节点数
tb a11y-audit --standards wcag2aa,wcag21aa --max-nodes 10
```

返回结构:
- `summary.violations` / `passes` / `incomplete` / `inapplicable` + `by_impact` (critical / serious / moderate / minor 计数)
- 每个 violation 含 `id` / `impact` / `help_url` (Deque 文档) / `tags` (WCAG 条款) / `node_count` / `nodes` (html + target + failure_summary)

实测: 故意写一个无 alt 的 `<img>` + 空 `<button>` + 空 `<a>` 的页面, axe 准确抓到 4 处违规:
```
[critical] button-name      1 node
[critical] image-alt        1 node
[serious]  html-has-lang    1 node
[serious]  link-name        1 node
```

## T51 — 并发安全 (浏览器单实例串行化)

daemon 用 ThreadingHTTPServer 多线程接 HTTP, 但浏览器 / controller 是单实例 — 多线程并发改 `current_page` / `snapshot` 会互相覆盖. T51 加 `op_lock` 串行化所有 controller-touching 操作:

```bash
# 1. /queue — 看当前 op + 等锁的请求数
$ curl -s http://127.0.0.1:8765/queue | jq
{
  "ok": true,
  "data": {
    "current_op": "GET /snapshot-vision",
    "running_for_s": 4.21,
    "lock_held": true,
    "waiters": 1,
    "lock_timeout_s": 30
  }
}

# 2. 等不到锁 → 503 + DAEMON_BUSY (可重试)
$ curl -s http://127.0.0.1:8765/snapshot
{"ok": false, "data": null,
 "error": {"code": "DAEMON_BUSY",
           "message": "another operation still running (waited 30.0s); check /queue or retry",
           "retryable": true}}
# HTTP 503 + retryable=true — agent 应 sleep 后重试, 不要干瞪眼
```

**白名单**: `/health` / `/queue` / `/stats` 不需要锁 (纯只读). 其它端点都进锁 — `/open` / `/click` / `/snapshot` / `/discover` / `/snapshot-vision` / ...

**关键测试**:
- `test_concurrent_open_serializes`: 两个 `/open` 并发跑, 都成功, 最终状态是其中一个 (后跑赢)
- `test_queue_shows_running_op_during_long_task`: SSE discover 期间 `/queue` 报 `current_op` + `running_for_s`

**`/queue` 字段**:
- `current_op`: 当前正在跑的方法 + 路径 (例 `GET /snapshot-vision`)
- `running_for_s`: 已运行时长 (秒, 2 位小数)
- `lock_held`: 锁是否被持有 (true=忙 / false=空闲)
- `waiters`: 等锁的请求数
- `lock_timeout_s`: 锁等超时 (默认 30s; 超时返 503)

agent 用法:
```python
# 提交任务前, 先看 daemon 闲不闲
queue = await call("GET", "/queue")
if not queue["data"]["lock_held"]:
    await call("POST", "/open", {"url": url})
else:
    # 忙 — 等 done 或 backoff 重试
    eta = queue["data"]["running_for_s"]
    await asyncio.sleep(max(1, eta))
```

**测试**: 4 新 (queue 空闲 / 并发 open 串行化 / SSE 期间 queue 显示 running / 锁正确释放). 全套 **571 passed, 7 skipped**.

## T57 — T40/T42/T43/T44 安全工具 MCP 暴露 + daemon 代理模式

MCP server 现在 66 个工具, 覆盖 T40+T42+T43+T44 全部 22 项 site intelligence 工具, 加 T18 调试 (console/network/errors) + T54 sessions + T56 capacity/admin 暴露. 关键设计: **可选 daemon 代理** —

```python
# 默认: in-process (每 MCP 客户端一个独立 SemanticBrowser, 适合轻量 Claude Desktop 用)
# 通过 SEMANTIC_BROWSER_DAEMON_URL env 或 MCPServer(daemon_url=...) 切到 daemon 代理:
import os
os.environ["SEMANTIC_BROWSER_DAEMON_URL"] = "http://127.0.0.1:8765"
# 现在 sessions/capacity/admin/queue/health 走 daemon HTTP — 多 agent 共享 chromium
```

**T57 新增 MCP 工具** (11):
- `sb_get_console` — JS console 消息 (log/warn/error 过滤) — XSS 审计
- `sb_get_network` — 网络请求缓冲 (按 method/失败过滤) — 敏感 endpoint 审计
- `sb_get_page_errors` — 页面 JS 异常 — SPA 排查
- `sb_sessions_list/create/delete` — 多 agent session CRUD (T54, 需 daemon)
- `sb_capacity` — sessions_active/max/ratio + degradation_level/label (T56, 需 daemon)
- `sb_admin_degrade/restore` — 显式 bump/restore 降级 (T56, 需 daemon, 测试/运维)
- `sb_queue` — op_lock 状态 (T51, 需 daemon) — agent 决定 backoff
- `sb_health` — 增强 health (T49, 需 daemon)

**业务错误透传**: daemon 返回的 `CAPACITY_DEGRADED` / `SESSION_NOT_FOUND` / `DEGRADED_READONLY` 等稳定错误码, MCP 层不丢, 通过 `_DaemonProxyError` 保留 code/level 字段. agent 一次调用拿全错误语义, 不用猜测.

**测试**: 13 新 (3 T18 in-engine / 3 缺 daemon_url 错误 / 5 daemon 代理走通 / 1 错误透传 / 1 env 注入). 全套 **612 passed, 7 skipped**.

## T59 — SSE pressure events (fable §2.5 backpressure)

Agent 订阅 daemon SSE stream, 容器/降级变化时主动避让, 不必每次轮询 `/capacity` — 是 §2.5 提到的"守规 agent 提前避让":

```bash
# 订阅全部事件
$ curl -N http://127.0.0.1:8765/events
id: 191
data: {"topic": "system.pressure", "payload": {"level": "critical", "capacity_ratio": 0.96, "reason": "auto_capacity"}, "seq": 191}

id: 192
data: {"topic": "daemon.degraded", "payload": {"level": 2, "label": "L2_preempt_low", "pressure": "critical", ...}, ...}

# 触发条件: capacity ≥ 0.85 (L1) / ≥ 0.95 (L2); admin 显式 (L1-L4); admin restore (回到 normal)

# 订阅指定 topic pattern
$ curl -N 'http://127.0.0.1:8765/events?topics=system.pressure'
# 只推 system.* 事件, daemon.* / session.* 噪声不进

# 跨重启续传 (T55 契约)
$ curl -N -H 'Last-Event-ID: 192' http://127.0.0.1:8765/events
# 先 replay bus 上 seq>192 的事件再接 live
```

**Topic / event 协议**:
- `system.pressure{level: normal|soft|high|critical, prev, reason, capacity_ratio, ts}` — 通用 backpressure 信号; 只在 level 真变化时发, 不 spam
- `daemon.degraded{level: 0-4, label, pressure, reason, capacity_ratio, ts}` — 显式降级事件; 同 event 一起发便于区分自动/手动
- 后续 `browser.crashed` (T60), `pool.pressure` (T60+) 都走同一通道 — 一个 SSE 端点全覆盖

**auto_degrade 只升不降 + pressure 镜像**:
- L1 (≥0.85) → system.pressure{level=high, reason=auto_capacity}
- L2 (≥0.95) → system.pressure{level=critical}
- admin degrade L1/L2 → high, L3/L4 → critical
- admin restore → normal (level 不再 auto-restored, 显式 `/admin/restore`)

**`/events` SSE 实现**:
- 协议复用 T55 SSE 续传 (`Last-Event-ID` header) + 200ms poll-based bridge (避开 asyncio.Queue 跨线程 fanout 问题)
- `topics` query param 默认 `*` (通配新增), 支持 `system.*` / `daemon.*` 等
- bridge task 在 daemon 自己的 event loop 上跑 (`asyncio.run_coroutine_threadsafe`); bridge_q (thread-safe) 投给 HTTP handler
- 不进 op_lock (放行路径); L4 全拒时仍可用 (degraded allowed)
- 不写入 Prometheus duration 直方图 (长流会扭曲)

**新容量字段**: `/capacity` 现在带 `pressure_level`, 一次拿全状态.

**测试**: 7 新 (admin 显式降级发 high / restore 发 normal / /capacity 含 pressure_level / SSE headers 正确 / live event 流 / Last-Event-ID 重传 replay / 不抢 op_lock). 全套 **656 passed, 7 skipped**.

## T60 — Browser watchdog + M×K capacity (fable §5.5 + §1.2)

daemon 后台 task 周期性 (默认 5s) 给心跳/健康监测:

```bash
$ curl -s http://127.0.0.1:8765/capacity | jq
{
  "M": 1, "K": 16, "slots_total": 16,
  "browsers_count": 1, "mem_per_browser_estimate_mb": 3310,
  "mem_total_estimate_mb": 5610,
  "last_heartbeat_ts": 1751408400.12, "heartbeat_age_s": 1.4,
  # 上面是 T56/T59 字段也都在
  "degradation_level": 0, "pressure_level": "normal"
}

# 订阅心跳
$ curl -N http://127.0.0.1:8765/events?topics=system.heartbeat
id: 205
data: {"topic": "system.heartbeat", "payload": {"pid": 1234, "browsers_alive": 1,
  "M": 1, "K": 16, "sessions_active": 3, "degradation_level": 0, "ts": 1751408405.2}}

# 监控卡死
$ curl -N 'http://127.0.0.1:8765/events?topics=browser.*'
data: {"topic": "browser.lock_stuck", "payload": {"op": "open", "held_seconds": 31.2, ...}}
```

**M×K 容量模型** (fable §1.2 公式实现):
```
mem_per_browser = BASE(250MB) + K × (CTX(15MB) + P̄(1.5) × PAGE(120MB))
slots_total    = M × K
mem_total      = M × mem_per_browser + DAEMON(300MB) + OS_RESERVE(2GB)
```
默认 16vCPU/64GB 机 → M=1/K=16/slots=16/约 3.3GB 单实例. 当前 pool 共享单 chromium 进程, M 仅作字段暴露; 多 worker 化留待 T62+.

**Watchdog 后台 task** (`serve_forever` 启动, `shutdown` 取消):
- 每 tick (5s 默认, `--watchdog-interval=0` 关闭):
  - `_last_heartbeat_ts = time.time()` 更新
  - `_watchdog_once` 检测 `op_lock` 被持 >30s → 发 `browser.lock_stuck{op, held_seconds}`
  - `list_sessions()` 失败 → 视为 browser 挂, 发 `browser.crashed`
- 每次 tick 发 `system.heartbeat{pid, browsers_alive, M, K, sessions_active, degradation_level}` 到 bus
- /events 订阅者用 `topics=system.heartbeat` 即可监控 daemon 还活着

**新增主题**:
- `system.heartbeat` — 5s 一次 (默认)
- `browser.lock_stuck` — op 卡 >30s (连续发, 噪声)
- `browser.crashed` — pool 层面异常 (RARE)

**CLI flags**:
```bash
tb daemon --m-browsers 1 --k-contexts 16 --watchdog-interval 5
# 测试 / 关 watchdog
tb daemon --watchdog-interval 0  # 关闭
```

**测试**: 3 新 (capacity M×K 字段完整 / 心跳真的发到 bus / 字段定义完整). 全套 **659 passed, 7 skipped**.

## T61 — storage_state 自动快照 (fable §5.4)

agent 一关 browser 登录态就丢 — 老问题. T61 把每个 session 的 cookies/localStorage 周期性快照到文件系统 + SQLite 索引, daemon 重启 / session preempt 后可恢复:

```bash
# Session 上线后 60s 内自动首次快照 (或 navigate 后 5s debounce)
# 之后每 60s sweep 一次, 只抓 dirty session
# 保留 3 份最新, 单份 ≤ 2MB (超限截断最大 localStorage key)

$ ls ~/.semantic-browser/snapshots/
default/
  ss_1751408400_a1b2c3d4e5f6.json   ← cookies + localStorage + origins
  ss_1751408460_b2c3d4e5f6a7.json
agent-1/
  ss_1751408480_c3d4e5f6a7b8.json
```

**架构** (评审 D4 — 故障章权威):
- **blob → 文件系统** (`~/.semantic-browser/snapshots/{session_id}/{snapshot_id}.json`) — JSON 单文件, 防止 SQLite WAL 在高写入下放大
- **索引 → SQLite** (`session_snapshots` 表): `snapshot_id, session_id, taken_at, trigger, size_bytes, open_pages, file_path, truncated`
- **容量硬限**: 单份 2MB (`_MAX_SNAPSHOT_BYTES`); 超限截断最大的 `localStorage.origins[i].localStorage[j].value` 并标 `truncated=true`
- **保留策略**: 每 session 保留 3 份 (`_RETENTION_COUNT`); 自动 GC 旧, 删文件 + 删索引行

**触发器** (统一 debounce 合并):
- `auto_sweep` — 后台 60s sweep, 只抓 dirty session (`session.storage_state.saved` bus 事件)
- `navigate_dirty` — `_open()` 成功时 mark dirty, 下次 sweep 抓
- 失败 → 发 `session.storage_state.failed{reason}` 到 bus; 不重试, 留给下个 tick

**实现**:
- `daemon/snapshots.py` (~165 行): `SnapshotStore` — SQLite 索引 + 文件系统 + 截断 + GC + dirty 集
- 后台 task `_start_snapshot_sweeper()`: 60s tick (`--sweep-interval=60` 可配; 0 = 关闭)
- 启动: `serve_forever` 启 sweeper task; `shutdown` 取消 + 关闭 sqlite
- dirty 集是 in-memory (`_dirty_lock` 保护); 当前不持久化, 重启时丢失 (acceptable — 大多数 dirty session 紧接着会再被 navigate 触发)

**API 路径** (后续 T62+ agent 接入):
```
GET /sessions/{id}/snapshots       — 列出某 session 快照 (最新在前)
GET /sessions/{id}/snapshots/{sid} — 读快照内容 (audit / 调试)
POST /sessions/{id}/snapshots/sweep — 手动触发 sweep (管理员)
```

**测试**: 8 新 SnapshotStore unit (`test_snapshot.py` 覆盖 mark dirty / take / open_pages / roundtrip / truncate > 2MB / GC 留 3 份 / list newest first). daemon integration 验证 sweeper 起动 + shutdown. 全套 **667 passed, 7 skipped**.

## T62 — Graceful drain on SIGTERM (fable §5.8)

daemon 收到 SIGTERM/SIGINT 改用 `shutdown()` 走完整 drain, 而不是直接 OS-default 退出. 之前在飞的 RPC 会断、agent 重连拿 connection reset, 整个工作流得重新跑. T62 把流程拆成三段:

```text
  signal SIGTERM
        │
        ▼
  _begin_drain()   ── 标 _draining=True + 发 daemon.draining 事件到 bus
        │
        │ (后台 drain 线程, 不阻塞 signal handler)
        ▼
  _finish_shutdown_after_drain()
        │ 等待 (默认 30s):
        │  • 当前 op 完成 (op_lock 释放)
        │  • 或 drain_timeout 到 → 发 daemon.drain_timeout 事件后强制
        ▼
  _finish_shutdown()  ── watchdog/sweeper task 取消 → httpd.shutdown() → owner.close()
```

**对 agent 的契约**:
- drain 中所有 write op（`/open` / `/click` / `/agent/run` 等）返 `503 DAEMON_DRAINING` + `Retry-After: 5`，body 带 `error.draining: true`
- 只读观测（`/health` / `/queue` / `/capacity` / `/metrics` / `/events` / `/admin/*`）照常工作 — agent 可以订阅 `/events?topics=daemon.*` 提前得到 `daemon.draining` 通知并切到备用节点
- in-flight op 跑完才真关；如果超 30s 还卡，发 `daemon.drain_timeout{op, held_seconds}` 警告后强制

**新增端点** `POST /admin/drain`（ops/测试手动触发，无需真杀进程）：
```bash
$ curl -X POST http://127.0.0.1:8765/admin/drain
{"ok": true, "data": {"draining": true, "drain_timeout_s": 30.0, ...}}
```

**改动**:
- `daemon/server.py`: `DAEMON_DRAINING` 错误码 (503) + `_DrainError` 异常 + `_begin_drain()` / `_finish_shutdown_after_drain()` / `_finish_shutdown()` 三段；`/health` 加 `draining / drain_elapsed_s / drain_timeout_s / in_flight_op` 字段；`POST /admin/drain` 端点；CLI `--drain-timeout=30`
- `shutdown()` 改为只标记 + 启动后台线程，不阻塞信号 handler

**测试**: 11 新 `TestT62GracefulDrain`（4 单元 + 7 集成），核心覆盖：
- 单元: `_enforce_drain` 在 drain 中拒写、放观测；`_begin_drain` 幂等；`DAEMON_DRAINING` → 503
- 集成: `/health.draining=false` 初始；`POST /admin/drain` → `/health` 报 `draining=true`；新 op 拿 503 + Retry-After:5；`/health /queue /metrics` drain 中仍可用；`daemon.draining` 事件进 bus

全套 **678 passed, 7 skipped** （+11 vs T61 套件）。

## T63 — Dogfooding UX 修复 (agent 实测反馈)

T62 上线后我作为 agent 真把 daemon 当工具用了一遍 (开 wikipedia 搜 "semantic browser" + 看安全 headers), 暴露了 10 条新手 agent 撞上的摩擦点. T63 / T63.1 / T63.2 三批全修了:

**T63 (4 条):**
- **`/state` 加 `type`** — agent 决策循环不用再调 `/snapshot` 拿当前 page 类型
- **`/open` 一站式给 refs** — 默认返 `{refs: [{ref, kind, text, href}], ref_count}`; agent 第一次 open 后能立刻 click, 不必先调 `/snapshot` 拿 ref 列表. `?detail=full` 时返完整 snapshot (`text_blocks/scripts/raw_aria` 全)
- **`/security-headers` 加 numeric score** — 旧的 `score: "OK/weak/missing"` 含义不明; 加 `score_points` (int) + `score_max` (满分 9, T63.2 笔误校正) 让 agent 用 numeric 写阈值
- **`tb daemon stop` 等时长对齐 drain_timeout** — 原来 hard-code 3s, 没 in-flight 时 `owner.close()` 关 browser 实例要 10s+ 不够. 改成 `--drain-timeout` 参数 (默认 30s)

**T63.1 (3 条 polish):**
- `tb daemon start` 加 `--allow-data-scheme` CLI flag (跟 daemon flag 对齐)
- `/capacity` 去重冗余字段 — 删 `browsers_count` (==M), `last_heartbeat_ts`/`heartbeat_age_s` 合并成 `watchdog_heartbeat_age_s`
- `/sessions?detail=1` 返每 session 当前 url+title, agent 不用 N+1 次 `/state?session=NAME`

**T63.2 (3 条 — 修了剩下全部 dogfooding 反馈):**
- **`/open` summary 更丰富** (修 2): 默认返 `heading` (h1 text) + `top_headings` ([h1]/[h2]/...) + `meta` (description/lang) + `counts` (text_blocks/links/controls/forms/scripts). 0 额外 I/O, 都是 snapshot 已有值. agent 第一次开页就能判断页面大致内容
- **`/open` 三段式分类 + 缓存** (修 3): 启发式 → URL 缓存 → LLM-augment. simple landing page (e.g. example.com) 启发式常判 `unknown`, 配 `OPENAI_API_KEY` 后 LLM 二次判断兜底. 同 URL 二次 `/open` 秒返 `type_source="cached"` 复用分类结果, 0 LLM 重调. **没配 key → silent 走启发式**, 不破原行为
- **`/security-headers` 加 letter grade** (修 10): 老 `score: "OK/weak/missing"` string 含义不清. 加 `score_grade` A-F (≥80%=A, ≥60%=B, ≥40%=C, ≥20%=D, 否则 F), agent 写阈值 `score_grade in {A,B}` 直白

`_classify_with_cache` 缓存 (URL → {page_type, confidence}) 256 LRU. `/state` 也吃缓存, `/open` → `/state` 常见模式无重复 LLM.

### 端点映射: daemon vs MCP

agent 既能用 daemon HTTP 端点也能用 MCP 工具, 两套 API 风格不同 (daemon kebab-case + GET/POST, MCP snake_case + JSON-RPC):

| 用途 | daemon 端点 | MCP 工具 | 说明 |
|------|------------|---------|------|
| 浏览 | `POST /open`, `POST /click`, `POST /type`, ... | `sb_browse`, `sb_click`, `sb_type`, ... | 写 op 用 POST + JSON body |
| 读 op | `GET /snapshot`, `GET /read`, `GET /state` | `sb_snapshot`, `sb_history`, ... | 读 op 用 GET + query string |
| 安全 (T40-T44) | `GET /security-headers`, `GET /dns-records`, ... | `sb_security_headers`, `sb_dns_records`, ... | kebab ↔ snake 别名 |
| Sessions | `GET /sessions[?detail=1]`, `POST /sessions`, `DELETE /sessions/{name}` | `sb_sessions_list/create/delete` | daemon 走 HTTP, MCP 走 daemon proxy |
| 降级 | `POST /admin/degrade`, `POST /admin/restore`, `POST /admin/drain` | (只有 daemon) | 显式运维操作 |
| 监控 | `GET /health`, `GET /capacity`, `GET /queue`, `GET /metrics`, `GET /events` | `sb_health`, `sb_capacity`, `sb_queue`, (无 /events) | SSE 端点只有 daemon 有 |
| Agent | `POST /agent/run`, `POST /agent/run/stream` | `sb_agent_run`, `sb_agent_plan` | stream 端点 SSE |

### SSE 实时事件 (T59)

agent 想看实时降级/压力事件, 不必轮询 `/capacity` — daemon 暴露 `GET /events` SSE 流, 推送 `system.pressure` + `daemon.degraded` + `daemon.draining` 等. MCP 没有这个端点, agent 用 daemon HTTP 直连.

```bash
curl -N http://127.0.0.1:8765/events
# data: {"topic": "daemon.degraded", "data": {"level": 2, ...}, "ts": ...}
```

测试 25 个 (T63 + T63.1 + T63.2) `TestT63*` 全过; 总测试 776 passed.

## T58 — SSRF guardrail (fable §7.1)

Agent 让浏览器"任意 URL 导航"是个 SSRF 大坑 — 攻击面包括 AWS / GCP metadata (`169.254.169.254` / `metadata.google.internal`) / 内网服务 / localhost 旁路 / `file:///etc/passwd`. T58 在 daemon `_open()` 入口加 default-deny 闸门, 任何 URL 进 browser controller 前先过这道闸:

```bash
# 默认拒: 私网 / loopback / link-local / cloud meta / .internal / .local
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"file:///etc/passwd"}'
{"ok": false, "error": {"code": "SSRF_BLOCKED", "message": "...", "retryable": false}}

# 内网 IP
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"http://10.0.0.1/admin"}'
{"ok": false, "error": {"code": "SSRF_BLOCKED", ...}}

# cloud metadata (literal hostname)
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"http://metadata.google.internal/"}'
{"ok": false, "error": {"code": "SSRF_BLOCKED", ...}}

# 公网 OK
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"https://example.com/"}'
{"ok": true, ...}
```

**核心设计** (`safety/ssrf.py`, ~160 行):
- `check_url(url, *, allowlist, resolver) → str | raise SSRFBlockedError` — pure function, 易测
- **DNS rebinding 防护**: 先 `socket.getaddrinfo()` 拿到所有 A 记录, 任何 IP 命中黑名单 (RFC1918 / loopback / link-local / CGNAT / IPv6 ULA / IPv4-mapped IPv6) 即拒
- **scheme 默认 deny**: 只放行 `http`/`https`. `file://` / `chrome://` / `javascript:` / `data:` / `view-source:` 全拒 (防浏览器内部 scheme 旁路 + data: URL XSS)
- **host pattern 默认 deny**: `*.internal` / `*.local` / `*.localhost` / `metadata.google.internal` / `metadata.internal`
- **公网 IP / 公网 host 直通** (有 resolver 可注入, 测试用 fake DNS)
- **allowlist 支持精确 + `*.example.com` 通配** — 测试 fixture / 内网开发绕过
- **解析失败默认拒** (避免 NXDOMAIN 状态穿过去)
- **大小写不敏感**: `MyHost.LOCAL` 一样拒, `FILE://` 一样拒

**daemon 集成** (`server.py:_open`):
```python
async def _open(self, url, session=None):
    try:
        checked_url = _ssrf_check(
            url, allowlist=self._ssrf_allowlist,
            allow_data=self._allow_data_scheme,
        )
    except SSRFBlockedError as e:
        # 错误向上抛 → classified as SSRF_BLOCKED (400)
        raise
    ctrl = await self.owner.aget_controller(session)
    page = await ctrl.open(checked_url)
```

**新错误码** (`_STATUS_BY_CODE` + `result.py`): `SSRF_BLOCKED` (400) — `retryable: false` (URL 不会自愈).

**CLI 标志**:
```bash
# 测试 fixture / 内网开发
tb daemon --ssrf-allowlist "*.test.example,internal.dev" --allow-data-scheme
```

**架构分层** (为什么放 daemon 而不是 controller):
- `controller.open()` 是低层 Playwright 封装, 任何调用方都该受这道闸保护
- 在 daemon HTTP 边界守着, MCP 客户端 + CLI + 直 API 调用全受益
- engine / extractor 等内部模块自己解析 host 时可以独立调用 `check_url()` (后续 T60+ agent path 会接入)

**测试**: 37 新 (29 unit `tests/test_ssrf.py` 覆盖 block/allow/allowlist/wildcard/IPv6/rebinding/case + 8 daemon integration `TestT58SSRFGuardrail` 通过 `/open` 验证返回 `SSRF_BLOCKED`). 全套 **649 passed, 7 skipped**.

## T56 — DegradationController L0-L4 + /capacity + 错误码扩展

daemon 在容量/资源压力下按 L0-L4 自动降级, agent 通过 `/capacity` 查当前退路, 不用猜; 阻挡的请求返回 503 + `Retry-After` 头让客户端做 backoff:

```bash
$ curl -s http://127.0.0.1:8765/capacity | jq
{
  "sessions_active": 1, "sessions_max": 20,
  "capacity_ratio": 0.05,
  "degradation_level": 0, "degradation_label": "L0_healthy"
}

# 测试: 强制 L3 (只读)
$ curl -X POST http://127.0.0.1:8765/admin/degrade -d '{"level":3}'
{"ok": true, "data": {"level": 3, "label": "L3_readonly"}}

# 写 op 拒
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"..."}'
HTTP/1.0 503
Retry-After: 30
{"ok": false, "data": null, "error": {
  "code": "DEGRADED_READONLY", "message": "daemon at degradation L3 (readonly) — refusing write op POST /open",
  "retryable": true, "level": 3}}

# 恢复
$ curl -X POST http://127.0.0.1:8765/admin/restore
{"ok": true, "data": {"level": 0, "label": "L0_healthy"}}
```

**降级级别 (fable §5.7) — 单进程内, 0ms 开销**:
| Level | 行为 | 触发条件 (auto) |
|---|---|---|
| L0_healthy | 全放行 | 默认 |
| L1_reject_new | 拒 POST /sessions (CAPACITY_DEGRADED), 其余放行 | `capacity_ratio ≥ 0.85` |
| L2_preempt_low | 同 L1 (fable 提议的抢占低优 session — 未实现, 留作 T57+) | `≥ 0.95` |
| L3_readonly | 拒所有写 op (DEGRADED_READONLY), 读 op 放行 | admin 显式 / 后续 OOM hook |
| L4_full | 拒除 /health / /queue / /capacity / /metrics / /admin 之外的全部 (SERVICE_UNAVAILABLE) | admin 显式 / 灾难模式 |

**新错误码** (`_STATUS_BY_CODE`): `CAPACITY_DEGRADED` (503) / `DEGRADED_READONLY` (503) / `SERVICE_UNAVAILABLE` (503) — 全部 `retryable: true` + 503 + `Retry-After: 30` 头.

**只升不降的自动机**: auto_degrade 只升级 (capacity 高时), 降级必须显式 `/admin/restore` — 避免 admin 刚 bump 完被下一请求 auto_degrade 回落.

**测试**: 8 新 (capacity 默认 / L1 拒新 / L3 阻写 / L4 阻全 / restore / 越界校验 / Retry-After 头 / auto 升不降). 全套 **596 passed, 7 skipped**.

## T55 — 持久化 Event Bus + SSE Last-Event-ID 续传

daemon 长任务 (SSE 流) 现在跨连接/重启持久化, agent 重连不带 `Last-Event-ID` 不丢事件 — 跟 LLM 增量 token 流一样的"游标续传":

```bash
# 跑流, 中途 ctrl-C 断开
$ tb agent-run "search for X" --stream
[start] goal="search for X" max_steps=20
[1/20] open https://google.com ✓
[2/20] type 'X' into search box ✓
... ctrl-C ...

# 重连 — 带 Last-Event-ID
$ curl -N -H "Last-Event-ID: 5" -X POST http://127.0.0.1:8765/agent/run/stream \
    -H "content-type: application/json" -d '{"goal":"search for X","max_steps":20}'
# daemon 读出 Last-Event-ID=5, 从 bus 拿 seq>5 的事件全 replay, 然后接 live
id: 6
data: {"type": "step", "step": 3, "action": "click", ...}
...
```

**架构** (fable §3.1 简化版, 单进程 / SQLite WAL):
- `EventBus.publish(topic, payload) → seq` — 同步写 SQLite WAL, 自增 seq, `event_id` UUID 去重 (LRU 200k)
- `EventBus.replay(since_seq, topic) → [events]` — 同步读, 给 SSE 重连续传
- `EventBus.subscribe(topic) → asyncio.Queue` — 给同进程内 live 推送 (T56+ 跨 controller)
- 双层去重: LRU + UNIQUE(event_id) — 极小概率碰撞也安全
- topic glob: `session.*` 匹配 `session.created`, `agent_run.foo` 精确匹配

**SSE 帧格式 (W3C)**: 每帧前带 `id: <seq>` 行, 客户端用 `Last-Event-ID` 头重连时断点续传.

**Topic 命名**: `agent_run.<goal前50字符>` / `discover.<start_url>` — 同一任务的多次连接能续到同一 stream.

**测试**: 3 新 (publish+replay round-trip / SSE `id:` 字段 / Last-Event-ID 重连拿的事件 id 全部 > 游标).

## T54 — 多 session 隔离 + /sessions CRUD

每个 agent 一个独立 `BrowserContext` (cookie/storage/cache 独立), 共用一个 chromium 进程 — 内存省 10x, 隔离保真:

```bash
# 列活跃 session (default 必存在)
$ curl -s http://127.0.0.1:8765/sessions | jq
{"ok": true, "data": {"sessions": ["default"], "active_count": 1}}

# 创建
$ curl -X POST http://127.0.0.1:8765/sessions -d '{"name":"agent-1"}'
{"ok": true, "data": {"name": "agent-1", "created": true, "active": ["default", "agent-1"]}}

# 自动生成
$ curl -X POST http://127.0.0.1:8765/sessions -d '{}'
{"ok": true, "data": {"name": "agent-2", "created": true, ...}}

# 显式 session 参数 — 操作落到该 session 的 context
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"https://a.com","session":"agent-1"}'
$ curl -X POST http://127.0.0.1:8765/open -d '{"url":"https://b.com","session":"agent-2"}'
# 互不干扰 (cookie/storage/page state 各自独立)

# 关闭
$ curl -X DELETE http://127.0.0.1:8765/sessions/agent-1
{"ok": true, "data": {"name": "agent-1", "released": true, ...}}
```

**架构** (沿用 T33 `ControllerPool`): 共享 chromium 进程, 每个 session 独立 `BrowserContext` (Playwright 的 incognito-like 沙箱). max_contexts=20 默认.

**默认 session**: daemon 启动时预创建 `default` — 旧代码 (无 `session` 参数) 落到这里, 100% 兼容.

**错误码**:
- `SESSION_NOT_FOUND` (404) — DELETE 不存在的
- `CANNOT_DELETE_DEFAULT` (400) — 不能删 default
- `SESSION_CREATE_FAILED` (503) — pool 满 / chromium 拉不起来

**`/sessions` POST 不传 `name`**: 自动 `agent-N` (N = 当前活跃数 + 1).

**测试**: 9 新 (list 含 default / create 成功 / 自动生成名 / delete 成功 / 404 / 不能删 default / 跨 session 隔离 / state 隔离 / 默认 session 隐式). 全套 **588 passed, 7 skipped**.

## T53 — /agent/run/stream SSE 流式 agent

之前 `/agent/run` 阻塞等 GoalAgent 跑完 (20 step × ~3s/step = 60s+), agent 干等浪费时间. T53 加 SSE 流式端点, 每 step 实时回传, agent 可以边看边 abort:

```bash
# 流式
$ tb agent-run "fill the form" --stream
[start] goal="fill the form" max_steps=20
[step 1] think ✓  Open login page
[step 2] open https://example.com/login ✓
[step 3] snapshot ✓  found 12 refs
...
[done] success=True steps=8 in 24.1s

# 阻塞老接口 — 仍兼容
$ tb agent-run "fill the form"
```

**协议** (同 /discover/stream):
```
POST /agent/run/stream
{"goal": "...", "max_steps": 20, "tier": "smart", "allow_destructive": false}

data: {"type": "start", "goal": "...", "max_steps": 20}
data: {"type": "step", "step": 1, "action": "think", "args": {...}, "success": true, "thought": "..."}
data: {"type": "step", "step": 2, "action": "open", "args": {"url": "..."}, "success": true, ...}
...
data: {"type": "done_result", "result": {完整 GoalResult.to_dict() 含 success/steps/elapsed/notes}}
```

复用 `GoalAgent.on_step` 钩子 — 跟 `/agent/run` 共享同 agent loop 实现, 不双轨.

**测试**: 3 新 (SSE 头/格式 / on_step 数据正确 / 客户端可消费). 全套 **579 passed, 7 skipped**.

## T52 — Prometheus /metrics 端点

daemon 现在原生吐 Prometheus 文本, 拿 `requests_total` / `request_duration_seconds` / `op_lock_wait` / `op_lock_hold` / `errors_total` / `daemon_uptime` 就能上 Grafana — 不用自造 exporter:

```bash
$ curl -s http://127.0.0.1:8765/metrics
# TYPE tb_requests counter
tb_requests_total{method="GET",path="/health",status="200"} 4
tb_requests_total{method="POST",path="/open",status="200"} 1
tb_requests_total{method="GET",path="/snapshot",status="200"} 2
# TYPE tb_request_duration histogram
tb_request_duration_bucket{method="GET",path="/health",le="0.01"} 4
tb_request_duration_bucket{method="GET",path="/health",le="+Inf"} 4
tb_request_duration_count{method="GET",path="/health"} 4
tb_request_duration_sum{method="GET",path="/health"} 0.002
...
# TYPE tb_op_lock_wait histogram
tb_op_lock_wait_bucket{path="/snapshot",le="0.01"} 2
tb_op_lock_wait_bucket{path="/snapshot",le="+Inf"} 2
tb_op_lock_wait_count{path="/snapshot"} 2
tb_op_lock_wait_sum{path="/snapshot"} 0.001
# TYPE tb_op_lock_hold histogram
tb_op_lock_hold_bucket{path="/snapshot",le="0.05"} 1
...
tb_op_lock_hold_count{path="/snapshot"} 2
tb_op_lock_hold_sum{path="/snapshot"} 0.083
# TYPE tb_errors counter
tb_errors_total{method="POST",path="/open",code="MISSING_PARAM"} 1
tb_daemon_uptime_seconds 142.37
```

Content-Type: `text/plain; version=0.0.4; charset=utf-8` — Prometheus / VictoriaMetrics / Grafana Agent 直接 scrape.

**指标清单**:
- `tb_requests_total{method,path,status}` — counter, 每个 HTTP 请求
- `tb_request_duration_seconds{method,path}` — histogram, 端到端请求时长 (SSE 长流除外, 会扭曲)
- `tb_op_lock_wait_seconds{path}` — histogram, 抢 op_lock 等待时长 (T51 串行化)
- `tb_op_lock_hold_seconds{path}` — histogram, 拿到 op_lock 后持锁时长
- `tb_errors_total{method,path,code}` — counter, 协议层 4xx + 业务层错误码 (例 `MISSING_PARAM` / `DAEMON_BUSY` / `INVALID_URL`)
- `tb_daemon_uptime_seconds` — gauge, 进程启动时长

**Prometheus scrape config** 一行搞定:
```yaml
scrape_configs:
  - job_name: semantic_browser
    static_configs:
      - targets: ['127.0.0.1:8765']
```

**关键测试**:
- `test_metrics_endpoint_returns_prometheus_text`: 验 `text/plain` + 非 JSON envelope
- `test_metrics_includes_required_series`: 必含 4 类 series (request / duration / errors / uptime)
- `test_metrics_includes_error_counter`: 4xx + 业务错码都进 `tb_errors_total`
- `test_metrics_records_op_lock_wait_and_hold`: `op_lock_*` 在多 op 后有 bucket 数据
- `test_metrics_increments_after_request`: 同一路径 N 次 → counter 准确递增

**测试**: 5 新 (Prometheus text 格式 / 必含 series / errors / op_lock 直方图 / counter 递增). 全套 **576 passed, 7 skipped**.

## T50 — 长任务进度流式回传 (SSE)

`tb discover` 现场爬站点可能要 30 秒+, 之前调用方只能干等. T50 加 SSE (Server-Sent Events) 流式端点, 客户端实时拿每页进度:

```bash
# 流式 (推荐) — 边爬边打印, 不再 dry wait
$ tb discover https://example.com --stream --max-pages 10
[start] https://example.com max_pages=10 max_depth=2
[1/10] https://example.com/ — Example Domain
[2/8] https://example.com/page2 — Page Two
[3/8] https://example.com/page3 — Page Three
[done] pages=3 failed=0 in 1.2s
Pages visited: 3
Pages failed:  0

example.com/
├── /
└── /page2
    └── /page3

# 老式 (阻塞, 等全部完成才返回) — 仍兼容
$ tb discover https://example.com
```

**协议** (任何 HTTP client / EventSource 都能消费):
```
GET /discover/stream?start_url=...&max_pages=N&max_depth=N

data: {"type": "start", "start_url": "...", "max_pages": N}
data: {"type": "page", "url": "...", "title": "...", "pages_done": N, "queue_remaining": M}
data: {"type": "failure", "url": "...", "error": "..."}
data: {"type": "done", "pages_done": N, "pages_failed": M, "total_seconds": S}
data: {"type": "done_result", "result": {完整 discover 结果, 含 tree_text / llm_summary / graph_dict}}
```

SSE header: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, keepalive comment (`:`) 每 15s 一次防中间设备断连.

**Agent 用法** (Python):
```python
import urllib.request, json
req = urllib.request.Request(f"{base}/discover/stream?start_url=...")
with urllib.request.urlopen(req, timeout=300) as resp:
    for line in resp:
        if not line.startswith(b"data: "): continue
        event = json.loads(line[6:])
        if event["type"] == "page":
            log(f"[{event['pages_done']}] {event['url']}")
        elif event["type"] == "done_result":
            result = event["result"]  # 完整 tree / graph
```

`discover()` 内部加可选 `progress_callback` 参数, SSE 端点只是它的 HTTP 包装; 想做别的传输 (websocket / 长轮询 / MCP progress notification) 也能复用同一回调.

**测试**: 5 新 (2 unit 验证 discover 的 progress_callback 行为含 None 静默, 3 SSE 端点含 missing param / 真实 data URL 完整流 / browser state 副作用). 全套 **567 passed, 7 skipped**.

## T49 — Daemon 生命周期加固

`tb daemon` 现在能正确处理崩溃/端口冲突/僵尸, 不再让用户面对裸 `OSError: address in use`:

**`tb daemon start` 预检**:
```bash
# 已有 daemon 在跑 → 拒绝 + 提示
$ tb daemon start --port 8765
Error: daemon already running on port 8765 (pid 12345); use `tb daemon stop --port 8765` first, or pass --force

# 强制重启 (会先 SIGTERM 现有 daemon)
$ tb daemon start --port 8765 --force
--force: stopping existing daemon (pid 12345) first
started: http://127.0.0.1:8765 (log: ~/.semantic-browser/daemon.log)

# 端口被非-daemon 进程占用 → 清晰错误
$ tb daemon start --port 80
Error: port 80 already in use on 127.0.0.1 (not us); pick another --port

# 后台模式不再 race — 轮询 /health 而不是固定 sleep
$ tb daemon start --background
started: http://127.0.0.1:8765 (log: ~/.semantic-browser/daemon.log)
```

**Stale PID 文件自动清理**: daemon 崩溃 (kill -9 / OOM / 段错误) 后 PID 文件残留 → 下次 start 时自动检测到进程已死, 清理掉再起.

**SIGTERM/SIGINT 优雅关闭**: daemon 收到信号后调 `shutdown()` (停 http server + 关浏览器 + 删 PID 文件) 而不是 OS 默认硬退出. `tb daemon stop` 流程也更可靠:

```bash
$ tb daemon stop --port 8765
stopped: daemon on port 8765 (pid 12345)
```

**`/health` 增强** (agent 排查时省一次 roundtrip):
```bash
$ curl -s http://127.0.0.1:8765/health | jq
{
  "ok": true,
  "data": {
    "status": "ok",
    "pid": 12345,
    "host": "127.0.0.1",
    "port": 8765,
    "uptime_seconds": 342.1,
    "page_url": "https://example.com/dashboard"
  }
}
```

**测试**: 14 个新测试 (8 unit 覆盖 `_pid_alive` / `_read_pid_file` / `_check_stale_pid` / `_port_in_use`, 3 CLI 覆盖 start 预检, 3 `/health` 增强). 全套 **562 passed, 7 skipped**.

## T48 — 类型化 Result 契约 (跨层一致)

daemon / MCP / CLI 三处都改用统一 `Result<T> = {ok, data, error}` envelope, agent 不用再为不同入口写不同错误处理:

```python
# 成功
{"ok": True,  "data": {...},  "error": None}

# 失败 (含稳定错误码 + 是否可重试)
{"ok": False, "data": None,   "error": {"code": "NETWORK_FAIL", "message": "...", "retryable": True}}
```

**稳定错误码 (7 个)**: `PAGE_NOT_OPENED` / `NETWORK_FAIL` / `INVALID_URL` / `EMPTY_RESULT` / `MISSING_PARAM` / `NOT_IMPLEMENTED` / `INTERNAL`

**HTTP 状态码映射 (daemon)**: 错误码 → 4xx/5xx — 客户端不用解析 body 也能粗判:
- `MISSING_PARAM` / `INVALID_URL` → 400
- `PAGE_NOT_OPENED` → 409
- `NETWORK_FAIL` → 502
- `NOT_IMPLEMENTED` → 501
- 其它 → 500

**MCP 透传**: tool 错误不破坏 JSON-RPC 200, 改用 `isError: true` + 内层 Result envelope. agent 一次调用拿全错误语义:
```json
{"isError": true, "content": [{"type": "text", "text": "{\"ok\": false, \"data\": null, \"error\": {\"code\": \"MISSING_PARAM\", ...}}"}]}
```

**CLI 转换**: `tb` 命令自动把 `error` 字段转成 `[CODE] message (retryable: yes/no)` 一行, 失败时 exit 码非 0.

agent 写一次错误处理:
```python
r = await call_tool(...)
if not r["ok"]:
    if r["error"]["retryable"]:
        await asyncio.sleep(2 ** attempt)
        # retry
    else:
        log(f"non-retryable: {r['error']['code']}: {r['error']['message']}")
```

**测试覆盖**: 16 个新测试 (result.py 9 + daemon 4 + MCP 2 + CLI 1) + 旧测试全部更新到 envelope 形状, **548 passed, 7 skipped**.

## T45 — 架构级别审计 (错误 / 重复 / 冲突)

T40+T42+T43+T44 共 39 项工具加完后, 跑了一轮 AST 级 + 跨层一致性审计, **零 findings** — 代码库结构干净:

| 检查项 | 结果 |
|---|---|
| 类内同名方法 (silent override) | 0 — 上次修过的 `get_storage` / `read_storage` 已稳定 |
| Click 同一组内命令冲突 | 0 |
| daemon 同一 if 链重复路径 | 0 |
| MCP `sb_xxx` 重名 | 0 |
| Module-level 死代码 | 0 (pyflakes: 0 unused imports / 0 undefined names) |
| 长函数 (>180 行) | 3 (都是派发表 — `_dispatch` 393 / `_extract_interactive` 291 / `_call_tool` 255, 重构纯 cosmetic) |
| `except Exception: pass` | 13 处全部合法 (重试循环 / URL parse 兜底 / per-element 迭代 / multi-strategy 软 404) |
| T43+T44 跨层暴露 | 22/22 全栈覆盖 (controller → MCP / CLI / daemon / 测试) |
| 全套测试 | 526 passed, 7 skipped (LLM e2e 需 OPENAI_API_KEY) |

## 后续路线

- [ ] MCP Server 封装（作为 Hermes MCP 插件）
- [ ] 页面分类 LLM 增强（低置信度时调 LLM 二次判断）
- [ ] 增量爬取（基于 Memory Store 的未访问链接队列）
- [ ] 页面相似度检测
- [ ] 登录态保持
- [ ] 代理 + Stealth 模式
