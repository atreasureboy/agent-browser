# Semantic Browser 文档

> Agent-readable semantic browser layer — 给 AI Agent 用的 Site Intelligence Layer

## 入门

| 文档 | 内容 |
|------|------|
| [installation.md](installation.md) | 安装（PyPI / 源码）+ Playwright 依赖 |
| [quickstart.md](quickstart.md) | 5 分钟上手：daemon + query + MCP |

## API 参考

| 文档 | 内容 |
|------|------|
| [api/query.md](api/query.md) | SemanticQuery：/v1/query、/v1/query/stream、cache |
| [api/sessions.md](api/sessions.md) | 多 agent session / lease / handoff 原语 |
| [api/security.md](api/security.md) | 30+ 安全审计端点（T40–T44） |
| [api/integrations.md](api/integrations.md) | LangChain / AutoGen / Aider 适配器 |

## 概念

| 文档 | 内容 |
|------|------|
| [concepts/architecture.md](concepts/architecture.md) | 总体架构：engine / daemon / MCP 三层 |
| [concepts/token-economy.md](concepts/token-economy.md) | Token 经济模型与预算控制 |
| [concepts/security-model.md](concepts/security-model.md) | SSRF 闸 / 危险动作守卫 / Stealth |

## 指南

| 文档 | 内容 |
|------|------|
| [guides/production.md](guides/production.md) | 生产部署（K8s manifest） |

## 参考

| 文档 | 内容 |
|------|------|
| [reference/config.md](reference/config.md) | 环境变量 + daemon CLI 参数大全 |
| [reference/error-codes.md](reference/error-codes.md) | 错误码 → HTTP 状态映射 |
| [design-log.md](design-log.md) | T40–T66 逐版本设计笔记（历史决策） |

其他资源：

- [../README.md](../README.md) — 项目总览 + 快速开始
- [../CHANGELOG.md](../CHANGELOG.md) — 版本变更
- [../super_plan.md](../super_plan.md) — 演进计划
- [transparent-browser-architecture.md](transparent-browser-architecture.md) — Transparent Browser 架构愿景
