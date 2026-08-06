# 安全模型

四道防线：SSRF 闸、危险动作守卫、路径闸、Stealth 反检测。

## 1. SSRF 闸（`safety/ssrf.py`）

所有接受 URL 的端点（/open、/response-headers、/script-source、安全审计
工具等）先过 `check_url`：

- 非 http/https scheme（file/chrome/javascript/data）拒绝
  （daemon 可用 `--allow-data-scheme` 放开 data: 供测试）
- 私网/回环/链路本地 IP、云元数据地址（169.254.169.254、
  metadata.google.internal）拒绝
- 内部 TLD（*.internal / *.local / *.localhost）拒绝
- 例外：`--ssrf-allowlist host,*.example.com`

违规返 `SSRF_BLOCKED`（HTTP 400）。

## 2. 危险动作守卫（`safety/guard.py`）

daemon 的 /click、/type、/drag、/fill-form、/keyboard/type、/with-retry
与 MCP sb_click/sb_type 在执行前调 `check_action`：

- type 文本含 delete/drop/rm -rf/truncate/submit… → 拦
- click 目标 label（aria-label/title/innerText，经 `get_ref_label` 取）
  含危险关键词 → 拦
- drag 目标 ref 匹配 trash/recycle/bin → 拦

拦截结果：`CONFIRM_REQUIRED`（HTTP 409，retryable=false）。人类确认后
客户端带 `"confirm_destructive": true` 重发。agent 不应自行绕过。

## 3. 文件路径闸（daemon `_safe_resolve_path`）

任何接受文件系统路径的端点（screenshot/set-files/download/state-save/
run-workflow）强制 resolved 路径落在 `~/.semantic-browser/` 或 cwd 内，
防路径穿越读 `/etc/shadow` 等。违规返 400。

## 4. Stealth 反检测（`safety/stealth.py`）

启动 Chromium 带反指纹参数 + init script 注入一致的 UA/platform/
Client Hints profile，降低 anti-bot 减分。配置见 `BrowserConfig`。

## 错误日志脱敏

`result.py` 的 `classify_exception` 对返给客户端的错误消息做 path/
token/Authorization redact，避免 FileNotFoundError 之类泄露服务器路径
或凭据。

## 相关测试

`tests/test_ssrf.py`（SSRF + 路径闸）、`tests/test_safety_guard.py`
（守卫 e2e）、`tests/test_stealth.py`。
