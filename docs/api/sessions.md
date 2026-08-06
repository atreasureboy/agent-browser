# Sessions / Lease / Handoff（多 agent 共享 daemon）

daemon 支持多 agent 共享同一组浏览器实例，通过命名 session + lease（租约）
做互斥与所有权转移。

## Session CRUD

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/sessions` | 创建 session（body: `{"name": "..."}`） |
| `GET` | `/v1/sessions` | 列出所有 session |
| `GET` | `/v1/sessions/{name}` | session 详情 |
| `DELETE` | `/v1/sessions/{name}` | 释放 session |
| `POST` | `/v1/sessions/{name}/reattach` | 崩溃/重启后重新挂载 |
| `GET` | `/v1/sessions/{name}/storage_state` | 读 storage_state 快照 |

默认 session 名为 `default`，不可删除。

## Lease（租约）

防止两个 agent 同时写同一 session。

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/sessions/{name}/lease` | 获取/抢占 lease |
| `GET` | `/v1/sessions/{name}/lease` | 读 lease 状态 |
| `POST` | `/v1/sessions/{name}/lease/{id}/renew` | 心跳续租 |
| `DELETE` | `/v1/sessions/{name}/lease/{id}` | 释放 lease |

- 获取 lease 时可带 `priority`；更高优先级可抢占（preempt），返回
  `BUSY_LOWER_PRIORITY`。
- 每次写操作带 `fence_token` 单调递增令牌；旧 token 触发 `FENCE_MISMATCH`。
- 心跳 TTL 默认 15s（`--lease-heartbeat-ttl-s`），超时 lease 失效可被他人抢占。

## Handoff（所有权移交）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/sessions/{name}/handoff` | 发起移交 |
| `POST` | `/v1/sessions/{name}/handoff/accept` | 接受移交 |

## 相关错误码

`SESSION_NOT_FOUND` / `CANNOT_DELETE_DEFAULT` / `BUSY` /
`BUSY_LOWER_PRIORITY` / `FENCE_MISMATCH` / `LEASE_INVALID` / `LEASE_LOST`。
见 [../reference/error-codes.md](../reference/error-codes.md)。
