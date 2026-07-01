# Semantic Browser

> Agent-readable semantic browser layer — 给 AI Agent 用的透明浏览器

**不是又一个浏览器工具，而是 Chromium 之上的 Site Intelligence Layer。**

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

## 后续路线

- [ ] MCP Server 封装（作为 Hermes MCP 插件）
- [ ] 页面分类 LLM 增强（低置信度时调 LLM 二次判断）
- [ ] 增量爬取（基于 Memory Store 的未访问链接队列）
- [ ] 页面相似度检测
- [ ] 登录态保持
- [ ] 代理 + Stealth 模式
