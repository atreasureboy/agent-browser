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
