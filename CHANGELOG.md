# Changelog — semantic-browser

格式: T 编号 + 中文短标题 + 子项 bullet + commit hash; 时间倒序 (新→旧). 不是 Keep a Changelog.

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