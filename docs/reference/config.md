# 配置参考

环境变量统一由 `semantic_browser/app_config.py` 管理（Round 3a）。

## LLM Provider

| 变量 | 说明 |
|------|------|
| `LLM_PROVIDER` | `openai` / `anthropic` / `gemini` / `ollama`；不设则按键自动探测 |
| `LLM_API_KEY` | 通用 key，优先于 provider 专属 |
| `LLM_BASE_URL` | 通用 base URL，优先于 provider 专属 |
| `LLM_MODEL_CHEAP` / `LLM_MODEL_MEDIUM` / `LLM_MODEL_SMART` | 三档 tier 模型覆盖 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL`（旧名 `OPENAI_API_BASE`） / `OPENAI_MODEL` | OpenAI 兼容（含 DeepSeek / vLLM） |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`（Claude Code 风格） / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Gemini |

provider 默认 base：openai → `https://api.deepseek.com/v1`，
anthropic → `https://api.anthropic.com`，
gemini → `https://generativelanguage.googleapis.com`。

provider 自动探测顺序：`LLM_PROVIDER` → ANTHROPIC_* key → GEMINI/GOOGLE key
→ base URL 尾缀 `:11434/v1`（ollama）→ fallback openai。

## SemanticQuery

无常驻环境变量；`budget` / `max_pages` / `cache_ttl_s` 均为每次调用参数，
默认值见 [../api/query.md](../api/query.md)。

## daemon / 工具链

| 变量 | 说明 |
|------|------|
| `SB_DAEMON_BASE` | `tb` CLI 默认 daemon 地址（fallback `http://127.0.0.1:$SMOKE_PORT`） |
| `SMOKE_PORT` | 上面 fallback 的端口，默认 8765 |
| `SEMANTIC_BROWSER_DAEMON_URL` | MCP server 代理到 daemon 的地址 |
| `SB_DEBUG` | CLI 错误时打印 traceback（`1/true/yes/on`） |

## daemon CLI 参数（`tb-daemon`）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | 127.0.0.1 | 监听地址 |
| `--port` | 8765 | 监听端口 |
| `--headed` | off | 有头模式 |
| `--state` | — | Playwright storage_state JSON |
| `--ssrf-allowlist` | — | 逗号分隔 host/通配 |
| `--allow-data-scheme` | off | 放开 `data:` URL（测试用） |
| `--m-browsers` / `--k-contexts` | 6 / 16 | M×K 容量模型 |
| `--watchdog-interval` | 5.0s | browser 健康检查 |
| `--sweep-interval` | 60.0s | storage_state 快照扫描 |
| `--session-idle-timeout` | — | 空闲 session 回收 |
| `--lease-heartbeat-ttl-s` | 15.0 | lease 心跳 TTL |
| `--drain-timeout` | 30.0 | SIGTERM 排空超时 |
| `--verbose` / `-v` | off | debug 日志 |
