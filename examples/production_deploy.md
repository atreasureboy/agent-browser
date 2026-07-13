# 生产部署指南 — SemanticQuery + daemon

> 怎么把 SemanticQuery 部署到生产 (多 agent 共享 daemon 场景).

## 1. 启动 daemon

```bash
# 端口 8765 默认; cache 持久化到 /var/lib/sb/query_cache.json
tb daemon start --port 8765 --background

# 验证 daemon 起来了
curl localhost:8765/healthz
# → {"alive": true, "pid": 1234, ...}
```

可调 config:
- `--m-browsers N` — 浏览器实例数 (默认 6, 与 max_pages 隔离)
- `--k-contexts N` — 每浏览器 context 数 (默认 16, 默认 16 个 session)
- `--watchdog-interval 5` — 心跳间隔秒
- `--ssrf-allowlist "*.example.com,internal.dev"` — 自托管 fixture
- `--allow-data-scheme` — 测试 fixture (生产应关)

## 2. 持久 cache 配置

daemon 默认 cache 持久到 `~/.semantic-browser/query_cache.json`:
- TTL 默认 600s (10 分钟)
- 容量 64 entries, LRU 淘汰
- 30 天过期自动清理

清理 cache (e.g. 内容大改后):
```bash
curl -X POST localhost:8765/v1/query/cache/clear
# → {"ok": true, "data": {"cleared": N, "remaining": 0}}
```

监控 cache 命中率:
```bash
curl localhost:8765/v1/query/stats
# → cache: {hits, misses, calls, size, hit_rate}
# → concurrency: {concurrency_limit, available_now}
```

SLA 参考 (实测):
- 高频重复 query (dashboard 类): hit_rate 应 ≥ 80%
- 多样化 query (research 类): hit_rate 30-60% 都正常

## 3. 并发限制

daemon 默认 `query_concurrency=4`, 跟 `_SEMAPHORE` 对应:
- 同 in-flight query 数 ≤ 4
- 第 5 个会等 (默认 30s 超时 `op_lock`)
- 调高 --query-concurrency=N 增加吞吐 (前提是 chromium 资源够)

## 4. SSRF 保护

daemon 自动挡:
- `file://`, `chrome://`, `javascript:`, `data:` (除非 --allow-data-scheme)
- 私网 (RFC1918 / loopback / link-local / CGNAT / cloud metadata IP)
- `*.internal`, `*.local`, `metadata.google.internal`

测试 fixture 用 `--ssrf-allowlist` 绕开。

## 5. 监控 + 告警

关键 endpoints:
- `GET /health` — daemon 状态 (drain / uptime / 当前页 URL)
- `GET /healthz` — k8s liveness (永远 200, 除非进程挂了)
- `GET /readyz` — k8s readiness (drain 时 503)
- `GET /capacity` — 容量 + 降级状态 (auto-degrade 等级)
- `GET /v1/query/stats` — query cache + LLM 统计
- `GET /metrics` — Prometheus 格式

Prometheus scrape config:
```yaml
scrape_configs:
  - job_name: semantic_browser
    static_configs:
      - targets: ['localhost:8765']
    metrics_path: /metrics
```

## 6. 部署示例 — K8s

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: semantic-browser
spec:
  replicas: 1  # 单一 daemon (cache 在内存; 多副本各自一份)
  template:
    spec:
      containers:
      - name: sb-daemon
        image: your-registry/semantic-browser:latest
        command: ["tb", "daemon", "start", "--port", "8765", "--background=false", "--foreground"]
        ports:
        - containerPort: 8765
        env:
        - {name: ANTHROPIC_AUTH_TOKEN, valueFrom: {secretKeyRef: {name: llm-secrets, key: token}}}
        - {name: ANTHROPIC_BASE_URL, value: "https://api.minimax.io/anthropic"}
        - {name: ANTHROPIC_MODEL, value: "MiniMax-M3"}
        volumeMounts:
        - {name: cache, mountPath: /root/.semantic-browser}
        livenessProbe:
          httpGet: {path: /healthz, port: 8765}
        readinessProbe:
          httpGet: {path: /readyz, port: 8765}
          periodSeconds: 10
          failureThreshold: 3
      volumes:
      - {name: cache, persistentVolumeClaim: {claimName: sb-cache-pvc}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  {name: sb-cache-pvc}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 1Gi}}
```

注: 多副本时 cache 不共享 (各副本独立). 高可用需要 sticky session 或额外 cache 层.

## 7. 监控脚本示例

```python
# examples/monitoring.py
import httpx

async def watch():
    async with httpx.AsyncClient() as cli:
        for _ in range(60):  # 5 分钟查一次
            s = (await cli.get("http://daemon:8765/v1/query/stats")).json()["data"]
            cache = s["cache"]
            conc = s["concurrency"]
            print(f"[{cache['calls']} calls, hit_rate={cache['hit_rate']}, "
                  f"concurrent={conc['concurrency_limit']-conc['available_now']}/{conc['concurrency_limit']}]")
            if cache["hit_rate"] and cache["hit_rate"] < 0.2:
                alert("Low cache hit rate!")
            await asyncio.sleep(60)
```

## 8. 错误码 (跨层稳定)

| Code | 含义 | HTTP | agent 处理 |
|---|---|---|---|
| `SSRF_BLOCKED` | URL 黑名单 | 400 | 跳过该 URL |
| `CAPACITY_DEGRADED` | L1 (capacity ≥ 85%) | 503 | backoff + 重试 |
| `DEGRADED_READONLY` | L3+ | 503 | 切到只读 path |
| `SERVICE_UNAVAILABLE` | L4 | 503 | 切到备用 daemon |
| `DAEMON_BUSY` | op_lock 超时 | 503 | sleep + 重试 |
| `DAEMON_DRAINING` | 收到 SIGTERM | 503 + Retry-After | 切到备用 daemon |
| `QUERY_FAILED` | query 内部失败 | 500 | 看 error.message |
| `MISSING_PARAM` | 缺 query | 400 | 补 query |
| `INVALID_PARAM` | 参数类型错 | 400 | 修参数 |

## 9. 安全 checklist

- [x] SSRF guardrail (T58) — 全部 URL 进 daemon 都过 _check_url
- [x] tenant 隔离 (T66.6) — session metadata 重启保留
- [x] lease + fence (T65.7) — 多 agent 防 GC 抢锁
- [x] degradation L0-L4 (T56) — 自动降级保护 chromium
- [x] graceful drain (T62) — SIGTERM 不踢 in-flight
- [x] SSRF bypass 修复 (T66.8) — 6 个 endpoint 路由层补 check
- [x] tenant immutability (T66.8) — body tenant_id 必须跟 sessions_index 一致

## 10. 推荐约定

1. **cache key**: `(query.lower().strip(), start_url)` — 同 query+URL 命中. query 大小写不敏感但保留.
2. **budget**: 默认 2000 tokens. LLM 调用全部走 cheap tier (M3). 大 query 适当调高.
3. **max_pages**: 多页 follow-link 默认 1. 复杂 research 适当调高 (但 ≤ 5, daemon 上限).
4. **plan-only**: 顶层 agent 不知道 URL 时先 `query(goal)` 一次, 拿 plan + sub_questions + keywords, 自己决定 URL 再 `query(goal, start_url=url)`.
5. **cache 清理**: URL 内容大改后 / 升级 M3 后 / 出现 stale answers 时 → `POST /v1/query/cache/clear`.
