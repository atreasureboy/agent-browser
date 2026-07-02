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
