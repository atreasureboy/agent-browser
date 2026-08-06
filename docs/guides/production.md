# 生产部署

完整 K8s manifest（Deployment + PVC + probes + 密钥注入）在
[examples/production_deploy.md](../../examples/production_deploy.md)。

## 要点速览

1. **单副本起步**：daemon 是有状态服务（浏览器实例 + SQLite），
   多副本需要前置路由做 session 粘滞。
2. **probes**：liveness 打 `/healthz`，readiness 打 `/readyz`。
3. **优雅退出**：SIGTERM 触发 drain（默认 30s），K8s
   `terminationGracePeriodSeconds` 要大于 `--drain-timeout`。
4. **密钥**：LLM key 走 Secret → 环境变量注入，不要烧进镜像。
5. **资源**：Chromium 内存按 ~300MB/实例预留，`--m-browsers`
   与 Pod memory limit 匹配。
6. **观测**：`/metrics`（Prometheus 文本格式）、`/capacity`（降级/
   熔断/计量汇总）、`/events`（SSE）。
7. **SSRF**：生产不要随意加 `--ssrf-allowlist`；确需访问内网服务
   时按最小 host 通配放行。

## 运维端点

```bash
curl -X POST localhost:8765/admin/drain          # 排空
curl -X POST localhost:8765/admin/drain/cancel   # 取消排空
curl -X POST localhost:8765/admin/degrade        # 手动降级
curl -X POST localhost:8765/admin/restore        # 恢复
curl -s localhost:8765/capacity                  # 总览
```
