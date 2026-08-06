# SemanticQuery API

SemanticQuery 是项目核心：顶级 agent 给一个自然语言问题 + token 预算，
系统用 cheap-tier 模型完成 plan → browse → relevance → synthesize，
返回精炼 markdown 答案 + 引用 + token 计量。

```
顶级 agent (贵模型)
   │ query + budget (~50 tokens)
   ▼
SemanticQuery — M3 编排 (cheap 模型)
   │ plan → browse → relevance filter → (follow-link) → synthesize
   ▼
精炼 markdown 答案 + sources (~500-1500 tokens) + tokens_used
```

## HTTP（daemon，多 agent 共享）

### `POST /v1/query`（阻塞）

```json
{ "query": "...", "start_url": "https://...", "budget": 2000, "max_pages": 3 }
```

- `start_url` 可省略 → 走 M3 自动站点发现
- `budget`：LLM token 预算（默认 2000 = `SemanticQuery.DEFAULT_BUDGET`）
- `max_pages`：多页 follow-link 上限（默认 1 = 单页）

响应 envelope：`{"ok": true, "data": {"request_id": ..., "answer": {...}}, "error": null}`

### `POST /v1/query/stream`（SSE）

同上参数，逐 phase 推送：`start → plan_done → browse_done →
relevance_done → synth_done → final`。

### 其他

| 端点 | 说明 |
|------|------|
| `GET /v1/query/stats` | cache 命中/未命中、并发余量 |
| `GET /v1/query/log` | 最近 query 日志 |
| `POST /v1/query/cache/clear` | 清 cache |

## Python API

```python
from semantic_browser.query import run_query
result = await run_query(
    "find GitHub PEP 703 discussions, give 3 perspectives",
    start_url="https://github.com/python/peps",
    budget=2000,
)
print(result.to_markdown())
print(result.tokens_used)   # {"used": {...}, "max_total": ..., "cache_hit": ...}
```

## CLI

```bash
sb query "Python 3.13 top 3 new features" \
    --start-url https://docs.python.org/3/whatsnew/3.13.html --json-out
```

## MCP

```python
mcp_tool("sb_query", {"query": "...", "start_url": "...", "budget": 2000})
```

## Cache

- 内存 LRU（64 条）+ TTL（默认 600s = `DEFAULT_CACHE_TTL_S`，构造时可改 `cache_ttl_s`）
- 持久化到 `~/.semantic-browser/query_cache.json`（重启后仍命中，0 token）
- 命中 key = query + start_url + budget + max_pages

## 默认值一览（`SemanticQuery` 类常量）

| 常量 | 值 | 含义 |
|------|----|------|
| `DEFAULT_BUDGET` | 2000 | LLM token 预算 |
| `DEFAULT_MAX_PAGES` | 1 | follow-link 页数上限 |
| `DEFAULT_SUFFICIENCY` | 0.7 | 提前收手置信度阈值 |
| `DEFAULT_RELEVANCE_THRESHOLD` | 0.3 | section 保留相关度阈值 |
| `DEFAULT_ANSWER_MAX_CHARS` | 2000 | 答案最大字符数 |
| `DEFAULT_CACHE_TTL_S` | 600 | cache TTL |

## 关键概念

| 名词 | 含义 |
|------|------|
| plan | 拆 query → primary_target / sub_questions / keywords / expected_format |
| browse | Playwright 开页 + snapshot + ContentExtractor 提 sections |
| relevance | cheap-LLM 给每 section 打 0-1 分，≥ 阈值保留（三层 fallback） |
| sufficiency | confidence ≥ 阈值即提前收手，不浪费预算 |
| follow-link | 多页时由 LLM 选下一个 URL |
| synthesize | 把保留 sections 合成 ≤ max_chars 的 markdown，标 `[1]..[N]` |
