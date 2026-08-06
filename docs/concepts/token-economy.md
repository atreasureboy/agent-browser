# Token 经济模型

核心命题：顶级 agent（贵模型）不该直接消化原始 DOM。Semantic Browser
在中间做压缩层，让贵模型只处理精炼结果。

## 分层消费

| 层 | 模型 | 消费 |
|----|------|------|
| 顶级 agent | Opus/GPT-4 级 | query + 精炼答案（~550–1550 tokens） |
| SemanticQuery M3 | cheap tier | plan/relevance/synthesize（预算内） |
| 启发式兜底 | 无 LLM | 分类器 / snapshot 纯本地 |

## 预算与控制

- `budget`（默认 2000）：一次 query 的 LLM token 总预算，超了降级/截断
- `sufficiency_threshold`（0.7）：置信度够了提前收手
- `relevance_threshold`（0.3）：低相关 section 直接丢，不进 synthesize
- `max_pages`：多页 follow-link 上限，防预算被翻页烧光

## Cache 省钱

同 `(query, start_url, budget, max_pages)` 命中内存 LRU / 持久 cache →
**0 token**。持久 cache 在 `~/.semantic-browser/query_cache.json`，跨重启有效。

`GET /v1/query/stats` 可看 hit_rate。

## 计量

每次 LLM 调用经 `daemon/metering.py` 记 `UsageEvent`（provider/model/
input/output tokens/cost_micro_usd）入 SQLite，`/v1/usage` 汇总。
熔断 + 计量状态也并入 `/capacity`。

## 实测参考（README）

| 操作 | 直读 DOM | SemanticQuery |
|------|----------|---------------|
| SPA 50KB DOM 问答 | ~50K tokens | ~500 tokens |
| 同 query 二次调用 | 再烧一次 | cache 命中 0 token |
