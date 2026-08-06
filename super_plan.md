# Super Plan: agent-browser 全面优化 (Phase 1 — 大方向)

> 目标: 将项目从"个人项目质量"提升到"成熟开源项目标准"
> 策略: 5 轮迭代，每轮聚焦一个维度，完成后审计再进入下一轮

## 进度 (2026-08-06)

- **Round 1**: ✅ 1a 已交付 (server.py dispatch 拆分); ✅ 1b 已交付
  (controller.py 4489→152 + 6 mixin); 1c 表驱动 dispatch 已交付。
  server.py 主体仍 ~3000 行 (生命周期/中间件), 进一步拆分收益递减, 暂缓。
- **Round 2**: ✅ 2a circuit breaker 接入; ✅ 2b metering 接入;
  ✅ 2c 安全守卫接入 daemon+MCP (CONFIRM_REQUIRED); ✅ 2d /v1/integrations。
- **Round 3**: ✅ 3a app_config 集中全部 os.getenv。3b JSON 日志 / 3c
  Prometheus 扩展 / 3d health 子检查 — 未启动 (现 /metrics 已有手写 registry,
  /health 已有 context; 待后续)。
- **Round 4**: MCP 工具 ~75。usage/lease/handoff/drain 等 MCP 包装未启动。
- **Round 5**: ✅ Makefile + ruff (src/ 0 errors) + pytest markers + CI lint;
  ✅ docs/ 站 15 篇 + README 83KB→21KB。
- **额外审计修复**: `_safe_resolve_path` 潜伏 NameError、死代码清理、
  文档漂移修复 (幻影 env 变量)。

---

## 审计发现总结

### 核心问题

| # | 类别 | 问题 | 严重度 |
|---|------|------|--------|
| P1 | 巨型文件 | `server.py` (4388行/148函数) + `controller.py` (4489行/133函数) = 项目45%代码在两个文件 | 🔴 高 |
| P2 | 休眠模块 | `circuit_breaker.py` 从未接入 daemon 请求流 | 🔴 高 |
| P3 | 休眠模块 | `metering.py` 完全未接入，PriceTable/UsageEvent 定义了但没调用 | 🔴 高 |
| P4 | 孤岛模块 | `integrations/` (langchain/autogen/aider) 无任何入口接入 | 🟡 中 |
| P5 | 配置分散 | 40+ 处 `os.getenv` 散布在 12 个文件中，无集中配置管理 | 🟡 中 |
| P6 | 日志不一致 | 中英混杂、level 使用混乱 (warning 用于 info)、无 structured logging | 🟡 中 |
| P7 | API 缺口 | 部分 daemon 端点无 MCP 工具对应；部分功能仅 CLI 可访问 | 🟡 中 |
| P8 | 文档膨胀 | README.md 83KB 单一文件，缺 API 参考手册、开发指南 | 🟡 中 |
| P9 | 类型覆盖 | `controller.py` 大量 `Any` 返回类型；多个函数缺少返回类型注解 | 🟡 中 |
| P10 | 测试缺口 | 熔断器/计量/集成适配器无测试；daemon lease/handoff 无集成测试 | 🟡 中 |

### "表面接入"问题清单

这些模块导入了但实际未触发生效：

1. **`daemon/circuit_breaker.py`** — `CircuitBreaker` / `CircuitBreakerRegistry` 在 server.py 中未 import 未使用。熔断逻辑完全未接入 daemon 请求流。
2. **`daemon/metering.py`** — `MeteringStore` / `PriceTable` / `UsageEvent` 在 server.py/mcp_server/engine 中无任何引用。LLM 调用产生的 token 消耗没有进入计量系统。
3. **`integrations/langchain_adapter.py`** — 定义 `LangChainTool` 但有 0 引用 (daemon/CLI/MCP 都不入口)
4. **`integrations/autogen_adapter.py`** — 同上
5. **`integrations/aider_adapter.py`** — 同上
6. **`safety/guard.py`** — `check_action` 只在 `agent/loop.py:_run_loop` 中调用。daemon 的 `_click`/`_type` 等直接操作不经过安全守卫。

---

## Round 1: 架构重构 — 拆分巨型文件

### 目标
将 `server.py` 和 `controller.py` 拆分为职责单一的模块。

### 1a: 拆分 daemon/server.py → 多文件

```
daemon/
  server.py          → 精简到 ~800 行 (HTTP server 骨架 + 生命周期)
  routers/            ← 新建
    __init__.py
    browser.py        ← open/click/type/scroll/screenshot 等浏览器操作
    security.py       ← T40-T44 安全审计工具 (security-headers/dns-records/xss-sinks 等)
    sessions.py       ← session CRUD + lease + reattach + handoff
    query.py          ← /v1/query + /v1/query/stream + query stats
    agent.py          ← /agent/run + /agent/run/stream
    discover.py       ← /discover + /discover/stream
    admin.py          ← /admin/* + /healthz + /readyz
    events.py         ← /events SSE 流
  _handler.py         ← _handle 方法 (JSON 解析 + 路由分发)
  _metrics.py         ← _MetricsRegistry (从 server.py 提取)
  _middleware.py      ← op_lock + degradation + drain 检查 (从 _handle 提取)
```

**拆分原则**:
- 每个 router 文件 < 300 行
- router 函数接收 `(daemon, args, session)` 返回 `dict`
- `_dispatch` 改为表驱动: `{("POST", "/open"): router_browser.handle_open, ...}`
- 不改 API 契约，不改测试

### 1b: 拆分 browser/controller.py → 多文件

```
browser/
  controller.py      → 精简到 ~1500 行 (核心浏览器操作)
  security_tools.py   ← T40-T44 安全审计工具提取 (~1200 行)
  interact.py         ← click/type/scroll/drag/hover/heal-click (~500 行)
  navigation.py       ← open/back/forward/reload/tabs (~400 行)
  debug.py            ← console/network/errors/websocket 监听 (~300 行)
  headers.py          ← CSP/HSTS/permissions-policy 解析 (~200 行)
  _utils.py           ← _redact_url_secrets 等共用工具
```

**拆分原则**:
- 不改 `BrowserController` 公开 API — 用 mixin 或 composition 向内部分发
- security_tools 中每个工具函数提取为静态方法或普通函数
- 保持向后兼容 (别的模块 `ctrl.xxx()` 调用不破)

### 1c: 路由表驱动 dispatch

```python
# server.py _dispatch 改为:
_ROUTES: dict[tuple[str, str], Callable] = {}

def _route(method: str, path: str):
    def decorator(fn):
        _ROUTES[(method, path)] = fn
        return fn
    return decorator

@_route("POST", "/open")
async def _open_handler(daemon, args, req): ...

def _dispatch(self, method, path, args, req):
    handler = _ROUTES.get((method, path))
    if handler is None:
        raise ValueError(f"unknown route: {method} {path}")
    return handler(self, args, req)
```

**验收标准**:
- `server.py` < 1000 行
- `controller.py` < 2000 行
- 全部 1,127 个测试通过
- daemon 启动正常，所有端点可访问

---

## Round 2: 激活休眠模块

### 目标
将已实现但未接入的模块接入主流程。

### 2a: 接入 CircuitBreaker

```python
# server.py __init__ 中:
from semantic_browser.daemon.circuit_breaker import CircuitBreakerRegistry
self.circuit_breaker = CircuitBreakerRegistry()

# _handle 中的中间件层:
def _check_circuit(self, path: str) -> None:
    """每次请求前检查熔断器。"""
    site = self._extract_site_from_path(path)
    if not self.circuit_breaker.check(site):
        raise _DegradationError("SERVICE_UNAVAILABLE", "circuit breaker open", 4)
```

**接入点**:
- 浏览器导航失败 (`NETWORK_FAIL`) → `record_failure(target_domain)`
- 浏览器导航成功 → `record_success(target_domain)`
- 站点级熔断器 (per-domain)，默认 threshold=5, timeout=30s
- `/capacity` 暴露熔断器状态

### 2b: 接入 Metering

```python
# server.py __init__ 中:
from semantic_browser.daemon.metering import MeteringStore, PriceTable
self.metering = MeteringStore("~/.semantic-browser/metering.db")
self.price_table = PriceTable()

# LLM 调用后记录:
def _record_llm_usage(self, model: str, input_tokens: int, output_tokens: int,
                      run_id: str = "", session_id: str = ""):
    cost, estimated = self.price_table.calculate_cost(model, input_tokens, output_tokens)
    event = UsageEvent(
        event_id=ulid_new(), kind="llm_usage", source="internal", ts=time.time(),
        tenant_id=..., agent_id=..., run_id=run_id, session_id=session_id,
        provider=..., model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_micro_usd=cost, estimated=estimated,
    )
    self.metering.ingest(event)
```

**接入点**:
- `SemanticQuery._call_llm` 后记录 token 用量
- `LLMEnhancedClassifier.classify` 后记录
- `GoalAgent._ask_llm` 后记录
- `SnapshotEngine.vision` 后记录 (多模态 token)
- `/v1/usage` 和 `/v1/usage/summary` 端点暴露

### 2c: 接入安全守卫到 daemon 操作

```python
# daemon/router_browser.py 中:
async def handle_click(daemon, args, session):
    ref = args["ref"]
    ref_label = await _get_ref_label(daemon, ref, session)  # 从 snapshot 拿 label
    check = check_action("click", args, ref_label)
    if check.needs_confirm and not args.get("confirm_destructive"):
        return err("CONFIRM_REQUIRED", check.reason, retryable=False)
    return daemon.owner.run(daemon._click(ref, session))
```

**接入点**:
- `/click` → 检查 ref label 是否含 delete/remove/submit
- `/type` → 检查 text 是否含 destructive keyword
- `/drag` → 检查 to_ref 是否危险

### 2d: 暴露集成适配器入口

```python
# daemon/router_integrations.py (新建):
- POST /v1/integrations/langchain/tools → 返 LangChain Tool 列表
- POST /v1/integrations/autogen/tools   → 返 AutoGen Tool 列表
- POST /v1/integrations/aider/tools     → 返 Aider Tool 列表
```

每个适配器的 `get_tools()` 方法返回该框架可用工具定义。daemon 启动时不主动加载，按需调。

**验收标准**:
- 熔断器在连续 5 次导航失败后 OPEN，阻止后续请求
- `/capacity` 显示熔断器状态和计量统计
- 每次 LLM 调用产生计量事件写入 SQLite
- 安全守卫在 daemon 层拦截危险操作
- 集成适配器有 HTTP 入口可调用

---

## Round 3: 配置管理 & 可观测性

### 目标
集中化配置、统一日志、完善 metrics。

### 3a: 集中配置系统

```python
# config.py (新建)
@dataclass
class BrowserConfig:
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    stealth_mode: bool = False
    user_agent: str | None = None

@dataclass
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    m_browsers: int = 6
    k_contexts: int = 16
    session_idle_timeout_s: float = 300.0
    drain_timeout_s: float = 30.0
    watchdog_interval_s: float = 5.0
    sweep_interval_s: float = 60.0
    ssrf_allowlist: frozenset = frozenset()
    allow_data_scheme: bool = False

@dataclass
class LLMConfig:
    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model_cheap: str = ""
    model_medium: str = ""
    model_smart: str = ""

@dataclass
class SemanticQueryConfig:
    default_budget: int = 2000
    max_pages: int = 3
    cache_ttl_s: int = 600
    concurrency: int = 4

@dataclass
class AppConfig:
    browser: BrowserConfig
    daemon: DaemonConfig
    llm: LLMConfig
    query: SemanticQueryConfig

    @classmethod
    def from_env(cls) -> "AppConfig": ...
    @classmethod
    def from_file(cls, path: str) -> "AppConfig": ...
```

**迁移策略**:
- 新增 `app_config.py` (不是 `config.py` — 避免与 Playwright 的 config 冲突)
- 所有模块从集中 config 读值，不再直接 `os.getenv`
- 兼容现有环境变量名 (映射到 config 字段)
- 支持 `~/.semantic-browser/config.yaml` 文件覆盖

### 3b: 统一日志

```python
# logging_config.py (新建)
import logging
import json
import sys

class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志。"""
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            **({"exc": self.formatException(record.exc_info)} if record.exc_info else {}),
        }, default=str)

def setup_logging(level: str = "INFO", json_mode: bool = False):
    """统一日志初始化。"""
    ...
```

**改动**:
- 所有 `logger.warning` 改为合适的 level
- 所有中文日志改为英文 (更利于 grep/日志聚合)
- 生产模式默认 JSON 格式
- 敏感数据不记入日志

### 3c: 完善 Prometheus Metrics

**新增指标**:
```
tb_llm_requests_total{provider, model, tier}       # LLM 调用计数
tb_llm_tokens_total{provider, model, direction}    # LLM token 用量
tb_llm_cost_micro_usd_total{provider, model}       # LLM 成本
tb_browser_navigation_total{status}                # 浏览器导航计数
tb_circuit_breaker_state{name}                     # 熔断器状态 (0/1)
tb_session_active                                  # 活跃 session 数 (gauge)
tb_query_cache_hits_total                          # query cache 命中
tb_storage_snapshots_total{trigger}                # 快照计数
```

### 3d: Health check 增强

```python
# GET /health → 增加:
{
  "checks": {
    "browser": {"status": "ok", "pid": 12345, "uptime_s": 3600},
    "circuit_breaker": {"status": "ok", "open_circuits": 0},
    "llm": {"status": "ok", "provider": "openai"},
    "database": {"status": "ok", "size_bytes": 1024000},
    "disk": {"status": "ok", "free_bytes": 50000000000}
  },
  "degradation": {"level": 0, "label": "L0_healthy"}
}
```

**验收标准**:
- 所有 `os.getenv` 调用迁至 `app_config.py` 的 `from_env()` 工厂方法
- 日志统一为英文，生产模式 JSON 格式
- `/metrics` 包含全部新指标
- `/health` 带子检查项

---

## Round 4: API 一致性 & MCP/CLI 补全

### 目标
所有功能在 daemon/MCP/CLI 三层完整可用。

### 4a: MCP 工具补全

**当前 MCP 工具数**: ~65 个
**缺失的工具** (daemon 有但 MCP 无):

| daemon 端点 | 建议 MCP 工具 |
|------------|-------------|
| `POST /v1/query` | ✅ 已有 `sb_query` |
| `POST /v1/query/stream` | 新增 `sb_query_stream` |
| `GET /events` | 新增 `sb_events_subscribe` |
| `POST /sessions/{id}/lease` | 新增 `sb_session_lease` |
| `POST /sessions/{id}/reattach` | 新增 `sb_session_reattach` |
| `POST /sessions/{id}/handoff` | 新增 `sb_session_handoff` |
| `POST /sessions/{id}/handoff/accept` | 新增 `sb_session_handoff_accept` |
| `GET /sessions/{id}/storage_state` | 新增 `sb_session_storage_state` |
| `POST /admin/degrade` | 新增 `sb_admin_degrade` |
| `POST /admin/restore` | 新增 `sb_admin_restore` |
| `POST /admin/drain` | 新增 `sb_admin_drain` |
| `POST /admin/drain/cancel` | 新增 `sb_admin_drain_cancel` |
| `GET /v1/usage` | 新增 `sb_usage_report` |
| `POST /run-workflow` | 新增 `sb_run_workflow` |

### 4b: CLI 命令补全

**当前 CLI 覆盖**: 基本完整
**缺失**:
- `tb session lease/session handoff/session storage-state` — 多 agent 原语
- `tb usage` — 用量查询
- `tb drain/drain-cancel` — 排水管理
- `tb circuit-breaker status/reset` — 熔断器运维

### 4c: API 版本化

- `/v1/*` namespace 覆盖剩余路由 (handoff/reattach/storage_state/usage)
- 老路径 (`/open`, `/click`, ...) 保持兼容但标记为 deprecated
- 在响应头中加 `Deprecation: true` 和 `Sunset: <date>`

**验收标准**:
- MCP 工具数 ≥ 75 个 (新增 ≥ 10 个)
- CLI 所有新命令可运行
- `/v1/*` namespace 100% 覆盖所有 daemon 路由

---

## Round 5: 文档 & 开发者体验

### 目标
从"一个 README"进化到"文档站"级别。

### 5a: 文档拆分

```
docs/
  index.md             ← 项目概述 + 快速开始 (~500 字，从 README 浓缩)
  installation.md      ← 安装指南
  quickstart.md        ← 5 分钟快速开始
  api/
    browser.md         ← 浏览器操作 API
    security.md        ← 安全审计工具 API
    sessions.md        ← Session + Lease + Handoff API
    query.md           ← SemanticQuery API
    agent.md           ← GoalAgent API
    admin.md           ← Admin 运维 API
    mcp.md             ← MCP 工具清单
    events.md          ← SSE 事件协议
  concepts/
    architecture.md    ← 架构设计 (从 README 提炼)
    token-economy.md   ← Token 经济模型
    security-model.md  ← 安全模型 (SSRF/Guard/Stealth)
    multi-agent.md     ← 多 Agent 共享 daemon
  guides/
    production.md      ← 生产部署 (从 examples/production_deploy.md 移到 docs/)
    troubleshooting.md ← 常见问题
    contributing.md    ← 参与贡献
  reference/
    config.md          ← 完整配置参考
    error-codes.md     ← 错误码大全
    env-vars.md        ← 环境变量大全
    changelog.md       ← symlink to CHANGELOG.md
```

### 5b: README 精简

当前 README 83KB。精简到 ~10KB:
- 保留: 核心理念、快速开始、架构图、模块表
- 移除: T40-T68 详细设计章节 (移到 docs/concepts/architecture.md)
- 移除: 所有 CHANGELOG 内容 (保留链接)
- 添加: docs/ 索引

### 5c: 开发者工具

```bash
# Makefile 或 Taskfile
make install        # pip install -e . + playwright install
make test           # pytest -q
make test-unit      # pytest tests/test_core.py tests/test_ssrf.py ...
make test-integration  # pytest tests/test_daemon.py tests/test_llm_e2e.py ...
make lint           # ruff check src/
make typecheck      # mypy src/
make docs           # mdbook serve docs/ (or mkdocs)
make dist           # python -m build
```

### 5d: 代码质量工具接入

```toml
# pyproject.toml 增加:
[tool.ruff]
target-version = "py310"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "SIM", "C4"]

[tool.mypy]
python_version = "3.10"
strict = false
warn_return_any = true
warn_unused_configs = true

[tool.pytest.ini_options]
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "integration: marks tests as integration tests",
    "llm: marks tests that need LLM API key",
    "browser: marks tests that need browser",
]
```

**验收标准**:
- docs/ 有 ≥ 10 个 .md 文件
- README < 15KB
- `make test-unit` 在 30s 内完成
- ruff 0 错误 (或明确记录例外)
- 测试用 `-m "not slow and not llm"` 可快速过滤

---

## 实施顺序与依赖

```
Round 1 (重构) ──→ Round 2 (激活) ──→ Round 3 (配置) ──→ Round 4 (API) ──→ Round 5 (文档)
      │                   │                   │                  │                 │
      无依赖             依赖 R1             依赖 R1            依赖 R2+R3       依赖 R1-R4
      (纯内部拆分)       (模块已拆分)         (config 集中)      (功能已激活)      (功能稳定)
```

每轮完成后的审计检查清单:
- [ ] 全部测试通过
- [ ] daemon 可正常启动
- [ ] 无新增 `except Exception: pass`
- [ ] 无新增 `os.getenv` (应走 app_config)
- [ ] CHANGELOG 记录本轮变更
- [ ] 本轮验收标准全部达成

---

## 预期效果

| 指标 | 当前 | 目标 |
|------|------|------|
| `server.py` 行数 | 4,388 | < 1,000 |
| `controller.py` 行数 | 4,489 | < 2,000 |
| 单文件最大行数 | 4,489 | < 800 |
| MCP 工具数 | ~65 | ≥ 75 |
| `os.getenv` 调用点 | 40+ | < 5 (仅在 config.py) |
| 休眠模块 | 5 个 | 0 个 |
| 文档文件数 | 1 (README) | ≥ 12 |
| README 大小 | 83KB | < 15KB |
| test 快速子集 | 60s+ | < 30s |
| ruff lint 错误 | 未运行 | 0 |
