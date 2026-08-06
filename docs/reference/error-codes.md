# 错误码参考

所有层（daemon HTTP / MCP / CLI）共享同一 Result envelope：

```json
{"ok": false, "data": null, "error": {"code": "...", "message": "...", "retryable": false}}
```

agent 应匹配 `error.code`（稳定），不要匹配 message；`retryable=true`
才可自动重试。

## 错误码 → HTTP 状态

| code | HTTP | 含义 |
|------|------|------|
| `MISSING_PARAM` | 400 | 缺参/参数非法 |
| `INVALID_URL` | 400 | URL 解析失败 |
| `SSRF_BLOCKED` | 400 | URL 命中 SSRF 闸 |
| `CONFIRM_REQUIRED` | 409 | 危险动作需人类确认（带 `confirm_destructive` 重发） |
| `PAGE_NOT_OPENED` | 409 | 需要先 /open |
| `SESSION_NOT_FOUND` | 404 | session 不存在 |
| `SNAPSHOT_NOT_FOUND` | 404 | storage_state 快照缺失 |
| `LEASE_INVALID` | 404 | lease 不存在/已失效 |
| `CANNOT_DELETE_DEFAULT` | 400 | default session 不可删 |
| `SESSION_CREATE_FAILED` | 503 | session 创建失败 |
| `DAEMON_BUSY` | 503 | op_lock 等不到（带 Retry-After） |
| `BUSY` / `BUSY_LOWER_PRIORITY` | 409 | lease 被占/被更高优先级抢占 |
| `FENCE_MISMATCH` | 409 | fence_token 过期 |
| `LEASE_LOST` | 409 | lease 中途丢失 |
| `CAPACITY_DEGRADED` | 503 | 降级 L1：拒新 session |
| `DEGRADED_READONLY` | 503 | 降级 L3：只读 |
| `SERVICE_UNAVAILABLE` | 503 | 降级 L4：全拒 |
| `DAEMON_DRAINING` | 503 | SIGTERM 排空中（带 Retry-After） |
| `LLM_UNAVAILABLE` | 503 | strict 模式 LLM 失败（可重试） |
| `NETWORK_FAIL` | 502 | DNS/连接/超时（retryable） |
| `EMPTY_RESULT` | — | 查询无数据（不应重试） |
| `NOT_IMPLEMENTED` | 501 | 未实现 |
| `INTERNAL` | 500 | 兜底 |

定义位置：`daemon/server.py:_STATUS_BY_CODE`、`result.py:classify_exception`。
