"""Browser debugging: console, network, errors, WebSocket monitoring, storage inspection."""

from __future__ import annotations

import logging
import time
from typing import Any

from semantic_browser.snapshot.engine import SnapshotEngine

logger = logging.getLogger(__name__)


class _DebugMixin:
    """Console/network/error monitoring, storage, and debugging — mixed into BrowserController."""

    # ── T18: Console / Network / PageError 观察 ─────────────────

    def _on_console(self, msg: Any) -> None:
        """console.log/warn/error/info → 缓存. agent 调试时 dump."""
        try:
            entry = {
                "type": msg.type,
                "text": msg.text,
                "location": str(msg.location) if msg.location else None,
            }
        except Exception:
            entry = {"type": "log", "text": str(msg), "location": None}
        self._console_messages.append(entry)
        self._trim_buffer(self._console_messages)

    def _on_request(self, req: Any) -> None:
        """每个 HTTP 请求开始时记录."""
        try:
            entry = {
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "ts": time.time(),
            }
        except Exception:
            entry = {"method": "?", "url": str(req), "resource_type": "?", "ts": time.time()}
        self._network_requests.append(entry)
        self._trim_buffer(self._network_requests)

    def _on_response(self, resp: Any) -> None:
        """每个响应回填 status, 改最后一条同 url+method 的未完成 request.

        T39: 同时存 response_headers (lowercased keys) — agent 调 get_response_headers 用.
        """
        try:
            url = resp.url
            status = resp.status
            method = resp.request.method if resp.request else None
            # T39: 抓 headers — 安全审计要 CSP/Set-Cookie 等
            try:
                headers_list = resp.headers or []
            except Exception:
                headers_list = []
            # headers 可能 list[tuple] 或 dict, 统一成 dict (lowercase keys)
            headers_dict: dict[str, str] = {}
            if isinstance(headers_list, dict):
                headers_dict = {str(k).lower(): str(v)[:500] for k, v in headers_list.items()}
            elif isinstance(headers_list, list):
                for h in headers_list:
                    try:
                        k, v = h[0], h[1]
                        headers_dict[str(k).lower()] = str(v)[:500]
                    except Exception:
                        continue
            # T117 audit fix: 之前 response_headers 完整存到 _network_requests
            # — Authorization / Proxy-Auth / Set-Cookie 任何 caller 都能通
            # 过 get_network_requests 拉到. 修: 敏感 header mask.
            _REDACTED_HEADERS = {
                "authorization", "proxy-authorization", "x-api-key",
                "x-goog-api-key", "cookie", "set-cookie", "x-csrf-token",
                "x-auth-token", "x-amz-security-token",
            }
            for k in _REDACTED_HEADERS:
                if k in headers_dict:
                    headers_dict[k] = "<redacted>"
        except Exception:
            return
        for entry in reversed(self._network_requests):
            if entry.get("url") == url and entry.get("method") == method and "status" not in entry:
                entry["status"] = status
                entry["response_headers"] = headers_dict
                break

    def _on_request_failed(self, req: Any) -> None:
        """请求失败 (网络/超时/CORS/404 等)."""
        try:
            failure = req.failure
        except Exception:
            failure = "?"
        # 找到最近一条匹配 request 并标记
        for entry in reversed(self._network_requests):
            if (entry.get("url") == req.url
                    and entry.get("method") == req.method
                    and "status" not in entry):
                entry["status"] = -1
                entry["failure"] = str(failure)[:200] if failure else "unknown"
                break

    def _on_web_error(self, err: Any) -> None:
        """未捕获 JS exception (page.on('pageerror'))."""
        try:
            err_obj = err.error
            entry = {
                "name": type(err_obj).__name__ if err_obj else "Error",
                "message": str(err_obj)[:300] if err_obj else "?",
                "page": err.page.url if hasattr(err, "page") and err.page else None,
            }
        except Exception:
            entry = {"name": "Error", "message": str(err)[:300], "page": None}
        self._page_errors.append(entry)
        self._trim_buffer(self._page_errors)

    # ── T40i: WebSocket 观察 ────────────────────────────────

    def _on_websocket(self, ws: Any) -> None:
        """page.on('websocket') — 每个 WS 连接 open 时记录.
        ws.url: wss://... 目标
        ws.on('framesent', ...) / ws.on('framereceived', ...) 可选,
        这里只记录 URL + 时间, 不抓 payload (可能很大/敏感).
        """
        try:
            entry: dict[str, Any] = {
                "url": ws.url,
                "opened_at": time.time(),
                "page": None,
            }
        except Exception:
            entry = {"url": str(ws), "opened_at": time.time(), "page": None}
        self._websocket_connections.append(entry)
        self._trim_buffer(self._websocket_connections)

    def get_websockets(self, limit: int = 100) -> list[dict[str, Any]]:
        """T40i: 返回累积的 WebSocket 连接列表 (新→旧).
        给 agent 看页面建立了哪些 WS 通道 (chat/live/realtime API).
        """
        return list(reversed(self._websocket_connections[-limit:]))

    def _trim_buffer(self, buf: list) -> None:
        """防无限增长; 超过 max 截断到 max (FIFO)."""
        if len(buf) > self._max_event_buffer:
            del buf[: len(buf) - self._max_event_buffer]

    def get_console_messages(
        self, type_filter: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """返回最近的 console 消息. type_filter: 'log'/'warn'/'error'/'info'/'debug'."""
        out = self._console_messages
        if type_filter:
            out = [m for m in out if m.get("type") == type_filter]
        return out[-limit:]

    def get_network_requests(
        self,
        *,
        only_failed: bool = False,
        method: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """返回最近的 network 请求. only_failed: 只看 status=-1 或 4xx/5xx."""
        out = self._network_requests
        if method:
            out = [r for r in out if r.get("method", "").upper() == method.upper()]
        if only_failed:
            out = [r for r in out if r.get("status", 0) < 0 or r.get("status", 0) >= 400]
        return out[-limit:]

    def get_page_errors(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回未捕获 JS 异常."""
        return self._page_errors[-limit:]

    def clear_event_buffer(self) -> None:
        """清空所有事件缓冲 (导航到新页时常用)."""
        self._console_messages.clear()
        self._network_requests.clear()
        self._page_errors.clear()
        self._websocket_connections.clear()  # T40i

    # ── T17: Cookie / Storage 管理 ───────────────────────────

    async def get_cookies(self, url: str | None = None) -> list[dict[str, Any]]:
        """列出 cookies. url=None = 所有 context cookies.

        Returns [{"name", "value", "domain", "path", "expires", "httpOnly", "secure"}, ...]
        """
        await self._ensure_page()
        # Playwright cookies API: 用 context 而不是 page
        cookies = await self._context.cookies(url) if url else await self._context.cookies()
        return [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "expires": c.get("expires", -1),
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", False),
                "sameSite": c.get("sameSite"),
            }
            for c in cookies
        ]

    async def set_cookie(
        self,
        name: str,
        value: str,
        url: str | None = None,
        domain: str | None = None,
        path: str = "/",
    ) -> dict[str, Any]:
        """设置一个 cookie.

        url 优先; 若没给 url, 用 domain+path.
        返回 {ok, name, error}.
        """
        try:
            cookie: dict[str, Any] = {"name": name, "value": value, "path": path}
            if url:
                cookie["url"] = url
            else:
                cookie["domain"] = domain or ""
                cookie["path"] = path
            await self._context.add_cookies([cookie])
            return {"ok": True, "name": name, "error": None}
        except Exception as e:
            return {"ok": False, "name": name, "error": str(e)[:200]}

    async def delete_cookie(self, name: str, url: str | None = None) -> dict[str, Any]:
        """删一个 cookie. url=None = 清空所有同名 cookie."""
        try:
            await self._context.clear_cookies(name=name, url=url)
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "name": name, "error": str(e)[:200]}

    async def clear_cookies(self) -> int:
        """清空所有 cookies. 返回清理的 cookie 数."""
        before = len(await self.get_cookies())
        await self._context.clear_cookies()
        return before

    async def read_storage(self, kind: str = "local") -> dict[str, str]:
        """读 localStorage / sessionStorage. kind: 'local' or 'session'.

        Returns {key: value} (value 是 str; 复杂类型可能需要 agent 自己 parse).
        注: T40a 完整版 (含 cookies) 用 get_storage() (无 kind 参数).
        """
        target = await self._active_page_or_frame()
        storage_kind = "localStorage" if kind == "local" else "sessionStorage"
        # JS 在 frame 内跑 (iframe 也支持)
        result = await target.evaluate(f"""
            () => {{
                const out = {{}};
                const storage = {storage_kind};
                for (let i = 0; i < storage.length; i++) {{
                    const k = storage.key(i);
                    out[k] = storage.getItem(k);
                }}
                return out;
            }}
        """)
        return result or {}

    async def set_storage(self, key: str, value: str, kind: str = "local") -> dict[str, Any]:
        """写 localStorage / sessionStorage."""
        target = await self._active_page_or_frame()
        storage_kind = "localStorage" if kind == "local" else "sessionStorage"
        try:
            await target.evaluate(
                f"([k, v]) => {storage_kind}.setItem(k, v)", [key, value],
            )
            return {"ok": True, "kind": kind, "key": key, "error": None}
        except Exception as e:
            return {"ok": False, "kind": kind, "key": key, "error": str(e)[:200]}

    async def clear_storage(self, kind: str = "local") -> dict[str, Any]:
        """清空 localStorage 或 sessionStorage. kind: 'local' / 'session' / 'all'."""
        target = await self._active_page_or_frame()
        storage_kind = "localStorage" if kind == "local" else "sessionStorage"
        try:
            if kind == "all":
                await target.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            else:
                await target.evaluate(f"() => {storage_kind}.clear()")
            return {"ok": True, "kind": kind, "error": None}
        except Exception as e:
            return {"ok": False, "kind": kind, "error": str(e)[:200]}

    async def get_storage(self) -> dict[str, Any]:
        """T40a: 客户端存储探针 — localStorage/sessionStorage 全文 + cookies 字段.

        Returns:
            {
              "localStorage":   {k: v (5000 字截断)},
              "sessionStorage": {k: v},
              "cookies": [{
                  "name", "value"(500 字), "domain", "path", "expires"(unix ts or None),
                  "httpOnly" (bool), "secure" (bool), "sameSite" (str), "url",
              }],
              "cookie_count": int,
              "page_url": str,
            }
        """
        page = await self._ensure_page()
        stores = await page.evaluate("""() => {
            const dump = (storage) => {
                const out = {};
                if (!storage) return out;
                for (let i = 0; i < storage.length; i++) {
                    const k = storage.key(i);
                    if (k == null) continue;
                    out[k] = (storage.getItem(k) || '').substring(0, 5000);
                }
                return out;
            };
            return {
                localStorage: dump(window.localStorage),
                sessionStorage: dump(window.sessionStorage),
                page_url: location.href,
            };
        }""")
        # cookies via Playwright context (gives typed fields)
        cookies: list[dict[str, Any]] = []
        try:
            raw_cookies = await self._context.cookies()
            for c in raw_cookies:
                cookies.append({
                    "name": c.get("name", ""),
                    "value": (c.get("value") or "")[:500],
                    "domain": c.get("domain", ""),
                    "path": c.get("path", ""),
                    "expires": c.get("expires"),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "sameSite": c.get("sameSite", "") or "",
                    "url": c.get("url", ""),
                })
        except Exception as e:
            logger.warning("get cookies failed: %s", e)
        return {
            "localStorage": stores.get("localStorage", {}),
            "sessionStorage": stores.get("sessionStorage", {}),
            "cookies": cookies,
            "cookie_count": len(cookies),
            "page_url": stores.get("page_url", page.url),
        }

    async def get_response_headers(self, url: str) -> dict[str, str] | None:
        """T39: 给定 URL, 拿最近一次响应的 HTTP headers (从 _network_requests 缓冲里查).

        Returns: header 字典 (lowercased keys), 或 None (没找到).
        用于查 CSP / HSTS / Set-Cookie / X-Frame-Options 等安全相关 header.
        """
        # 优先查完全匹配, 其次 path 匹配
        for req in reversed(self._network_requests):
            if req.get("url") == url and req.get("response_headers"):
                return req["response_headers"]
        # 兜底: path 匹配 (允许只给 path, 拼上当前 origin)
        from urllib.parse import urljoin
        page = self.current_page
        if page is not None:
            full = urljoin(page.url, url)
            for req in reversed(self._network_requests):
                if req.get("url") == full and req.get("response_headers"):
                    return req["response_headers"]
        # 兜底2: 用户给了 URL 但还没 open 过 — 用 httpx 直接 GET 拿头 (不跑 body)
        if url.startswith(("http://", "https://")):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    r = await client.head(url, headers={"User-Agent": "semantic-browser/0.1"})
                    if r.status_code < 400:
                        return {k.lower(): v for k, v in r.headers.items()}
            except Exception:
                pass
        return None

    async def get_dom_diff(self, before_refs: set[str]) -> dict[str, Any]:
        """T39: 比较当前 snapshot 的 ref 集合和 before_refs, 报告 diff.

        Agent 用来判断"我点击之后, 页面发生了什么":
        - disappeared: 之前在现在不在的 ref (页面被替换/navigate)
        - appeared:    之前不在现在在的 ref (新内容加载)
        - url_changed: 当前 URL vs 之前 URL

        Returns: {"appeared": [...], "disappeared": [...], "url_changed": bool,
                  "current_url": str}
        """
        page = self.current_page
        if page is None:
            return {"appeared": [], "disappeared": list(before_refs),
                    "url_changed": False, "current_url": ""}
        current_url = page.url
        try:
            engine = SnapshotEngine(page)
            snap = await engine.capture(base_url=current_url)
        except Exception:
            return {"appeared": [], "disappeared": list(before_refs),
                    "url_changed": False, "current_url": current_url}
        current_refs = {c.ref for c in snap.controls} | {l.ref for l in snap.links}
        return {
            "appeared": sorted(current_refs - before_refs),
            "disappeared": sorted(before_refs - current_refs),
            "url_changed": False,  # 没记录 before URL, 这里只能给当前
            "current_url": current_url,
        }
