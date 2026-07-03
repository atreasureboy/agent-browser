# Changelog — semantic-browser

格式: T 编号 + 中文短标题 + 子项 bullet + commit hash; 时间倒序 (新→旧). 不是 Keep a Changelog.

## T66.7 — Audit coverage expansion (C1 + C2 + C4 + C7)

**Headline**: T66.6 修完 audit event 的 tenant_id 后, 继续审计发现核心所有权原语和 session 生命周期还有 4 类盲区 — 多 agent 共享 daemon 时 ops 缺可观测性.

**新增事件 (C1)**:
- `session.lease.acquired` — POST /sessions/{name}/lease. payload: `lease_id`/`fence_token`/`agent_id`/`preempted_lease_id`/`priority`. dedup 按 lease_id.
- `session.lease.released` — DELETE /sessions/{name}/lease/{id}. payload: `lease_id`/`fence_token`/`reason`. dedup 按 lease_id.
- `session.handoff.offered` — POST /sessions/{name}/handoff. payload: `from_agent`/`to_agent`/`offer_token`/`deadline_ms`/`ttl_s`. 跟 `session.handed_off` (accept 端) 配对. **额外**: 跟 T66.6.2 一致, handoff_offer 现在用 `cur.tenant_id` 不用 body.

**Tenant_id 补齐 (C2)**: 新增 `_publish_with_session_tenant` helper — 自动从 `lease_manager.sessions_index` (持久化, 跨重启) 读 tenant_id, fallback in-memory meta. 统一给 `session.storage_state.saved`/`failed`, `session.expired` 用. 修复: 这些事件之前 scope 字段写 'session' 但 tenant_id 字段没设, 订阅者按 tenant 过滤不到.

**Session lifecycle (C4)**: 2 个新事件 — `session.created` (POST /sessions) + `session.deleted` (DELETE /sessions/{name}). tenant_id 优先 sessions_index, fallback in-memory.

**State save audit (C7)**: `_save_state` 加 `state.exported` 事件 (trigger=`user_explicit`, 跟 `session.storage_state.exported` 区分). **意外修了 latent bug**: `_save_state` 自 T8 起调 `self.owner.browser.save_storage_state` — `_BrowserShim` 只暴露 `.controller`, 该方法在 controller 上. 改成 `controller.save_storage_state`. 该 bug 让 /state/save 自 T8 起在 daemon 路径下一直 500.

**测试**: 8 个新测试 (`TestT66p7*`) — lease acquire/release/handoff-offered 各 1, session-scoped 1, session.created/deleted/state.exported 各 1. **总测试数 180 passed** (172 旧 + 8 新, 零回归).

**Breaking changes**: 无. 全是增量 audit events — 老订阅者不受影响.

## T66.6 — Audit/Metadata 一致性修复 (B1 + B2 + B3)

**Headline**: Scope A 上线后 agent 体验测试发现 3 个真实缺陷 (audit event 错 tenant / metadata 重启丢失 / handoff 写 anonymous). 核心所有权原语 (lease/fence) 本身 OK, 错的是审计 + 元数据层.

**B2 — session metadata 持久化 (高严重度)**:
- **T66.6.1**: `sessions_index` 升为 session metadata 的 source of truth. `lease.py` 加 `list_session_meta()` / `upsert_session_meta()` / `get_session_meta()` 方法, `sessions_index` 表加 `created_at_ms` 列 (启动时 `PRAGMA table_info` 检查 + `ALTER TABLE` 幂等迁移). `set_session_meta` 镜像写 SQLite, `_AsyncOwner.__init__` 启动时调 `list_session_meta()` 预热. 跨重启保留 tenant/agent 元数据.

**B3 — handoff tenant 写错 (中严重度)**:
- **T66.6.2**: `_handle_handoff_accept` 改用 `cur = lease_manager.get_active_for_session(name)` 拿原 offer 时的 lease, `tenant_id = cur.tenant_id` (不读 request body), 传给 `accept_handoff`. `set_session_meta` + audit event 都用 `result.lease.tenant_id` (accept_handoff 已写到 sessions_index).

**B1 — audit event tenant 错 (中严重度)**:
- **T66.6.3**: 4 个 handler audit event 统一从权威源取 tenant:
  - `session.restored` (reattach): `cur.tenant_id` (原 lease, 不读 body)
  - `session.storage_state.exported`: 优先 `lease_manager.get_session_meta()` (持久化) → fallback in-memory meta
  - `session.handed_off` (handoff): T66.6.2 已修
  - `daemon.drain.cancelled`: 保持 `'anonymous'` (global admin op, 无 tenant 上下文, 加注释说明)

**测试**: 8 个新测试 (`TestT66p6*`) — 持久化/handoff tenant 保留/3 类 audit event tenant 正确/重启端到端. **总测试数 172 passed** (164 旧 + 8 新, 零回归).

**测试间隔离修复**: T66.6.1 让 sessions_index 跨重启保留, 暴露 T65p6 「DB 默认空」隐性 bug. `daemon` fixture 加 `_reset_global_sb_db()` 启动前清 `leases.db` + `event_log.db` (含 WAL/SHM), 让每 test 拿到干净状态. 不影响生产 daemon.

**Breaking changes**: 无. 修的都是 T65/T66 引入的内部不一致.

## T66 — v1 namespace 第二波 (Scope A: session lifecycle + admin)

**Headline**: T66 调研涉及 5 个子系统 (lifecycle / blackboard / artifacts / LLM proxy / admin), 全套 5-7 天. 用户决策 Scope A 只做 session lifecycle + admin (1-1.5 天, 零新模块); blackboard / artifacts / LLM proxy / observers / admin reconcile 推迟到 T67+.

**Session lifecycle**:
- **T66.1 — Reattach (POST /sessions/{id}/reattach)**: daemon 重启 / 实例 crash 后, 旧 agent 用 `lease_id` + `fence_token` 恢复所有权. state ∈ ACTIVE/GRACE/RECOVERING 允许, RELEASED/EXPIRED → 410 LEASE_LOST, fence 不匹配 → 409 FENCE_MISMATCH. age > 300s → `advice="re_verify_auth"`. 每次 emit `session.restored` 审计事件 (dedup 幂等). 设计取舍: reattach **不 bump fence** — agent 真活着的话 bump 反而拒它后续写.
- **T66.2 — Handoff (POST /sessions/{id}/handoff + /accept)**: lease 状态机加 `OFFERED` 子状态 — A agent 主动让渡给 B: `offer` → 30s 内 `accept` → 原子换持有 + fence bump. reaper 扫过期 offer → 回 ACTIVE (A 继续持有, 不 bump fence). 错误码: BUSY 409 / OFFER_NOT_FOUND 410 / FENCE_MISMATCH 409 / OFFER_EXPIRED 410.
- **T66.3 — Storage state read (GET /sessions/{id}/storage_state)**: 读 `SnapshotStore.latest_snapshot()`, 不存在 → 404 SNAPSHOT_NOT_FOUND. 每次导出 emit `session.storage_state.exported` 审计事件 (dedup 按 content sha256 幂等).

**Admin**:
- **T66.4 — Drain cancel (POST /admin/drain/cancel)**: 撤销 drain 标志让 daemon 恢复接流量 (误触 / 提前中止排水时用). L4 状态时仍能 cancel (在 `_DEGRADED_ALLOWED` 白名单里). 每次 cancel emit `daemon.drain.cancelled` 事件.
- **T66.5 — Probes (/healthz vs /readyz 拆分)**: k8s 编排需要区分「进程在跑」(/healthz liveness, 永远 200) 和「能接流量」(/readyz readiness, drain/L4 时 503 + Retry-After: 30). `/health` 老路径保留 backward-compat 200 ok/draining + 完整 context.

**v1 namespace 扩展**: /v1/ 路径下加 `/v1/readyz` + `/v1/sessions/{id}/reattach` + `/v1/sessions/{id}/handoff[/accept]` + `/v1/sessions/{id}/storage_state` 5 个新端点 alias.

**测试**: 15 个新测试 (`TestT66p1*` ~ `TestT66p5*`), 总测试数 **164 passed** (零回归).

**Breaking changes**: T65.9 之前 /v1/healthz 等价 /health — T66.5 后 /v1/healthz 是 liveness probe (payload 简化为 `{alive, pid, uptime}`), /health 才是 full context. **测试断言从相等改语义拆分**. 老 dogfooding 直接调 /health 的代码不受影响.

## T65 — 多 agent 共享 daemon runtime (M=6/K=16 + tenant/agent + lease/fence + 持久 EventBus)

**Headline**: daemon 从「单进程 1×20 capacity 的单机工具」升级到「M×K=96 容量 + 多租户隔离 + lease/fence 防 GC 抢锁 + 持久事件总线」的生产级多 agent runtime.

**生产化加固 (2 天)**:
- **T65.1 — session idle 自动回收**: `_session_idle_timeout_s=300s` (env `DAEMON_SESSION_IDLE_TIMEOUT_S` 可改), `_start_snapshot_sweeper` 60s tick 加 idle 分支, 超时 → 关闭 BrowserContext + emit `session.expired`. 同时修了 T60/T61 隐藏的跨线程 `loop.create_task` bug — 一直没用 `run_coroutine_threadsafe`, watchdog/sweeper 自 T60 起就没真跑过. (`13efdc4`)
- **T65.2 — `/open` LLM strict 双路径**: `?strict=true` + LLM 失败 → 返 `LLM_UNAVAILABLE` 503 retryable=true; 默认 silent fallback 维持. (`T65.2 commit`)
- **T65.3 — SSE 客户端示例**: `examples/sse_client.py` (httpx + W3C SSE 解析 + 指数退避) + `examples/sse_client.ts` (native fetch + ReadableStream). README 加链接. (`78dfd65`)
- **T65.4 — CI workflow**: `.github/workflows/test.yml` (Python 3.11+3.12 矩阵, Playwright, `pytest -q`) + `smoke.yml` (daemon 端到端 `/health` + `/capacity` + `/open` + `/events` + `/sessions` CRUD). (`85e4625`)

**多 agent 共享架构 (3 天)**:
- **T65.5 — 容量升级到 M=6/K=16**: 默认值从 1×20 升到 6×16 (容量 96 个 context); 加 `M_BASE_MB=250 / M_CTX_MB=15 / M_PAGES=1.5` 常量, `_compute_mem_budget()` 暴露 `mem_per_browser_estimate_mb` / `mem_total_estimate_mb` / `mem_high_watermark` 到 `/capacity`. (`73fa688`)
- **T65.6 — Tenant + Agent 标识**: 每个 session 带 `tenant_id` + `agent_id` 元数据 (默认 `anonymous`/`anonymous`); `GET /sessions` 支持 `?tenant_id=` 过滤, `/capacity` 加 `tenants` 分布; 老非-detail 模式仍保持 `list[str]` 兼容 dogfooding. (`79c4895`)
- **T65.7 — Lease + Fence 所有权原语**: 新 `daemon/lease.py` (状态机 ACTIVE/GRACE/PREEMPTED/RECOVERING/EXPIRED/RELEASED + acquire/heartbeat/release API + reaper 后台线程) + `daemon/ulid.py` (26 字符 time-ordered ULID). HTTP 端点: `POST /sessions/{name}/lease` (acquire/preempt), `POST .../renew` (心跳), `DELETE .../lease/{id}` (release), `GET .../lease` (看状态). **踩坑**: DELETE 路径没读 body; lease DELETE 被 generic session DELETE 吞; UNIQUE INDEX 含 PREEMPTED 撞约束. (`7e8ef90`)
- **T65.8 — 持久 EventBus schema 扩展**: `events` 表加列 `scope` / `scope_id` / `tenant_id` / `producer_kind` / `producer_id` / `provenance` / `dedup_key` / `persistent` / `payload_json` / `expires_at`. `dedup_key` UNIQUE + INSERT OR IGNORE 兜底. `replay()` 加 tenant_id 过滤. SSE `/events` 加 `?tenant_id=` 查询参数. event_id 升 ULID. 向后兼容 — 老 `publish()` 调用零改动. (`4092d6b`)
- **T65.9 — `/v1/*` namespace 共存**: 多 agent 走 `/v1/*`, 老 dogfooding 路径零回归. v1 第一波核心 8 路由 (healthz/capacity/events/sessions CRUD/lease CRUD). 推迟到 T66: handoff/observers/blackboard/artifacts/llm-proxy/usage/budget/admin. (`8a0a737`)

**测试**: 41 个新测试 (`TestT65p1*` ~ `TestT65p9*`), 总测试数 **149 passed** (零回归).

**Breaking changes**: 无. 所有 T65 新增都是增量; 老调用者零改动.

---

## T64 — Dogfooding round 3

5 个测试 + UX 修复, agent 实测反馈. 详见 git log `--grep T64`.

## T63 — Dogfooding UX 修复 (round 1+2)

25 个测试覆盖 CLI polish + `/open` strict 路径 + LLM augment. T63.0 + T63.1 + T63.2 三个 commit.

## T63.x 之前

T40-T44 安全审计套件 (39 项 site intelligence 工具), T49-T56 daemon 生产化 (生命周期/op_lock/metrics/SSE/event_bus/sessions/降级), T57 MCP 暴露, T58 SSRF guardrail, T59 pressure events, T60 watchdog, T61 storage_state, T62 graceful drain. 详见 git log.