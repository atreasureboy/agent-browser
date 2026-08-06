# 安全审计工具 API（T40 — T44）

daemon 暴露 30+ 安全审计端点（`GET`，除特殊说明外都需先 `/open` 一个页面）。
完整设计演进见 [../design-log.md](../design-log.md)。

## 存储与探针

| 端点 | 说明 |
|------|------|
| `/storage` | 客户端存储探针（local/session/cookies） |
| `/probe-paths` | 隐藏路径探针（well_known/discovery/admin，可 `categories=`） |
| `/probe-http-methods` | HTTP 方法嗅探 |

## 头部与协议

| 端点 | 说明 |
|------|------|
| `/security-headers` | CSP/HSTS/XFO 等结构化 + CORS 风险评估 |
| `/parse-csp` | CSP 逐指令解析 |
| `/dns-records` | DNS 记录 |
| `/tls-subdomains` | 从 TLS SAN 抽子域名 |
| `/wayback-urls` | Wayback Machine 历史 URL |

## 内容与脚本

| 端点 | 说明 |
|------|------|
| `/extract-api-endpoints` | 从 JS 提取 API endpoints（fetch/axios/XHR） |
| `/extract-js-libraries` | JS 库指纹 |
| `/extract-secrets-from-js` | JS 中的疑似密钥 |
| `/detect-graphql` | GraphQL 端点探测 |
| `/decode-jwts` | JWT 解码 |
| `/detect-waf` | WAF 识别 |
| `/fingerprint-tech` | 技术栈指纹 |

## 漏洞探测

| 端点 | 说明 |
|------|------|
| `/find-xss-sinks` | XSS 注入点 |
| `/find-open-redirect-sinks` | 开放重定向 |
| `/find-idor-urls` | IDOR 可疑 URL |
| `/check-csrf-coverage` | CSRF 覆盖 |
| `/detect-auth-methods` / `/detect-2fa` | 认证方式 / 2FA |
| `/find-disclosure` / `/analyze-exposed-files` | 信息泄露 / 暴露文件 |
| `/discover-api-specs` | OpenAPI/Swagger 发现 |
| `/check-subdomain-takeover` | 子域名接管 |
| `/find-cloud-resources` | 云资源泄露（S3/Blob/GCS…） |
| `/enumerate-subdomains` | 子域名枚举 |
| `/inventory-external-resources` | 外部资源清单 |

## 可访问性

| 端点 | 说明 |
|------|------|
| `/a11y-audit` | axe-core 集成无障碍审计 |

## 说明

- 所有 URL 参数都过 SSRF 闸（私网/云元数据/内部 TLD 被拒）。
- MCP 端对应 `sb_*` 工具；`tb` CLI 提供 `security-headers` / `probe-paths` 等子命令。
- 探测类工具仅做被动/低风险检查，不做主动攻击。
