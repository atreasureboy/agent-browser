# 快速开始（5 分钟）

## 1. 配置 LLM

SemanticQuery 的 plan/relevance/synthesize 走 cheap tier 模型，任选其一：

```bash
# DeepSeek / 任意 OpenAI 兼容
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com/v1

# 或 Anthropic (Claude Code 风格也支持 ANTHROPIC_AUTH_TOKEN)
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# 或本地 Ollama（0 token 成本）
export LLM_PROVIDER=openai
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_MODEL=qwen2.5-coder
```

## 2. 启动 daemon

```bash
tb-daemon --port 8765 &
```

## 3. 查询

```bash
# 阻塞式
curl -s -X POST localhost:8765/v1/query \
  -d '{"query":"Python 3.13 top features","budget":2000}'

# SSE 流式（phase 实时推送）
curl -N -X POST localhost:8765/v1/query/stream \
  -d '{"query":"...","start_url":"https://docs.python.org/3/whatsnew/3.13.html"}'
```

## 4. 浏览操作（ref 驱动）

```bash
curl -s -X POST localhost:8765/open -d '{"url":"https://example.com"}'
curl -s localhost:8765/snapshot                 # 元素带 eN ref
curl -s -X POST localhost:8765/click -d '{"ref":"e3"}'
curl -s -X POST localhost:8765/type  -d '{"ref":"e5","text":"hello"}'
```

危险动作（label/text 含 delete/remove/submit 等）会返回
`CONFIRM_REQUIRED`（HTTP 409），需要人类确认后带
`"confirm_destructive": true` 重试。见
[../concepts/security-model.md](../concepts/security-model.md)。

## 5. MCP（Claude Desktop / 任意 MCP 客户端）

```json
{
  "mcpServers": {
    "semantic-browser": {
      "command": "python",
      "args": ["-m", "semantic_browser.mcp_server"]
    }
  }
}
```

连接已有 daemon（共享浏览器会话）：设 `SEMANTIC_BROWSER_DAEMON_URL=http://127.0.0.1:8765`。

## 下一步

- [api/query.md](api/query.md) — 查询 API 细节
- [api/sessions.md](api/sessions.md) — 多 agent 共享
- [reference/config.md](../reference/config.md) — 全部配置项
