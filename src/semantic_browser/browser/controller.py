"""
Browser Controller — Playwright 封装层。

真实浏览器控制：open / back / forward / reload / scroll / wait / screenshot。
不做复杂逻辑，只保证稳定可靠。
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse

from semantic_browser.snapshot.engine import SnapshotEngine
from playwright.async_api import (

    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)


@dataclass
class BrowserConfig:
    """浏览器配置。"""

    headless: bool = True
    viewport: dict[str, int] = field(default_factory=lambda: {"width": 1280, "height": 720})
    user_agent: Optional[str] = None
    timeout: int = 30000  # 毫秒
    locale: str = "en-US"
    storage_state_path: Optional[str] = None


# ── T40f: 安全头 parser 帮手 ─────────────────────────────

def _parse_csp(csp: str) -> dict[str, Any]:
    """Parse CSP header — 拆 directives, 标常见不安全 source."""
    directives: dict[str, list[str]] = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(None, 1)
        name = bits[0].lower()
        sources = bits[1].split() if len(bits) > 1 else []
        directives[name] = sources
    all_srcs = [s for vs in directives.values() for s in vs]
    return {
        "raw": csp,
        "directives": directives,
        "directive_names": list(directives.keys()),
        "has_unsafe_inline": "'unsafe-inline'" in all_srcs,
        "has_unsafe_eval": "'unsafe-eval'" in all_srcs,
        "allows_wildcard": "*" in all_srcs,
        "allows_data": "data:" in all_srcs,
        "allows_https": "https:" in all_srcs,
        "has_script_src": "script-src" in directives,
        "has_object_src": "object-src" in directives,
        "has_default_src": "default-src" in directives,
    }


def _parse_hsts(hsts: str) -> dict[str, Any]:
    """Strict-Transport-Security."""
    out = {"raw": hsts, "max_age": 0, "include_subdomains": False, "preload": False}
    for tok in hsts.split(";"):
        tok = tok.strip()
        if tok.lower().startswith("max-age="):
            try:
                out["max_age"] = int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif tok.lower() == "includesubdomains":
            out["include_subdomains"] = True
        elif tok.lower() == "preload":
            out["preload"] = True
    return out


def _parse_permissions_policy(pp: str) -> dict[str, Any]:
    """Permissions-Policy 解析成 {directive: allowed-origins 或 []}."""
    directives: dict[str, list[str]] = {}
    for part in pp.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("=", 1)
        name = bits[0].strip().lower()
        sources: list[str] = []
        if len(bits) > 1:
            sources = bits[1].split()
        directives[name] = sources
    return {"raw": pp, "directives": directives}


def _parse_set_cookie(sc_value: str) -> dict[str, Any]:
    """解析单个 Set-Cookie 字符串."""
    parts = sc_value.split(";")
    first = parts[0].strip()
    name = ""
    value = ""
    if "=" in first:
        name, value = first.split("=", 1)
        name = name.strip()
        value = value.strip()
    out: dict[str, Any] = {
        "name": name,
        "value": value[:500],
        "httpOnly": False,
        "secure": False,
        "sameSite": "",
        "path": "",
        "domain": "",
        "max_age": None,
        "expires": "",
    }
    for tok in parts[1:]:
        tok = tok.strip()
        low = tok.lower()
        if low == "httponly":
            out["httpOnly"] = True
        elif low == "secure":
            out["secure"] = True
        elif low.startswith("samesite="):
            out["sameSite"] = tok.split("=", 1)[1]
        elif low.startswith("path="):
            out["path"] = tok.split("=", 1)[1]
        elif low.startswith("domain="):
            out["domain"] = tok.split("=", 1)[1]
        elif low.startswith("max-age="):
            try:
                out["max_age"] = int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif low.startswith("expires="):
            out["expires"] = tok.split("=", 1)[1]
    return out


class BrowserController:
    """
    Playwright 异步封装。

    用法:
        controller = BrowserController()
        await controller.start()
        page = await controller.open("https://example.com")
        snapshot = await controller.get_aria_snapshot()
        await controller.close()
    """

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._active_idx: int = 0  # T7: 当前活跃 tab 在 self._pages 中的下标
        self._frame = None  # T15: 当前活跃 FramePage; None = 顶层
        # T18: 累积式 console + network 缓冲 (给 agent 当调试器)
        self._console_messages: list[dict[str, Any]] = []
        self._network_requests: list[dict[str, Any]] = []
        self._page_errors: list[dict[str, Any]] = []
        # T40i: WebSocket 观察 — 每个 (url, opened_at) 一条
        self._websocket_connections: list[dict[str, Any]] = []
        self._max_event_buffer = 1000  # 防无限增长

    async def start(self) -> None:
        """启动浏览器。"""
        if self._browser is not None:
            return  # 已启动
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
        )
        await self._start_context()

    async def _start_context(self) -> None:
        """T33: 给当前 controller 创建独立 context. Pool 用 — 不重复启动 browser."""
        import os
        context_kwargs = {
            "viewport": self.config.viewport,
            "user_agent": self.config.user_agent,
            "locale": self.config.locale,
        }
        if self.config.storage_state_path and os.path.exists(self.config.storage_state_path):
            context_kwargs["storage_state"] = self.config.storage_state_path
        assert self._browser is not None
        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(self.config.timeout)
        # T18: 全局监听 console / network / pageerror (适用于 context 内所有页)
        self._context.on("console", self._on_console)
        self._context.on("request", self._on_request)
        self._context.on("requestfailed", self._on_request_failed)
        self._context.on("response", self._on_response)
        self._context.on("weberror", self._on_web_error)

    async def _ensure_context(self) -> None:
        """T33: Pool 创建的 controller 用 — 第一次操作前确保 context 存在."""
        if self._context is None and self._browser is not None:
            await self._start_context()
        # T18: 全局监听 console / network / pageerror (适用于 context 内所有页)
        self._context.on("console", self._on_console)
        self._context.on("request", self._on_request)
        self._context.on("requestfailed", self._on_request_failed)
        self._context.on("response", self._on_response)
        self._context.on("weberror", self._on_web_error)
        logger.info("BrowserController started (headless=%s)", self.config.headless)

    async def close(self) -> None:
        """关闭浏览器。"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._active_idx = 0
        logger.info("BrowserController closed")

    # ── Tab 管理 (T7) ──────────────────────────────────────

    @property
    def pages(self) -> list[Page]:
        """所有当前活跃 tab 的 Page 对象列表 (按用户操作顺序)。"""
        if self._context is None:
            return []
        # 过滤已关闭的
        return [p for p in self._context.pages if not p.is_closed()]

    @property
    def active_index(self) -> int:
        """当前活跃 tab 在 self.pages 里的下标; 若 page 已关闭则回退到 0。"""
        if self._page is None or self._page.is_closed():
            return 0
        try:
            return self.pages.index(self._page)
        except ValueError:
            return 0

    def list_tabs(self) -> list[dict[str, Any]]:
        """列出所有 tab, 用于 CLI/daemon 输出。同步; 不查 title (异步)。"""
        out = []
        active = self.active_index
        for i, p in enumerate(self.pages):
            out.append({
                "index": i,
                "url": p.url,
                "active": i == active,
            })
        return out

    async def new_tab(self, url: str = "") -> Page:
        """打开新 tab 并切到它。空 url = about:blank。"""
        if self._context is None:
            await self.start()
        page = await self._context.new_page()
        page.on("websocket", self._on_websocket)  # T40i
        if url:
            await page.goto(url, wait_until="networkidle")
        # 新建后自动成为当前活跃 tab (Playwright 默认就是, 但 explicit set 更稳)
        self._page = page
        self._active_idx = self.active_index
        logger.info("Opened new tab: %s", url or "(blank)")
        return page

    async def switch_tab(self, index: int) -> Page:
        """切换到第 N 个 tab。"""
        tabs = self.pages
        if index < 0 or index >= len(tabs):
            raise ValueError(
                f"tab index {index} out of range (have {len(tabs)} tabs: 0..{len(tabs)-1})"
            )
        page = tabs[index]
        # Playwright: bring_to_front 让 tab 在 UI 上聚焦 (headless 不必要, 但无害)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._page = page
        self._active_idx = index
        logger.info("Switched to tab %d: %s", index, page.url)
        return page

    async def close_tab(self, index: int | None = None) -> int:
        """关闭一个 tab。None = 关闭当前。返回剩余 tab 数。"""
        tabs = self.pages
        if not tabs:
            return 0
        if index is None:
            index = self.active_index
        if index < 0 or index >= len(tabs):
            raise ValueError(
                f"tab index {index} out of range (have {len(tabs)} tabs)"
            )
        target = tabs[index]
        await target.close()
        # 切到下一个可用 tab
        remaining = self.pages
        if remaining:
            new_active = min(index, len(remaining) - 1)
            self._page = remaining[new_active]
            self._active_idx = new_active
        else:
            self._page = None
            self._active_idx = 0
        logger.info("Closed tab %d; %d remaining", index, len(remaining))
        return len(remaining)

    async def _ensure_page(self) -> Page:
        """确保有 current_page — 必要时建一个。"""
        if self._page is None or self._page.is_closed():
            if self._context is None:
                # T33: Pool 创建的 controller 共享 browser 但 context 还没建
                if self._browser is not None:
                    await self._start_context()
                else:
                    await self.start()
            self._page = await self._context.new_page()
            # T40i: WebSocket 监控 (per-page, open 握手触发)
            self._page.on("websocket", self._on_websocket)
            self._active_idx = 0
        return self._page

    # ── 基本浏览器动作 ──────────────────────────────────────────

    async def open(self, url: str) -> Page:
        """打开 URL，等待 networkidle。"""
        page = await self._ensure_page()
        await page.goto(url, wait_until="networkidle")
        logger.info("Opened: %s", url)
        return page

    async def back(self) -> None:
        page = await self._ensure_page()
        await page.go_back()

    async def forward(self) -> None:
        page = await self._ensure_page()
        await page.go_forward()

    async def reload(self) -> None:
        page = await self._ensure_page()
        await page.reload()

    async def scroll(self, direction: str = "down", amount: int = 500) -> None:
        """滚动页面。direction: up/down, amount: 像素。"""
        page = await self._ensure_page()
        if direction == "down":
            await page.mouse.wheel(0, amount)
        else:
            await page.mouse.wheel(0, -amount)
        await asyncio.sleep(0.3)

    async def wait(self, seconds: float = 1.0) -> None:
        await asyncio.sleep(seconds)

    # ── T8: 智能等待 — 等元素 / 文本 / URL 出现, 而不是固定 sleep ──

    async def wait_for_text(
        self, text: str, *, timeout_ms: int = 10000,
        in_selector: str = "body",
    ) -> bool:
        """轮询页面直到 in_selector 内出现 text (默认 body 全局)。

        Returns True 找到了, False 超时。
        """
        target = await self._active_page_or_frame()
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await target.locator(in_selector).filter(has_text=text).count()
                if count > 0:
                    return True
            except Exception:
                # locator 暂时无效 (页面切换中), 重试
                pass
            await asyncio.sleep(0.2)
        return False

    async def wait_for_ref(self, ref: str, *, timeout_ms: int = 10000) -> bool:
        """轮询直到 ref 元素出现在 DOM 中 (可见也算, 但不强求 — 现代 SPA
        ref 元素可能在 viewport 外但仍可交互)。"""
        target = await self._active_page_or_frame()
        selector = self._ref_to_selector(ref)
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await page.locator(selector).count()
                if count > 0:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    async def wait_for_url(self, pattern: str, *, timeout_ms: int = 10000) -> bool:
        """轮询直到 page.url 包含 pattern (substring 匹配, 不是 regex — 简单可靠)。"""
        page = await self._ensure_page()
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            if pattern in page.url:
                return True
            await asyncio.sleep(0.2)
        return False

    async def screenshot(self, path: str | None = None) -> bytes:
        """截图。返回 PNG bytes，同时存到 path（如果给定）。"""
        target = await self._active_page_or_frame()
        return await target.screenshot(path=path, full_page=False)

    async def save_storage_state(self, path: str | None = None) -> str:
        """保存 cookies/localStorage 登录态，返回保存路径。"""
        if self._context is None:
            await self.start()
        target = path or self.config.storage_state_path or "~/.semantic-browser/storage-state.json"
        import os
        target = os.path.expanduser(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        await self._context.storage_state(path=target)
        return target

    async def click(self, ref: str) -> bool:
        """通过 @ref 点击元素。"""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=5000)
            logger.info("Clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Click failed ref=%s: %s", ref, e)
            return False

    async def click_with_healing(self, ref: str, *, heal_attempts: int = 2) -> dict[str, Any]:
        """T22: 带 self-healing 的 click — 失败时自动 retry with:
        1. force=True (绕过遮挡检查)
        2. JS click (绕过 Playwright actionability 检查)
        Returns {"ok": bool, "ref": str, "tried": [str], "error": Optional[str]}.
        """
        target = await self._active_page_or_frame()
        selector = self._ref_to_selector(ref)
        tried: list[str] = []
        last_err = None

        # 第一次: 标准 click
        tried.append("normal")
        try:
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=5000)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        if heal_attempts <= 0:
            return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

        # 第二次: force=True (跳过遮挡检查)
        tried.append("force")
        try:
            locator = target.locator(selector).first
            await locator.click(force=True, timeout=5000)
            logger.info("Healed click with force=True ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        # 第三次: JS click (绕过所有 actionability)
        tried.append("js")
        try:
            ok = await target.evaluate(
                "(sel) => { const el = document.querySelector(sel); "
                "if (el) { el.click(); return true; } return false; }",
                selector,
            )
            if ok:
                logger.info("Healed click via JS ref=%s", ref)
                return {"ok": True, "ref": ref, "tried": tried, "error": None}
            last_err = "JS click: element not found"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

    async def type_text(self, ref: str, text: str) -> bool:
        """通过 @ref 输入文本。"""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.fill(text, timeout=5000)
            logger.info("Typed into ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Type failed ref=%s: %s", ref, e)
            return False

    async def type_with_healing(self, ref: str, text: str, *, heal_attempts: int = 2) -> dict[str, Any]:
        """T22: 带 self-healing 的 type_text — 失败时自动:
        1. force=True fill
        2. JS set value + dispatch input event (绕过 React 受控组件检查)
        Returns {"ok", "ref", "tried", "error"}.
        """
        target = await self._active_page_or_frame()
        selector = self._ref_to_selector(ref)
        tried: list[str] = []
        last_err = None

        # 第一次: 标准 fill
        tried.append("normal")
        try:
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.fill(text, timeout=5000)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        if heal_attempts <= 0:
            return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

        # 第二次: force=True fill
        tried.append("force")
        try:
            locator = target.locator(selector).first
            await locator.fill(text, force=True, timeout=5000)
            logger.info("Healed fill with force=True ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        # 第三次: JS dispatch input event (绕过 React 受控组件 / 框架拦截)
        tried.append("js")
        try:
            await target.evaluate(
                "([sel, v]) => { const el = document.querySelector(sel); "
                "if (!el) return false; "
                "const setter = Object.getOwnPropertyDescriptor("
                "  window.HTMLInputElement.prototype, 'value').set; "
                "setter.call(el, v); "
                "el.dispatchEvent(new Event('input', { bubbles: true })); "
                "el.dispatchEvent(new Event('change', { bubbles: true })); "
                "return true; }",
                [selector, text],
            )
            logger.info("Healed fill via JS ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

    async def fill_form(self, fields: dict[str, str]) -> dict[str, bool]:
        """T11: 一次性填多个字段 (人类填表的"批量"动作)。

        Returns {ref: ok} — agent 能立即看出哪些字段没填上, 再针对性 retry。
        """
        out: dict[str, bool] = {}
        for ref, text in fields.items():
            out[ref] = await self.type_text(ref, text)
        return out

    # ── T19: 完整动作原语 (hover / dblclick / rightclick / drag / select) ──

    async def hover(self, ref: str) -> bool:
        """T19: 鼠标悬停在 ref 元素上 (触发 hover 状态 / tooltip / 下拉菜单等)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.hover(timeout=5000)
            logger.info("Hovered ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Hover failed ref=%s: %s", ref, e)
            return False

    async def dblclick(self, ref: str) -> bool:
        """T19: 双击元素 (人类编辑文件 / 打开项目的动作)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.dblclick(timeout=5000)
            logger.info("Double-clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Dblclick failed ref=%s: %s", ref, e)
            return False

    async def rightclick(self, ref: str) -> bool:
        """T19: 右键点击元素 (打开 context menu)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.click(button="right", timeout=5000)
            logger.info("Right-clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Rightclick failed ref=%s: %s", ref, e)
            return False

    async def drag(self, from_ref: str, to_ref: str) -> bool:
        """T19 + T28: 拖拽 from_ref 到 to_ref. 鼠标手势 + HTML5 双策略.

        优先 mouse gesture (兼容 jQuery UI draggable, Sortable.js 老版本);
        失败 fallback 到 HTML5 DataTransfer dispatch (React-dnd / 现代 dnd 库).
        返回 True 表示任意一种方式触发了 drop event.
        """
        target = await self._active_page_or_frame()
        try:
            from_sel = self._ref_to_selector(from_ref)
            to_sel = self._ref_to_selector(to_ref)
            from_loc = target.locator(from_sel).first
            to_loc = target.locator(to_sel).first
            await from_loc.scroll_into_view_if_needed(timeout=5000)
            await to_loc.scroll_into_view_if_needed(timeout=5000)
            from_box = await from_loc.bounding_box()
            to_box = await to_loc.bounding_box()
            if from_box is None or to_box is None:
                raise RuntimeError("element not visible (no bounding box)")
            # 鼠标手势拖 (兼容性最好 — 不依赖 HTML5 drag API)
            sx = from_box["x"] + from_box["width"] / 2
            sy = from_box["y"] + from_box["height"] / 2
            tx = to_box["x"] + to_box["width"] / 2
            ty = to_box["y"] + to_box["height"] / 2
            await target.mouse.move(sx, sy)
            await target.mouse.down()
            # 多步移动 (某些 framework 需要中间步骤才触发 dragenter/dragover)
            await target.mouse.move((sx + tx) / 2, (sy + ty) / 2, steps=10)
            await target.mouse.move(tx, ty, steps=10)
            await target.mouse.up()
            logger.info("Dragged (mouse) ref=%s -> ref=%s", from_ref, to_ref)
            return True
        except Exception as e:
            logger.warning("Mouse drag failed ref=%s->%s: %s; trying HTML5", from_ref, to_ref, e)
            return await self.drag_html5(from_ref, to_ref)

    async def drag_html5(self, from_ref: str, to_ref: str) -> bool:
        """T28: HTML5 drag-and-drop via DataTransfer + dispatchEvent.

        解决 React-dnd / 现代 dnd 库对 mouse gesture 无响应的问题.
        通过共享 DataTransfer 对象构造 dragstart → dragover → drop 序列.
        """
        target = await self._active_page_or_frame()
        try:
            from_sel = self._ref_to_selector(from_ref)
            to_sel = self._ref_to_selector(to_ref)
            # 在 page 上跑一段脚本: 用共享 DataTransfer 派发 dragstart/dragenter/dragover/drop
            ok = await target.evaluate(
                """([fromSel, toSel]) => {
                    const from = document.querySelector(fromSel);
                    const to = document.querySelector(toSel);
                    if (!from || !to) return {ok: false, error: 'element not found'};
                    const dt = new DataTransfer();
                    const fire = (el, type) => {
                        const r = el.getBoundingClientRect();
                        const ev = new DragEvent(type, {
                            bubbles: true, cancelable: true,
                            dataTransfer: dt,
                            clientX: r.left + r.width / 2,
                            clientY: r.top + r.height / 2,
                        });
                        el.dispatchEvent(ev);
                        return ev;
                    };
                    fire(from, 'dragstart');
                    fire(to, 'dragenter');
                    fire(to, 'dragover');
                    fire(to, 'drop');
                    fire(from, 'dragend');
                    return {ok: true};
                }""",
                [from_sel, to_sel],
            )
            if isinstance(ok, dict) and ok.get("ok"):
                logger.info("Dragged (html5) ref=%s -> ref=%s", from_ref, to_ref)
                return True
            err = ok.get("error") if isinstance(ok, dict) else "unknown"
            logger.warning("HTML5 drag failed ref=%s->%s: %s", from_ref, to_ref, err)
            return False
        except Exception as e:
            logger.warning("HTML5 drag exception ref=%s->%s: %s", from_ref, to_ref, e)
            return False

    async def select_option(self, ref: str, value: str | list[str]) -> bool:
        """T19: 在 <select> ref 上选 value. 接受单值或 list (multi-select).

        value 可以是 option 的 value / label / index (Playwright 支持).
        """
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.select_option(value, timeout=5000)
            n = len(value) if isinstance(value, list) else 1
            logger.info("Selected %d option(s) on ref=%s", n, ref)
            return True
        except Exception as e:
            logger.warning("Select failed ref=%s: %s", ref, e)
            return False

    async def press_key(self, key: str) -> None:
        """按键，如 Enter, Tab, Escape。"""
        page = await self._ensure_page()
        await page.keyboard.press(key)

    # ── T13: 文件上传 ────────────────────────────────────────

    async def set_files(self, ref: str, paths: list[str]) -> dict[str, Any]:
        """T13: 通过 ref 给 file input 设置文件路径 (人类"上传附件"动作).

        Returns {"ok": bool, "ref": str, "file_count": int, "error": Optional[str]}.
        """
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.set_input_files(paths, timeout=10000)
            logger.info("Set files ref=%s: %d files", ref, len(paths))
            return {"ok": True, "ref": ref, "file_count": len(paths), "error": None}
        except Exception as e:
            logger.warning("set_files failed ref=%s: %s", ref, e)
            return {"ok": False, "ref": ref, "file_count": 0, "error": str(e)[:200]}

    # ── T14: 下载拦截 ────────────────────────────────────────

    async def download_file(
        self,
        trigger_ref: str | None = None,
        *,
        save_to: str | None = None,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """T14: 触发下载并保存文件。

        用法 1 — 知道 ref: `download_file(trigger_ref='e5', save_to='/tmp/file.zip')`
        用法 2 — 已点击外部触发器 (e.g. agent 已 click): `download_file(save_to='/tmp/x')`
                等下一个下载事件 (适用罕见场景).

        Returns {"ok", "path", "size", "suggested_filename", "url"}.
        """
        page = await self._ensure_page()
        import os as _os

        async def _do_download():
            if trigger_ref:
                # 边 click 边捕获 download 事件
                async with page.expect_download(timeout=timeout_ms) as dl_info:
                    ok = await self.click(trigger_ref)
                    if not ok:
                        raise RuntimeError(f"click {trigger_ref} failed")
                download = await dl_info.value
            else:
                # 等待下一个 download 事件 (调用前已 click 过了)
                download = await page.expect_download(timeout=timeout_ms).__aenter__()
            suggested = download.suggested_filename
            target = save_to or _os.path.join("/tmp", suggested or "download.bin")
            await download.save_as(target)
            return download, target, suggested

        try:
            download, target, suggested = await _do_download()
            size = _os.path.getsize(target) if _os.path.exists(target) else 0
            return {
                "ok": True,
                "path": target,
                "size": size,
                "suggested_filename": suggested,
                "url": download.url,
            }
        except Exception as e:
            return {
                "ok": False,
                "path": None,
                "size": 0,
                "suggested_filename": None,
                "url": None,
                "error": f"{type(e).__name__}: {e}"[:200],
            }

    # ── 页面信息 ──────────────────────────────────────────────

    async def get_url(self) -> str:
        page = await self._ensure_page()
        return page.url

    async def get_title(self) -> str:
        page = await self._ensure_page()
        return await page.title()

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
        from urllib.parse import urlparse, urljoin
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

    async def fetch_script_source(self, url: str, *, timeout_ms: int = 5000) -> str:
        """T39: deep 模式专用 — 按 URL 抓 JS 源码 (httpx).

        不通过浏览器 — 因为浏览器里 fetch 受 CORS 限制.
        直接服务端 fetch (允许任意 origin), 给 agent 看完整 JS.
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
                r = await client.get(url)
                return r.text[:50000]  # 50K 上限, 防止 OOM
        except Exception as e:
            return f"(fetch failed: {type(e).__name__}: {e})"

    # ── T40g: API endpoint extraction ─────────────────────────

    # 简单 regex 模式 — 不追求穷举, 抓常见 fetch/XHR/axios/$.ajax 即可
    _API_PATTERNS: tuple[tuple[str, str], ...] = (
        # fetch("...") / fetch(`...`)
        (r'''fetch\s*\(\s*[`"']([^`"']{3,300})[`"']''', "fetch"),
        # axios.<method>("...")
        (r'''axios\.(?:get|post|put|delete|patch|head|options)\s*\(\s*[`"']([^`"']{3,300})[`"']''', "axios"),
        # xhr.open("METHOD", "URL")
        (r'''\.open\s*\(\s*[`"'](?:GET|POST|PUT|DELETE|PATCH|HEAD)["']\s*,\s*[`"']([^`"']{3,300})[`"']''', "xhr"),
        # $.ajax({url: "..."})
        (r'''\$\.ajax\s*\(\s*\{[^}]*?url\s*:\s*[`"']([^`"']{3,300})[`"']''', "jquery"),
        # superagent / got: .get("/api/...") .post("/api/...")
        (r'''\.(?:get|post|put|delete|patch)\s*\(\s*[`"'](/[a-zA-Z][^`"']{2,300})[`"']''', "rest-method"),
    )

    async def extract_api_endpoints(
        self,
        *,
        max_scripts: int = 25,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T40g: 从页面 JS 中提取 API endpoint.

        流程:
          1. page.evaluate 列出所有 <script src=...> (含 inline src)
          2. httpx 直抓每个 JS 源码 (避开 CORS)
          3. 走 _API_PATTERNS regex, 提取候选 URL/path
          4. 去重 + 分类 + 返回

        Returns {
          "page_url",
          "scripts_scanned": int,
          "scripts_failed": int,
          "endpoints": [
            {"value": "/api/users", "method": "GET", "sources": ["fetch"], "script": "https://..."},
            ...
          ],
          "by_method": {"GET": N, "POST": M, ...},
        }
        """
        import re
        import httpx
        from urllib.parse import urljoin

        page = await self._ensure_page()

        # 1. 列出 scripts (只 external, inline 太难 dedup)
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        # 限制总数
        scripts = scripts[:max_scripts]

        # 2. 转绝对 URL
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        endpoints: dict[str, dict[str, Any]] = {}
        scripts_scanned = 0
        scripts_failed = 0

        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:200000]  # 200K 上限
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue

                for pat, source in self._API_PATTERNS:
                    for m in re.finditer(pat, body, re.DOTALL):
                        val = m.group(1).strip()
                        if not val:
                            continue
                        # 过滤: 必须以 / 开头 (path) 或 http 开头 (absolute url)
                        if not (val.startswith("/") or val.startswith("http")):
                            continue
                        # 跳过太短/太通用
                        if len(val) < 3:
                            continue
                        if val in ("/", "//"):
                            continue
                        # 截断模板字符串 (含 ${} 或 backtick 不完整)
                        val = val.split("${")[0].rstrip("/")
                        if not val:
                            continue
                        ep = endpoints.setdefault(val, {
                            "value": val,
                            "sources": set(),
                            "scripts": set(),
                            "first_method": source,
                        })
                        ep["sources"].add(source)
                        ep["scripts"].add(url)

        # 3. 序列化 + 简单分类
        out_list = []
        by_source: dict[str, int] = {}
        for v, ep in sorted(endpoints.items()):
            out_list.append({
                "value": ep["value"],
                "sources": sorted(ep["sources"]),
                "scripts_count": len(ep["scripts"]),
            })
            for s in ep["sources"]:
                by_source[s] = by_source.get(s, 0) + 1

        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "endpoint_count": len(out_list),
            "endpoints": out_list,
            "by_source": by_source,
        }

    # ── T42b: JS library fingerprinting ────────────────────────

    # 已知 JS 库 + 关键 CVE 表 — name → [(regex, version_group_index, [(max_vuln_ver, cve_id, desc)])]
    # 客户端版本字符串通常出现在 URL: jquery-3.5.1.min.js, react@17.0.2.js, vue/2.6.14/vue.min.js
    _JS_LIB_FINGERPRINTS: tuple[dict[str, Any], ...] = (
        {
            "name": "jQuery",
            "patterns": (
                r"jquery[/-](\d+\.\d+(?:\.\d+)?)",
                r"jquery[.-](\d+\.\d+(?:\.\d+)?)",
            ),
            "cves": (
                ("3.5.0", "CVE-2020-11022/CVE-2020-11023", "XSS via untrusted HTML passed to DOM manipulation methods"),
                ("3.0.0", "CVE-2019-11358", "Prototype pollution in jQuery.extend"),
                ("3.4.0", "CVE-2016-10706", "Prototype pollution via jQuery.uniqueSort"),
            ),
        },
        {
            "name": "AngularJS",
            "patterns": (r"angular[/-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (
                ("1.8.0", "CVE-2020-7676", "XSS in angular.copy"),
            ),
        },
        {
            "name": "Bootstrap",
            "patterns": (r"bootstrap[/-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (
                ("4.0.0", "CVE-2019-8331", "XSS in tooltip/popover data-template"),
            ),
        },
        {
            "name": "Lodash",
            "patterns": (r"lodash[.-](\d+\.\d+(?:\.\d+)?)", r"lodash@(\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("4.17.21", "CVE-2020-8203", "Prototype pollution in zipObjectDeep"),
            ),
        },
        {
            "name": "Moment.js",
            "patterns": (r"moment[.-](\d+\.\d+(?:\.\d+)?)", r"moment[/-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("2.29.0", "CVE-2022-24785", "Path traversal in moment.locale"),
            ),
        },
        {
            "name": "Vue.js",
            "patterns": (r"vue[/@](\d+\.\d+(?:\.\d+)?)", r"vue[.-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
        {
            "name": "React",
            "patterns": (r"react[/@](\d+\.\d+(?:\.\d+)?)", r"react[.-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
        {
            "name": "Backbone.js",
            "patterns": (r"backbone[.-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (),
        },
        {
            "name": "Handlebars",
            "patterns": (r"handlebars[.-](\d+\.\d+(?:\.\d+)?)", r"handlebars[/-]v?(\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("4.3.0", "CVE-2019-19919", "Arbitrary code execution via lookup helper"),
                ("4.0.14", "CVE-2017-16016", "XSS via templates"),
            ),
        },
        {
            "name": "axios",
            "patterns": (r"axios[.-](\d+\.\d+(?:\.\d+)?)", r"axios[/@](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
    )

    async def extract_js_libraries(
        self,
        *,
        max_scripts: int = 30,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T42b: 从 <script src> URL 中识别 JS 库 + 版本 + 已知 CVE.

        流程:
          1. 收集所有 <script src=...> URLs
          2. 对每个 URL 用 _JS_LIB_FINGERPRINTS 里的 regex 扫
          3. 命中后解析版本, 对照已知 CVE 表 (用 < 字符串比对)
          4. 多个 URL 命中同一 lib 只保留版本最高的

        Returns {
          "page_url", "scripts_scanned", "scripts_failed",
          "libraries": [
            {"name", "version", "urls": [...], "cves": [{id, max_version, desc}]}
          ],
          "vulnerable_count": int  # 有 known CVE 的 lib 数
        }
        """
        import re
        from urllib.parse import urljoin
        import httpx

        def _vuln_to_cve_entry(threshold: str, cve_id: str, desc: str) -> dict[str, str]:
            return {"max_vuln_version": threshold, "id": cve_id, "desc": desc}

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        # 收集所有 URL 文本 (script src 字符串 + 未来可能 fetch 源码)
        url_corpus = "\n".join(abs_urls)
        scripts_scanned = len(abs_urls)
        scripts_failed = 0

        # 解析 lib 命中
        lib_hits: dict[str, dict[str, Any]] = {}
        for fp in self._JS_LIB_FINGERPRINTS:
            name = fp["name"]
            for pat in fp["patterns"]:
                for m in re.finditer(pat, url_corpus, re.IGNORECASE):
                    ver = m.group(1)
                    hit = lib_hits.setdefault(name, {
                        "name": name,
                        "_versions": {},  # ver -> [urls]
                        "cves": [],
                    })
                    hit["_versions"].setdefault(ver, set()).add(abs_urls[0] if not abs_urls else "")
                    # 找到对应的 url — 用 match.start() 反推
                    for u in abs_urls:
                        if m.group(0) in u:
                            hit["_versions"][ver].add(u)
                            break

        # 计算 CVE
        libraries_out = []
        vulnerable_count = 0
        for name, hit in lib_hits.items():
            fp = next((f for f in self._JS_LIB_FINGERPRINTS if f["name"] == name), None)
            if not fp:
                continue
            # 选最高版本
            best_ver = max(hit["_versions"].keys(), key=lambda v: tuple(int(x) for x in v.split(".")))
            # 选最 representative url (出现次数最多)
            best_urls = sorted(hit["_versions"][best_ver])
            cves: list[dict[str, str]] = []
            for threshold, cve_id, desc in fp["cves"]:
                if _version_lt(best_ver, threshold):
                    cves.append(_vuln_to_cve_entry(threshold, cve_id, desc))
            if cves:
                vulnerable_count += 1
            libraries_out.append({
                "name": name,
                "version": best_ver,
                "urls": best_urls[:5],
                "cves": cves,
            })
        libraries_out.sort(key=lambda x: x["name"])

        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "library_count": len(libraries_out),
            "libraries": libraries_out,
            "vulnerable_count": vulnerable_count,
        }

    # ── T42g: GraphQL introspection ────────────────────────────

    async def detect_graphql(
        self,
        endpoint: str,
        *,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T42g: 给定 GraphQL 端点 URL, 跑 introspection query dump schema.

        经典 introspection:
          {
            __schema {
              queryType { name }
              mutationType { name }
              types { name kind }
            }
          }

        Returns {
          "endpoint", "is_graphql": bool, "error": str or None,
          "query_type": str or None, "mutation_type": str or None,
          "types": [str, ...]   # 所有 type name
          "type_count": int,
        }
        """
        import httpx
        introspection = {
            "query": (
                "{ __schema { queryType { name } mutationType { name } "
                "types { name kind } } }"
            )
        }
        try:
            async with httpx.AsyncClient(
                timeout=timeout_ms / 1000,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "semantic-browser-probe/1.0",
                    "Accept": "application/json",
                },
            ) as client:
                r = await client.post(endpoint, json=introspection)
            if r.status_code >= 400:
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            try:
                data = r.json()
            except Exception as e:
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": f"non-JSON response: {e}"}
            if "data" not in data or "__schema" not in data.get("data", {}):
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": "response missing __schema (likely not GraphQL)"}
            schema = data["data"]["__schema"]
            types = [t["name"] for t in schema.get("types", []) if not t["name"].startswith("__")]
            return {
                "endpoint": endpoint,
                "is_graphql": True,
                "error": None,
                "query_type": (schema.get("queryType") or {}).get("name"),
                "mutation_type": (schema.get("mutationType") or {}).get("name"),
                "types": sorted(types),
                "type_count": len(types),
            }
        except Exception as e:
            return {"endpoint": endpoint, "is_graphql": False,
                    "error": f"{type(e).__name__}: {e}"}

    # ── T40a: 客户端存储 ─────────────────────────────────

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

    # ── T40f: 安全头结构化 ───────────────────────────────

    async def get_security_headers(self, url: str) -> dict[str, Any] | None:
        """T40f: 给定 URL, 把响应头解析成结构化安全审计数据.

        Returns: {
          "url", "raw": {...全部 headers...},
          "csp": {directives, has_unsafe_inline, has_unsafe_eval, ...} 或 None,
          "hsts": {max_age, include_subdomains, preload} 或 None,
          "x_frame_options": str 或 None,
          "x_content_type_options": str 或 None,
          "referrer_policy": str 或 None,
          "coop": str 或 None,
          "coep": str 或 None,
          "permissions_policy": {directives: [...]} 或 None,
          "set_cookie_parsed": [{name, value, httpOnly, secure, sameSite, ...}],
          "score": "OK" | "weak" | "missing"   # 简易评分
        } 或 None (没拿到头).
        """
        raw = await self.get_response_headers(url)
        if raw is None:
            return None
        out: dict[str, Any] = {"url": url, "raw": raw}

        # CSP
        csp_val = raw.get("content-security-policy")
        out["csp"] = _parse_csp(csp_val) if csp_val else None

        # HSTS
        hsts_val = raw.get("strict-transport-security")
        out["hsts"] = _parse_hsts(hsts_val) if hsts_val else None

        out["x_frame_options"] = raw.get("x-frame-options")
        out["x_content_type_options"] = raw.get("x-content-type-options")
        out["referrer_policy"] = raw.get("referrer-policy")
        out["coop"] = raw.get("cross-origin-opener-policy")
        out["coep"] = raw.get("cross-origin-embedder-policy")

        pp_val = raw.get("permissions-policy")
        out["permissions_policy"] = _parse_permissions_policy(pp_val) if pp_val else None

        # Set-Cookie: header 不一定在 response_headers (httpx 通常会按 set-cookie 拆出)
        sc = raw.get("set-cookie") or raw.get("Set-Cookie")
        out["set_cookie_parsed"] = (
            [_parse_set_cookie(s) for s in (sc if isinstance(sc, list) else [sc])]
            if sc else []
        )

        # T42c: CORS 风险评估
        cors_origin = raw.get("access-control-allow-origin")
        cors_creds = raw.get("access-control-allow-credentials", "").lower() == "true"
        out["cors"] = {
            "allow_origin": cors_origin,
            "allow_credentials": cors_creds,
            "allow_methods": raw.get("access-control-allow-methods"),
            "allow_headers": raw.get("access-control-allow-headers"),
            "expose_headers": raw.get("access-control-expose-headers"),
            "max_age": raw.get("access-control-max-age"),
            "risk": _assess_cors_risk(cors_origin, cors_creds),
        }

        # 简易评分 (安全头覆盖度)
        score = 0
        if out["csp"]:               score += 2
        if out["hsts"]:              score += 1
        if out["x_frame_options"]:   score += 1
        if out["x_content_type_options"]: score += 1
        if out["referrer_policy"]:   score += 1
        if out["coop"] or out["coep"]: score += 1
        if out["set_cookie_parsed"]:
            for sc_entry in out["set_cookie_parsed"]:
                if sc_entry.get("httpOnly"): score += 1
                if sc_entry.get("secure"): score += 1
                break  # 只看第一个 cookie 的 flags, 避免重复计
        if score >= 6:
            out["score"] = "OK"
        elif score >= 3:
            out["score"] = "weak"
        else:
            out["score"] = "missing"
        return out

    # ── T40b: Hidden paths probe ─────────────────────────────

    # 常见 path 列表 — 分四类
    _WELL_KNOWN_PATHS: tuple[str, ...] = (
        "/.well-known/security.txt",
        "/.well-known/openid-configuration",
        "/.well-known/change-password",
        "/.well-known/apple-app-site-association",
        "/.well-known/assetlinks.json",
        "/.well-known/mta-sts.txt",
        "/.well-known/acme-challenge/",
    )
    _DISCOVERY_PATHS: tuple[str, ...] = (
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/llms.txt",
        "/humans.txt",
        "/manifest.json",
        "/crossdomain.xml",
        "/clientaccesspolicy.xml",
        "/.git/HEAD",
        "/.env",
    )
    _ADMIN_PATHS: tuple[str, ...] = (
        "/admin",
        "/admin/login",
        "/administrator",
        "/login",
        "/wp-admin/",
        "/wp-login.php",
        "/user/login",
        "/api",
        "/api/v1",
        "/graphql",
        "/cgi-bin/",
        "/phpmyadmin/",
        "/server-status",
        "/.htaccess",
    )
    # T42f: devops / debug / actuator 端点 (Spring Boot Actuator / Flask debug / Django / PHP)
    _DEBUG_PATHS: tuple[str, ...] = (
        "/debug",
        "/debug/vars",
        "/debug/pprof",
        "/trace",
        "/actuator",
        "/actuator/env",
        "/actuator/health",
        "/actuator/info",
        "/actuator/metrics",
        "/actuator/beans",
        "/actuator/mappings",
        "/actuator/configprops",
        "/actuator/heapdump",
        "/actuator/threaddump",
        "/actuator/loggers",
        "/env",
        "/info",
        "/health",
        "/metrics",
        "/_debug",
        "/__debug__",
        "/_profiler",
        "/phpinfo.php",
        "/server-info",
        "/status",
        "/.env.production",
        "/.env.local",
        "/config",
        "/configuration",
        "/swagger",
        "/swagger-ui.html",
        "/swagger-ui/",
        "/v1/api-docs",
        "/v2/api-docs",
        "/v3/api-docs",
        "/openapi.json",
        "/openapi.yaml",
        "/api-docs",
        "/redoc",
        "/graphiql",
        "/playground",
    )

    async def probe_paths(
        self,
        base_url: str,
        *,
        categories: list[str] | None = None,
        timeout_ms: int = 5000,
        max_concurrency: int = 6,
    ) -> dict[str, Any]:
        """T40b: 探测常见隐藏路径 — 给 agent / 安全审计用.

        探测三类 path:
          - well_known:  /.well-known/* (RFC 8615 + 行业标准)
          - discovery:  robots.txt / sitemap.xml / .git/HEAD 等发现类
          - admin:      /admin /login /api /graphql 等常见管理/API 入口

        不通过浏览器 — 用 httpx 直发 (避开 CORS, 不污染浏览历史).
        所有 path 并发探测 (max_concurrency 控制并发).

        Args:
            base_url: 起点 URL, 自动从其中解析 origin
            categories: 子集白名单 (None = 全部三类); 可选 "well_known"/"discovery"/"admin"
            timeout_ms: 单 path 超时
            max_concurrency: 并发上限

        Returns: {
          "base_url", "origin",
          "found": [{"path", "status", "category", "content_type", "size", "redirect"}],
          "missing": [{"path", "category", "status": 404}],
          "total_probed": int,
          "duration_ms": int,
        }
        """
        import httpx
        import time as _time
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        wanted_cats = categories or ["well_known", "discovery", "admin", "debug"]
        all_paths: list[tuple[str, str]] = []
        if "well_known" in wanted_cats:
            all_paths += [("well_known", p) for p in self._WELL_KNOWN_PATHS]
        if "discovery" in wanted_cats:
            all_paths += [("discovery", p) for p in self._DISCOVERY_PATHS]
        if "admin" in wanted_cats:
            all_paths += [("admin", p) for p in self._ADMIN_PATHS]
        if "debug" in wanted_cats:  # T42f
            all_paths += [("debug", p) for p in self._DEBUG_PATHS]

        sem = asyncio.Semaphore(max_concurrency)
        found: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        soft_404_count = 0  # T42e
        t0 = _time.monotonic()

        # T42e: 第一次先拿一个肯定不存在的 path, 用它的 body length 作 soft-404 baseline.
        baseline_size: int | None = None
        baseline_has_404: bool = False
        try:
            async with httpx.AsyncClient(
                timeout=timeout_ms / 1000,
                follow_redirects=False,
                headers={"User-Agent": "semantic-browser-probe/1.0"},
            ) as client:
                r = await client.get(origin + "/zzz-sb-probe-nonexistent-zzz")
                baseline_size = len(r.content)
                baseline_has_404 = (r.status_code == 200 and (
                    "404" in r.text[:5000].lower() or
                    "not found" in r.text[:5000].lower() or
                    "page not found" in r.text[:5000].lower()
                ))
        except Exception:
            pass

        def _is_soft_404(content: bytes, status: int) -> bool:
            """T42e: 检测 soft-404 — 200 但内容是 404 页.
            启发式: 内容很短 (<= baseline+10%) 且包含 '404'/'not found' 关键字.
            """
            if status != 200 or baseline_size is None:
                return False
            size = len(content)
            # 体积异常小 (与 baseline 几乎一致, 误差 < 10%)
            if baseline_size > 0 and abs(size - baseline_size) < max(50, baseline_size * 0.10):
                text = content[:5000].decode("utf-8", errors="ignore").lower()
                if "404" in text or "not found" in text or "page not found" in text:
                    return True
            return False

        async def _probe_one(cat: str, path: str) -> None:
            nonlocal soft_404_count
            url = origin + path
            try:
                async with sem:
                    async with httpx.AsyncClient(
                        timeout=timeout_ms / 1000,
                        follow_redirects=False,
                        headers={"User-Agent": "semantic-browser-probe/1.0"},
                    ) as client:
                        r = await client.get(url)
                status = r.status_code
                entry: dict[str, Any] = {
                    "path": path,
                    "status": status,
                    "category": cat,
                    "url": url,
                }
                if status in (200, 301, 302, 307, 308, 401, 403):
                    entry["content_type"] = r.headers.get("content-type", "")
                    entry["size"] = len(r.content)
                    if 300 <= status < 400:
                        entry["redirect"] = r.headers.get("location", "")
                    # T42e: soft-404 标记
                    if status == 200 and _is_soft_404(r.content, status):
                        entry["soft_404"] = True
                        soft_404_count += 1
                    found.append(entry)
                else:
                    missing.append({"path": path, "category": cat, "status": status})
            except Exception as e:
                missing.append({
                    "path": path, "category": cat,
                    "status": -1, "error": f"{type(e).__name__}: {e}",
                })

        await asyncio.gather(*[_probe_one(c, p) for c, p in all_paths])

        return {
            "base_url": base_url,
            "origin": origin,
            "found": sorted(found, key=lambda x: (x["category"], x["path"])),
            "missing": sorted(missing, key=lambda x: (x["category"], x["path"])),
            "total_probed": len(all_paths),
            "soft_404_count": soft_404_count,  # T42e
            "duration_ms": int((_time.monotonic() - t0) * 1000),
        }

    async def get_content(self) -> str:
        """获取页面 (或当前 frame) 的 HTML。"""
        target = await self._active_page_or_frame()
        return await target.content()

    async def get_aria_snapshot(self) -> str:
        """
        获取 Playwright aria snapshot — 这是核心能力。

        返回的是 accessibility tree 的 YAML 表示，类似:
            - main:
              - heading "Welcome" [level=1]
              - link "About" [ref=e3]
              - textbox "Search" [ref=e4]
        """
        page = await self._ensure_page()
        try:
            return await page.aria_snapshot()
        except Exception as exc:
            logger.warning("aria_snapshot failed: %s", exc)
            return ""

    def _format_aria_tree(self, node: dict, indent: int = 0) -> str:
        """递归格式化 aria tree 为可读文本。"""
        lines = []
        prefix = "  " * indent
        role = node.get("role", "")
        name = node.get("name", "")
        ref = ""

        # Playwright 给可操作元素分配 ref
        if "ref" in node:
            ref = f" [ref=e{node['ref']}]"

        label = f"{prefix}- {role}"
        if name:
            label += f' "{name}"'
        if ref:
            label += ref
        lines.append(label)

        for child in node.get("children", []):
            lines.append(self._format_aria_tree(child, indent + 1))
        return "\n".join(lines)

    @staticmethod
    def _ref_to_selector(ref: str) -> str:
        """将 eN ref 转为 SnapshotEngine 注入的稳定 DOM selector。"""
        ref = ref.strip().lstrip("@")
        if ref.isdigit():
            ref = f"e{ref}"
        if not re.fullmatch(r"e\d+", ref):
            raise ValueError(f"Invalid semantic browser ref: {ref!r}")
        return f'[data-sb-ref="{ref}"]'

    @property
    def current_page(self) -> Optional[Page]:
        return self._page

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
        page = await self._ensure_page()
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

    # ── T16: 键盘 / 焦点 / Tab 导航 ───────────────────────────
    #
    # agent 模拟人类键盘浏览: Tab / Shift+Tab / Enter / Esc / 焦点环查询 /
    # 键盘快捷键 (Ctrl+A/F5 等). 现代 SPA 大量依赖键盘可达性.

    async def get_focused_element(self) -> dict[str, Any]:
        """T16: 返回当前 active element 的描述.

        Returns {"tag", "type", "ref", "text", "value", "href"} 或 {} 若无焦点.
        用 :focus + [data-sb-ref] 查 ref.
        """
        target = await self._active_page_or_frame()
        info = await target.evaluate("""
            () => {
                const el = document.activeElement;
                if (!el || el === document.body) return null;
                const out = {
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    ref: el.getAttribute('data-sb-ref') || null,
                    text: (el.textContent || '').trim().slice(0, 80),
                    value: el.value !== undefined ? String(el.value).slice(0, 200) : null,
                    href: el.href || null,
                    placeholder: el.placeholder || null,
                    aria_label: el.getAttribute('aria-label') || null,
                };
                return out;
            }
        """)
        return info or {}

    async def focus(self, ref: str) -> bool:
        """T16: 把焦点设到 ref 元素上 (无需 click)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.focus(timeout=5000)
            return True
        except Exception as e:
            logger.warning("Focus failed ref=%s: %s", ref, e)
            return False

    async def tab(self, shift: bool = False, count: int = 1) -> str | None:
        """T16: 按 Tab N 次. shift=True = Shift+Tab (反方向).

        返回最后焦点元素的 ref (若有), 便于 agent 接着操作.
        """
        target = await self._active_page_or_frame()
        for _ in range(count):
            key = "Shift+Tab" if shift else "Tab"
            await target.keyboard.press(key)
            await asyncio.sleep(0.05)
        # 看现在焦点在哪儿
        info = await self.get_focused_element()
        return info.get("ref") if isinstance(info, dict) else None

    async def keyboard_shortcut(self, *keys: str) -> None:
        """T16: 键盘组合键. 用法: keyboard_shortcut('Control', 'a') (全选).
        或者 keyboard_shortcut('F5') (单键也支持).
        """
        target = await self._active_page_or_frame()
        if len(keys) == 1:
            await target.keyboard.press(keys[0])
        else:
            await target.keyboard.press("+".join(keys))

    async def type_into_active(self, text: str, delay_ms: int = 0) -> bool:
        """T16: 直接往当前焦点元素打字 (不需要 ref). 模拟人类"键入"动作.

        delay_ms > 0 时模拟真实键入速度 (避免某些 framework 拦截过快键入).
        """
        target = await self._active_page_or_frame()
        try:
            if delay_ms > 0:
                await target.keyboard.type(text, delay=delay_ms)
            else:
                await target.keyboard.type(text)
            return True
        except Exception as e:
            logger.warning("type_into_active failed: %s", e)
            return False

    # ── T15: Frame (iframe) 支持 ─────────────────────────────

    @property
    def active_frame(self) -> Optional[Page]:
        """当前活跃的 frame (page 或 frame_page); 默认 = current_page."""
        return self._page  # 初始 = 顶层 page

    async def list_frames(self) -> list[dict[str, Any]]:
        """T15/T40e: 列出所有 frame (顶层 + 所有 iframe) — 含结构信息.

        每个 frame 报告:
          - name, url, is_main
          - depth: 嵌套深度 (顶层 = 0)
          - parent: 父 frame 的 name (顶层 = None)
          - is_cross_origin: 与顶层不同源 (可能受 CORS 限制, agent 拿不到内部 DOM)
          - child_count: 直接子 frame 数

        Returns [
          {"name": "main", "url": "...", "is_main": True, "depth": 0,
           "parent": None, "is_cross_origin": False, "child_count": N},
          {"name": "frame[foo]", "url": "...", "is_main": False, "depth": 1,
           "parent": "main", "is_cross_origin": bool, "child_count": M},
          ...
        ]
        """
        from urllib.parse import urlparse
        page = await self._ensure_page()
        origin_top = urlparse(page.url).netloc
        # 先建一个 name → frame 的索引, 同时递归算 child_count + depth
        frames = [f for f in page.frames]

        def _parent_of(f):
            return f.parent_frame if f.parent_frame in frames else None

        def _children_of(f):
            return [c for c in frames if _parent_of(c) is f]

        out: list[dict[str, Any]] = []
        # 主 frame
        out.append({
            "name": "main",
            "url": page.url,
            "is_main": True,
            "depth": 0,
            "parent": None,
            "is_cross_origin": False,
            "child_count": len(_children_of(page.main_frame)),
        })
        # BFS 算 depth
        visited: set[int] = {id(page.main_frame)}
        queue: list[tuple[Any, int]] = [(page.main_frame, 0)]
        # index by id, 用于 child lookup
        id_to_frame = {id(f): f for f in frames}
        id_to_frame[id(page.main_frame)] = page.main_frame
        while queue:
            cur, depth = queue.pop(0)
            for child in _children_of(cur):
                if id(child) in visited:
                    continue
                visited.add(id(child))
                queue.append((child, depth + 1))
                try:
                    child_origin = urlparse(child.url).netloc
                    is_cross = child_origin != origin_top
                except Exception:
                    is_cross = True
                out.append({
                    "name": f"frame[{child.name or '(unnamed)'}]",
                    "url": child.url,
                    "is_main": False,
                    "depth": depth + 1,
                    "parent": "main" if cur is page.main_frame else f"frame[{cur.name or '(unnamed)'}]",
                    "is_cross_origin": is_cross,
                    "child_count": len(_children_of(child)),
                })
        return out

    async def switch_frame(self, name_or_url: str) -> dict[str, Any]:
        """T15: 切换活跃 frame (按 name substring 或 url substring 匹配).

        设置 _frame 后, 所有 click/type/snapshot/wait 都作用在该 frame 上。
        Returns {"name", "url"} or raises ValueError if not found.
        """
        page = await self._ensure_page()
        # 主 frame 用特殊 key
        if name_or_url in ("main", "top"):
            self._frame = None
            return {"name": "main", "url": page.url}
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if (frame.name and name_or_url in frame.name) or name_or_url in frame.url:
                self._frame = frame
                logger.info("Switched to frame: %s (%s)", frame.name, frame.url)
                return {"name": frame.name, "url": frame.url}
        raise ValueError(f"frame not found: {name_or_url!r}; try one of {[f['name'] for f in await self.list_frames()]}")

    async def to_top_frame(self) -> None:
        """T15: 回到顶层 frame."""
        self._frame = None

    async def _active_page_or_frame(self) -> Any:
        """返回当前活跃 page (或 frame 替身). Frame 也实现了 page-like 接口
        (locator, click, fill, set_input_files, screenshot 等),
        所以 click/type/snapshot/wait 等操作都可以路由到 frame.

        若 frame 已设, 直接返回 frame (避免无谓 page 初始化).
        """
        if self._frame is not None:
            return self._frame
        return await self._ensure_page()

    # ── T43a: 子域名枚举 (crt.sh + TLS cert SAN) ──────────

    async def enumerate_subdomains(
        self,
        host: str,
        *,
        include_tls_san: bool = True,
        crtsh_timeout: float = 12.0,
    ) -> dict[str, Any]:
        """T43a: 子域名枚举 — pen-tester recon 第一步.

        1. crt.sh JSON API (Certificate Transparency logs)
        2. 可选: TLS cert SAN 解析 (fallback / 补全)

        Returns {
          "host",
          "subdomains": [sorted unique list ending in host],
          "by_source": {"crtsh": N, "tls_san": M},
          "crtsh_status": "ok" | "timeout" | "error",
          "crtsh_error": str | None,
          "subdomain_count": int,
        }
        """
        import json as _json
        import re as _re
        from urllib.request import urlopen, Request
        from urllib.error import URLError

        seen: dict[str, set[str]] = {}
        crtsh_status = "ok"
        crtsh_error: str | None = None

        # 1) crt.sh
        try:
            url = f"https://crt.sh/?q={host}&output=json"
            req = Request(url, headers={"User-Agent": "semantic-browser-recon/1.0"})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urlopen(req, timeout=crtsh_timeout, context=ctx) as r:  # noqa: S310 - intentional CT log query
                body = r.read()
            entries = _json.loads(body)
            for e in entries:
                nv = e.get("name_value") or ""
                for s in nv.split("\n"):
                    s = s.strip().lower().lstrip("*.")
                    if not s or s == host:
                        continue
                    if s.endswith("." + host) or s == host:
                        seen.setdefault(s, set()).add("crtsh")
        except (URLError, TimeoutError, ValueError) as e:
            crtsh_status = "error" if "timeout" not in str(e).lower() else "timeout"
            crtsh_error = str(e)[:200]

        # 2) TLS SAN (optional)
        if include_tls_san:
            for s in _tls_subdomains(host):
                seen.setdefault(s, set()).add("tls_san")

        subdomains = sorted(seen.keys())
        by_source: dict[str, int] = {}
        for srcs in seen.values():
            for src in srcs:
                by_source[src] = by_source.get(src, 0) + 1
        return {
            "host": host,
            "subdomains": subdomains,
            "by_source": by_source,
            "crtsh_status": crtsh_status,
            "crtsh_error": crtsh_error,
            "subdomain_count": len(subdomains),
        }

    # ── T43b: JS 源码硬编码 secret 扫描 ────────────────────

    async def extract_secrets_from_js(
        self,
        *,
        max_scripts: int = 20,
        max_body: int = 200_000,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        r"""T43b: 扫页面所有 <script src> 源码, 找硬编码 secret.

        模式:
          - AWS access key:    AKIA[0-9A-Z]{16}
          - AWS secret key:    [A-Za-z0-9/+=]{40} 紧跟 aws_secret
          - GitHub token:      ghp_[A-Za-z0-9]{36} / gho_/ghs_/ghr_
          - Slack token:       xox[baprs]-[A-Za-z0-9-]+
          - Google API key:    AIza[0-9A-Za-z_-]{35}
          - Generic Bearer:    Bearer [A-Za-z0-9._-]{20,}
          - api_key=:          api[_-]?key["']?\s*[:=]\s*["']?([A-Za-z0-9_\-]{16,})
          - password=:         (?:password|passwd|pwd)\s*[:=]\s*["']([^"']{6,})["']
          - private key:       -----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----

        Returns {
          "page_url",
          "scripts_scanned", "scripts_failed",
          "findings": [
            {"type", "value" (truncated 80), "script", "sample" (50 chars around match)},
            ...
          ],
          "by_type": {"aws_access_key": N, "github_token": M, ...},
          "secret_count": int,
        }
        """
        import re as _re
        from urllib.parse import urljoin
        import httpx

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        # secret patterns: (name, regex, group_idx_for_value)
        patterns: list[tuple[str, _re.Pattern[str], int]] = [
            ("aws_access_key", _re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
            ("github_token",   _re.compile(r"\b(gh[ps]_[A-Za-z0-9]{36})\b"), 1),
            ("slack_token",    _re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"), 1),
            ("google_api_key", _re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"), 1),
            ("bearer",         _re.compile(r"Bearer\s+([A-Za-z0-9._\-]{20,})"), 1),
            ("api_key",        _re.compile(r"""api[_-]?key["']?\s*[:=]\s*["']?([A-Za-z0-9_\-]{16,})""", _re.IGNORECASE), 1),
            ("password",       _re.compile(r"""(?:password|passwd|pwd)\s*[:=]\s*["']([^"']{6,})["']""", _re.IGNORECASE), 1),
            ("private_key",    _re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), 0),
        ]

        findings: list[dict[str, Any]] = []
        scripts_scanned = 0
        scripts_failed = 0
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:max_body]
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue
                for name, pat, g in patterns:
                    for m in pat.finditer(body):
                        val = m.group(g) if g else m.group(0)
                        start = max(0, m.start() - 30)
                        end = min(len(body), m.end() + 30)
                        sample = body[start:end].replace("\n", " ")
                        findings.append({
                            "type": name,
                            "value": (val or "")[:80],
                            "script": url,
                            "sample": sample[:120],
                        })

        # dedup by (type, value, script)
        seen = set()
        uniq = []
        for f in findings:
            k = (f["type"], f["value"], f["script"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(f)

        by_type: dict[str, int] = {}
        for f in uniq:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "findings": uniq,
            "by_type": by_type,
            "secret_count": len(uniq),
        }

    # ── T43c: WAF 指纹 ────────────────────────────────

    async def detect_waf(self) -> dict[str, Any]:
        """T43c: WAF 指纹 — 综合 response headers / cookies / 页面内容.

        检测对象: Cloudflare, Akamai, Imperva, AWS WAF, Fastly, Vercel, Netlify, Sucuri.

        Returns {
          "page_url",
          "detected": [waf_name, ...],     # 可能多个 (WAF 链)
          "signals": [{waf, indicator, value, kind: "header"|"cookie"|"content"}],
          "confidence": "high" | "medium" | "low" | "none",
        }
        """
        page = await self._ensure_page()
        # 1) 当前页的 response headers (来自最近一次请求)
        try:
            resp = await page.request.fetch(page.url, method="GET", max_redirects=5)
            headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        except Exception:
            headers = {}
        # 也加上 document 的 main resource headers
        try:
            main_resource = await page.evaluate("() => performance.getEntriesByType('navigation')[0] || {}")
            for k, v in (main_resource or {}).items():
                if isinstance(v, str) and ":" in v:
                    pass  # not standard headers
        except Exception:
            pass

        # 2) cookies
        try:
            cookies = await self._context.cookies()
            cookie_names = {c.get("name", "").lower() for c in cookies}
        except Exception:
            cookie_names = set()

        # 3) 页面内容 (title + meta)
        try:
            content = await page.content()
        except Exception:
            content = ""
        content_lower = content[:20000].lower()

        # WAF signatures: (name, header_pattern, cookie_pattern, content_pattern)
        # pattern = None means skip
        waf_sigs: list[tuple[str, str | None, str | None, str | None]] = [
            ("Cloudflare", r"cloudflare|cf-ray|cf-cache-status", r"__cfuid|cf_clearance|cf_bm", r"cloudflare"),
            ("Akamai",     r"akamai|x-akamai", r"_abck|ak_bmsc|bmuid", r"akamai"),
            ("Imperva",    r"x-iinfo|x-cdn|incapsula", r"incap_ses|visid_incap|nlbi_", r"incapsula|imperva"),
            ("AWS WAF",    r"x-amzn-waf|awsalb|awselb|x-amz-cf-id", r"awsalb|awselb", None),
            ("Fastly",     r"x-served-by|x-fastly|x-fasto", r"fastly", None),
            ("Vercel",     r"x-vercel-id|server:\s*vercel", r"__vercel", None),
            ("Netlify",    r"server:\s*netlify|x-nf-request-id", r"netlify", None),
            ("Sucuri",     r"x-sucuri-id|x-sucuri-cache", r"sucuri", r"sucuri"),
            ("CloudFront", r"x-amz-cf-id|x-amz-cf-pop|via:\s*cloudfront", None, None),
            ("Wordfence",  None, r"wfwaf-authcookie|wfvt_", r"wordfence"),
        ]

        signals: list[dict[str, str]] = []
        detected: list[str] = []
        import re as _re
        for waf, hp, cp, ctp in waf_sigs:
            hit = False
            if hp:
                for k, v in headers.items():
                    if _re.search(hp, f"{k}: {v}", _re.IGNORECASE):
                        signals.append({"waf": waf, "indicator": f"{k}: {v[:60]}", "kind": "header"})
                        hit = True
                        break
            if not hit and cp:
                for cn in cookie_names:
                    if _re.search(cp, cn, _re.IGNORECASE):
                        signals.append({"waf": waf, "indicator": f"cookie: {cn}", "kind": "cookie"})
                        hit = True
                        break
            if not hit and ctp and _re.search(ctp, content_lower, _re.IGNORECASE):
                signals.append({"waf": waf, "indicator": f"content match: {ctp}", "kind": "content"})
                hit = True
            if hit:
                detected.append(waf)

        if len(detected) >= 2:
            confidence = "high"
        elif len(detected) == 1:
            # 多个 signals → high, 单 signal → medium
            waf_signals = [s for s in signals if s["waf"] == detected[0]]
            confidence = "high" if len(waf_signals) >= 2 else "medium"
        else:
            confidence = "none"
        return {
            "page_url": page.url,
            "detected": detected,
            "signals": signals,
            "confidence": confidence,
        }

    # ── T43d: 开放重定向 / SSRF sink 检测 ─────────────────

    async def find_open_redirect_sinks(self) -> dict[str, Any]:
        """T43d: 扫页面所有链接 + form action, 找可能开放重定向/SSRF 的参数.

        Sink params: returnUrl, redirect, url, next, return, return_to, continue,
                     back, target, redir, redirect_uri, callback, image, fetch
        Sink 路径:   /api/redirect?url=, /login?next=, /logout?redirect=
        Returns {
          "page_url",
          "sinks": [
            {"source": "link" | "form", "href": "...", "param": "next", "value": "/dashboard"},
            ...
          ],
          "sink_count": int,
        }
        """
        import re as _re
        page = await self._ensure_page()
        # 抓所有 link href + form action
        links = await page.evaluate("""() => {
            const out = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.getAttribute('href');
                if (h) out.push(h);
            }
            for (const f of document.querySelectorAll('form[action]')) {
                const a = f.getAttribute('action');
                if (a) out.push(a);
            }
            return out;
        }""")
        # sink param names (lowercase)
        SINK_PARAMS = {
            "returnurl", "redirect", "url", "next", "return",
            "return_to", "continue", "back", "target", "redir",
            "redirect_uri", "callback", "image", "fetch", "site",
            "view", "page", "dest", "destination", "out",
        }
        # sink path patterns
        SINK_PATHS = _re.compile(r"/(?:api/redirect|login|logout|oauth/authorize|auth/callback)", _re.IGNORECASE)

        sinks: list[dict[str, str]] = []
        seen = set()
        for href in links:
            # 拆 query
            if "?" not in href:
                # 也看 path 模式
                if SINK_PATHS.search(href):
                    key = ("path", href)
                    if key not in seen:
                        seen.add(key)
                        sinks.append({"source": "path", "href": href[:300], "param": "path", "value": href[:120]})
                continue
            path_part, _, query = href.partition("?")
            try:
                from urllib.parse import parse_qs
                params = parse_qs(query)
            except Exception:
                continue
            for k, vals in params.items():
                if k.lower() in SINK_PARAMS:
                    v = vals[0] if vals else ""
                    key = (k, v, path_part[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    sinks.append({
                        "source": "query",
                        "href": href[:300],
                        "param": k,
                        "value": v[:200],
                    })
            if SINK_PATHS.search(path_part):
                key = ("path", path_part)
                if key not in seen:
                    seen.add(key)
                    sinks.append({
                        "source": "path",
                        "href": href[:300],
                        "param": "path",
                        "value": path_part[:200],
                    })
        return {
            "page_url": page.url,
            "sinks": sinks[:100],
            "sink_count": len(sinks),
        }

    # ── T43e: 敏感信息泄露扫描 ────────────────────────

    async def find_disclosure(self) -> dict[str, Any]:
        """T43e: 扫页面 HTML 找敏感泄露.

        检测:
          - email
          - 内网 IP (RFC1918 + 127.x + 169.254.x)
          - AWS access key (AKIA[0-9A-Z]{16})
          - GitHub token (gh*_*)
          - Private key header
          - debug 字符串 ("Stack trace", "Exception in", "DEBUG =", "Traceback")
          - 注释里的 TODO/FIXME/HACK/XXX

        Returns {
          "page_url",
          "findings": [{type, value, context}],
          "by_type": {email: N, internal_ip: M, ...},
        }
        """
        import re as _re
        page = await self._ensure_page()
        content = await page.content()

        patterns: list[tuple[str, _re.Pattern[str], int]] = [
            ("email",       _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), 0),
            ("internal_ip", _re.compile(r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|127\.\d+\.\d+\.\d+|169\.254\.\d+\.\d+)\b"), 0),
            ("aws_key",     _re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
            ("github_tok",  _re.compile(r"\b(gh[ps]_[A-Za-z0-9]{36})\b"), 1),
            ("private_key", _re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), 0),
            ("debug_str",   _re.compile(r"(?i)(?:stack trace|traceback \(most recent|exception in|debug\s*=\s*True|tb_last)"), 0),
            ("code_marker", _re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]"), 0),
        ]

        findings: list[dict[str, str]] = []
        seen = set()
        for name, pat, g in patterns:
            for m in pat.finditer(content):
                val = m.group(g) if g else m.group(0)
                start = max(0, m.start() - 30)
                end = min(len(content), m.end() + 30)
                ctx = content[start:end].replace("\n", " ")[:120]
                k = (name, val[:80])
                if k in seen:
                    continue
                seen.add(k)
                findings.append({
                    "type": name,
                    "value": (val or "")[:120],
                    "context": ctx,
                })
        by_type: dict[str, int] = {}
        for f in findings:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        return {
            "page_url": page.url,
            "findings": findings[:200],
            "by_type": by_type,
            "finding_count": len(findings),
        }

    # ── T43f: 备份/源码/配置文件暴露分析 ───────────────

    async def analyze_exposed_files(
        self,
        base_url: str | None = None,
        *,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T43f: 探常见备份/源码/配置文件, 解析暴露内容.

        探针:
          /.git/HEAD, /.git/config, /.svn/entries
          /.env, /.env.local, /.env.production
          /.DS_Store, /Thumbs.db
          /backup.zip, /backup.tar.gz, /dump.sql, /db.sqlite
          /phpinfo.php, /server-status, /server-info
          /wp-config.php.bak, /config.php.bak, /config.yml.bak

        Returns {
          "base_url",
          "exposed": [
            {"path", "status", "size", "kind": "git"|"env"|"backup"|"config"|"other", "info": {...}}
          ],
          "exposed_count": int,
        }
        """
        import re as _re
        from urllib.parse import urlparse
        import httpx

        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"

        PROBES = [
            ("/.git/HEAD", "git"),
            ("/.git/config", "git"),
            ("/.svn/entries", "svn"),
            ("/.env", "env"),
            ("/.env.local", "env"),
            ("/.env.production", "env"),
            ("/.DS_Store", "macos"),
            ("/Thumbs.db", "windows"),
            ("/backup.zip", "backup"),
            ("/backup.tar.gz", "backup"),
            ("/dump.sql", "backup"),
            ("/db.sqlite", "backup"),
            ("/db.sqlite3", "backup"),
            ("/phpinfo.php", "phpinfo"),
            ("/server-status", "apache"),
            ("/server-info", "apache"),
            ("/wp-config.php.bak", "config"),
            ("/config.php.bak", "config"),
            ("/config.yml.bak", "config"),
            ("/.htaccess", "htaccess"),
            ("/web.config", "config"),
        ]

        exposed: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-recon/1.0"},
            follow_redirects=False,
        ) as client:
            for path, kind in PROBES:
                url = origin + path
                try:
                    r = await client.get(url)
                except Exception:
                    continue
                if r.status_code >= 400:
                    continue
                body = r.content[:20000]
                size = len(body)
                info: dict[str, Any] = {}
                if kind == "git":
                    text = body.decode("utf-8", errors="ignore").strip()
                    info["ref"] = text[:120]
                    if text.startswith("ref:"):
                        info["branch"] = text.split("/")[-1].strip()
                elif kind == "env":
                    text = body.decode("utf-8", errors="ignore")
                    # 只列 key 不列 value (避免误报出真密码)
                    keys = []
                    for line in text.splitlines():
                        if "=" in line and not line.strip().startswith("#"):
                            k = line.split("=", 1)[0].strip()
                            if k and _re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                                keys.append(k)
                    info["key_count"] = len(keys)
                    info["keys_sample"] = keys[:10]
                elif kind == "phpinfo":
                    text = body.decode("utf-8", errors="ignore")
                    v = _re.search(r"PHP Version\s*=>\s*([\d.]+)", text)
                    if v:
                        info["php_version"] = v.group(1)
                    else:
                        info["php_version"] = "unknown"
                elif kind == "apache":
                    text = body.decode("utf-8", errors="ignore")
                    if "Apache" in text:
                        info["server"] = "Apache (status page exposed)"
                elif kind in ("backup", "svn", "macos", "windows", "config", "htaccess"):
                    text = body.decode("utf-8", errors="ignore")
                    if kind == "htaccess":
                        # 只看第一行 (RewriteRule / Deny / AuthType)
                        info["first_line"] = text.splitlines()[0][:120] if text else ""
                exposed.append({
                    "path": path,
                    "status": r.status_code,
                    "size": size,
                    "kind": kind,
                    "info": info,
                })
        return {
            "base_url": base_url,
            "exposed": exposed,
            "exposed_count": len(exposed),
        }

    # ── T43g: OpenAPI / Swagger 自动发现 + 解析 ────────────

    async def discover_api_specs(
        self,
        base_url: str | None = None,
        *,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T43g: 探常见 OpenAPI / Swagger 路径, 解析 path + method.

        探针:
          /swagger.json, /openapi.json, /api/swagger.json,
          /api/openapi.json, /api/v1/openapi.json, /api/v2/openapi.json,
          /v3/api-docs, /api-docs, /swagger/v1/swagger.json

        Returns {
          "base_url",
          "specs": [
            {"url", "version", "title", "path_count", "method_count", "by_method": {GET:N,POST:M}}
          ],
          "spec_count": int,
        }
        """
        import json as _json
        from urllib.parse import urlparse
        import httpx

        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"

        PROBES = [
            "/swagger.json", "/openapi.json",
            "/api/swagger.json", "/api/openapi.json",
            "/api/v1/openapi.json", "/api/v2/openapi.json",
            "/api/v1/swagger.json",
            "/v3/api-docs", "/api-docs",
            "/swagger/v1/swagger.json",
        ]

        specs: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-recon/1.0", "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for path in PROBES:
                url = origin + path
                try:
                    r = await client.get(url)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    doc = r.json()
                except Exception:
                    continue
                if not isinstance(doc, dict):
                    continue
                # OpenAPI 3: doc.get("openapi").startswith("3.")
                # Swagger 2:  doc.get("swagger") == "2.0"
                is_spec = (
                    (isinstance(doc.get("openapi"), str) and doc["openapi"].startswith("3."))
                    or doc.get("swagger") == "2.0"
                    or (isinstance(doc.get("paths"), dict) and doc["paths"])
                )
                if not is_spec:
                    continue
                paths = doc.get("paths", {}) or {}
                by_method: dict[str, int] = {}
                for p, ops in paths.items():
                    if isinstance(ops, dict):
                        for m in ops:
                            if m.lower() in ("get", "post", "put", "delete", "patch", "options", "head"):
                                by_method[m.upper()] = by_method.get(m.upper(), 0) + 1
                info = doc.get("info", {}) or {}
                specs.append({
                    "url": url,
                    "version": doc.get("openapi") or doc.get("swagger") or "unknown",
                    "title": info.get("title", ""),
                    "path_count": len(paths),
                    "method_count": sum(by_method.values()),
                    "by_method": by_method,
                    "sample_paths": list(paths.keys())[:5],
                })
        return {
            "base_url": base_url,
            "specs": specs,
            "spec_count": len(specs),
        }

    # ── T43h: TLS 证书 SAN → 子域 ──────────────────────

    async def tls_subdomains(self, host: str, port: int = 443, timeout: float = 5.0) -> dict[str, Any]:
        """T43h: TLS 证书 SAN 解析 — 取 subjectAltName / issuer / 有效期.

        Returns {
          "host", "tls_version", "issuer" (str), "not_before", "not_after",
          "sans" (sorted unique DNS list), "san_count",
          "subdomains" (sans ending with host),
        }
        """
        from datetime import datetime, timezone
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
                    cert = ss.getpeercert(binary_form=False) or {}
                    tls_version = ss.version()
            # parse SANs from binary form via regex on DER (subjectAltName ext OID 2.5.29.17)
            import re as _re
            sans = []
            # 1) 优先用 binary_form=True 的 SAN
            try:
                for entry in cert.get("subjectAltName", []):
                    if entry and entry[0].lower() == "dns":
                        sans.append(entry[1].lower())
            except Exception:
                pass
            # 2) fallback: parse DER bytes for SAN extension (crude: 找 DNS: 后的 host)
            if not sans and der:
                text = der.decode("latin-1", errors="ignore")
                # 找 DNS: 后的 fqdn 字符
                for m in _re.finditer(r"DNS:([A-Za-z0-9._-]+)", text):
                    sans.append(m.group(1).lower())
            sans = sorted(set(sans))
            # issuer
            issuer = ""
            try:
                iret = cert.get("issuer", ())
                if iret:
                    parts = []
                    for tup in iret:
                        for k, v in tup:
                            if k == "commonName":
                                parts.append(v)
                            elif k == "organizationName":
                                parts.insert(0, v)
                    issuer = ", ".join(parts)
            except Exception:
                pass
            # not_before / not_after → ISO
            def _parse_dt(s: str | None) -> str | None:
                if not s:
                    return None
                try:
                    return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).isoformat()
                except Exception:
                    return s
            subs = sorted({s for s in sans if s == host or s.endswith("." + host)})
            return {
                "host": host,
                "tls_version": tls_version,
                "issuer": issuer,
                "not_before": _parse_dt(cert.get("notBefore")),
                "not_after": _parse_dt(cert.get("notAfter")),
                "sans": sans,
                "san_count": len(sans),
                "subdomains": subs,
            }
        except Exception as e:
            return {
                "host": host,
                "error": str(e)[:200],
                "sans": [],
                "san_count": 0,
                "subdomains": [],
            }

    # ── T43i: 技术栈指纹 ────────────────────────────────

    async def fingerprint_tech(self) -> dict[str, Any]:
        """T43i: 综合 meta / cookie / header 推断技术栈.

        检测:
          - Server / X-Powered-By / X-AspNet-Version / X-Runtime (Rails)
          - meta name=generator (WordPress / Drupal / Ghost 版本)
          - 框架 session cookie: PHPSESSID, JSESSIONID, ASP.NET_SessionId, _rails_session, connect.sid, JSESSIONID
          - 已知 meta name 模式

        Returns {
          "page_url",
          "server": str, "x_powered_by": str, "generator": str,
          "framework_cookies": [name, ...],
          "signals": [{kind, name, value, hint}],
        }
        """
        import re as _re
        page = await self._ensure_page()
        # 1) headers from current page response
        try:
            resp = await page.request.fetch(page.url, method="GET", max_redirects=5)
            headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        except Exception:
            headers = {}
        # 2) meta generator
        generator = ""
        try:
            generator = await page.evaluate("""() => {
                const m = document.querySelector('meta[name="generator"]');
                return m ? m.getAttribute('content') || '' : '';
            }""")
        except Exception:
            generator = ""
        # 3) cookies
        try:
            cookies = await self._context.cookies()
            cookie_names = [c.get("name", "") for c in cookies]
        except Exception:
            cookie_names = []

        # 框架 cookie 签名
        FRAMEWORK_COOKIE_HINTS = {
            "PHPSESSID": "PHP",
            "JSESSIONID": "Java (Tomcat/jetty)",
            "ASP.NET_SessionId": "ASP.NET",
            "_rails_session": "Ruby on Rails",
            "connect.sid": "Express (Node.js)",
            "sessionid": "Django",
            "csrftoken": "Django / generic",
            "laravel_session": "Laravel (PHP)",
            "XSRF-TOKEN": "Laravel / generic",
            "wp-settings-": "WordPress",
            "wordpress_logged_in": "WordPress",
            "ghost": "Ghost (blog)",
            "shopify_session": "Shopify",
            "mage-cache-storage": "Magento",
        }
        framework_cookies: list[dict[str, str]] = []
        for cn in cookie_names:
            cn_l = cn.lower()
            for sig, hint in FRAMEWORK_COOKIE_HINTS.items():
                if sig.lower() in cn_l:
                    framework_cookies.append({"name": cn, "hint": hint})
                    break

        signals: list[dict[str, str]] = []
        srv = headers.get("server", "")
        if srv:
            signals.append({"kind": "header", "name": "server", "value": srv, "hint": _server_hint(srv)})
        xpb = headers.get("x-powered-by", "")
        if xpb:
            signals.append({"kind": "header", "name": "x-powered-by", "value": xpb, "hint": xpb})
        aspv = headers.get("x-aspnet-version") or headers.get("x-aspnetmvc-version")
        if aspv:
            signals.append({"kind": "header", "name": "x-aspnet-version", "value": aspv, "hint": "ASP.NET"})
        runtime = headers.get("x-runtime", "")
        if runtime:
            signals.append({"kind": "header", "name": "x-runtime", "value": runtime, "hint": "Ruby/Rails"})
        if generator:
            signals.append({"kind": "meta", "name": "generator", "value": generator, "hint": _generator_hint(generator)})
        for fc in framework_cookies:
            signals.append({"kind": "cookie", "name": fc["name"], "value": "", "hint": fc["hint"]})
        return {
            "page_url": page.url,
            "server": srv,
            "x_powered_by": xpb,
            "generator": generator,
            "framework_cookies": framework_cookies,
            "signals": signals,
        }

    # ── T43j: JWT 探测 + payload 解码 (无签名校验) ────────────

    async def decode_jwts(self) -> dict[str, Any]:
        """T43j: 在 localStorage / sessionStorage / cookie / 页面内容中找 JWT, 解码 payload.

        JWT 格式: header.payload.signature (base64url 编码)
        解码 header + payload (不做签名校验, 仅供 agent 看清结构).
        Returns {
          "page_url",
          "tokens": [
            {"source": "localStorage"|"cookie"|"page", "key": "name", "token": "...", "header": {...}, "payload": {...}, "is_expired": bool}
          ],
          "token_count": int,
        }
        """
        import re as _re
        import base64
        import json as _json
        page = await self._ensure_page()
        # 1) storage
        storage = await self.get_storage()
        # 2) page content (HTML + inline scripts)
        try:
            content = await page.content()
        except Exception:
            content = ""
        # 3) cookies
        cookies = storage.get("cookies", []) or []

        def _b64url_decode(s: str) -> bytes | None:
            try:
                pad = "=" * (-len(s) % 4)
                return base64.urlsafe_b64decode(s + pad)
            except Exception:
                return None

        JWT_RE = _re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,})\.(eyJ[A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\b")
        tokens: list[dict[str, Any]] = []
        seen = set()

        def _record(source: str, key: str, token: str) -> None:
            if token in seen:
                return
            seen.add(token)
            h, p, s = token.split(".", 2)
            header_raw = _b64url_decode(h)
            payload_raw = _b64url_decode(p)
            try:
                header = _json.loads(header_raw) if header_raw else {}
            except Exception:
                header = {"_raw": header_raw.decode("utf-8", errors="ignore")[:80]}
            try:
                payload = _json.loads(payload_raw) if payload_raw else {}
            except Exception:
                payload = {"_raw": payload_raw.decode("utf-8", errors="ignore")[:200]}
            expired = False
            if isinstance(payload, dict):
                exp = payload.get("exp")
                if isinstance(exp, (int, float)):
                    import time as _t
                    expired = exp < _t.time()
            tokens.append({
                "source": source,
                "key": key,
                "token": token[:80] + ("..." if len(token) > 80 else ""),
                "header": header,
                "payload": payload,
                "is_expired": expired,
            })

        # localStorage / sessionStorage
        for kind in ("localStorage", "sessionStorage"):
            for k, v in (storage.get(kind) or {}).items():
                for m in JWT_RE.finditer(v or ""):
                    _record(kind, k, m.group(0))
        # cookies (value 直接是 JWT 或含 JWT)
        for c in cookies:
            v = c.get("value", "") or ""
            for m in JWT_RE.finditer(v):
                _record("cookie", c.get("name", ""), m.group(0))
        # page content
        for m in JWT_RE.finditer(content):
            _record("page", "(html)", m.group(0))

        return {
            "page_url": page.url,
            "tokens": tokens,
            "token_count": len(tokens),
        }

    # ── T44a: DNS 记录 (A/AAAA/MX/NS/TXT-SPF/TXT-DMARC) ────────

    async def dns_records(
        self,
        host: str,
        *,
        doh_endpoint: str = "https://dns.google/resolve",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """T44a: DNS 记录查询 — 用 DoH (DNS-over-HTTPS) 避开 dig 依赖.

        查询类型: A / AAAA / MX / NS / TXT (SPF 提取) / _dmarc.<host>.TXT (DMARC 提取).
        Returns {
          "host",
          "a":       [ip, ...],
          "aaaa":    [ip, ...],
          "mx":      [{priority, exchange}, ...],
          "ns":      [ns_host, ...],
          "spf":     [spf_record, ...] (从 TXT 提取 v=spf1),
          "dmarc":   [dmarc_record, ...] (从 _dmarc.<host>.TXT),
          "security_grade": "ok" | "weak" | "missing"   (spf + dmarc + mx 综合),
          "notes":   [str, ...]  (pen-tester 视角的解读),
          "errors":  {rtype: err, ...}  (部分失败不阻塞)
        }
        """
        import re as _re
        import httpx

        async def _query(rtype: str, qname: str) -> list[dict[str, Any]]:
            url = f"{doh_endpoint}?name={qname}&type={rtype}"
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, headers={"Accept": "application/dns-json"})
            if r.status_code != 200:
                raise RuntimeError(f"DoH status={r.status_code}")
            doc = r.json()
            return doc.get("Answer", []) or []

        result: dict[str, Any] = {
            "host": host,
            "a": [],
            "aaaa": [],
            "mx": [],
            "ns": [],
            "spf": [],
            "dmarc": [],
            "security_grade": "ok",
            "notes": [],
            "errors": {},
        }

        async def _safe(rtype: str, qname: str) -> list[dict[str, Any]]:
            try:
                return await _query(rtype, qname)
            except Exception as e:
                result["errors"][rtype] = str(e)[:200]
                return []

        # A
        for ans in await _safe("A", host):
            if ans.get("type") == 1:
                result["a"].append(ans.get("data", ""))
        # AAAA
        for ans in await _safe("AAAA", host):
            if ans.get("type") == 28:
                result["aaaa"].append(ans.get("data", ""))
        # MX
        for ans in await _safe("MX", host):
            if ans.get("type") == 15:
                data = ans.get("data", "")
                # format: "10 mail.example.com."
                parts = data.split(None, 1)
                if len(parts) == 2:
                    result["mx"].append({"priority": int(parts[0]), "exchange": parts[1].rstrip(".")})
        # NS
        for ans in await _safe("NS", host):
            if ans.get("type") == 2:
                result["ns"].append(ans.get("data", "").rstrip("."))
        # TXT — 提取 SPF
        for ans in await _safe("TXT", host):
            if ans.get("type") == 16:
                data = ans.get("data", "").strip('"')
                if data.lower().startswith("v=spf1"):
                    result["spf"].append(data)
        # DMARC
        for ans in await _safe("TXT", f"_dmarc.{host}"):
            if ans.get("type") == 16:
                data = ans.get("data", "").strip('"')
                if data.lower().startswith("v=dmarc1"):
                    result["dmarc"].append(data)

        # 解读
        if not result["mx"]:
            result["notes"].append("no MX — 域名不收邮件 (或不接受 SMTP)")
        if not result["spf"]:
            result["notes"].append("no SPF — 邮件伪造无任何 SPF 防线")
        else:
            # 检查 SPF 是否 -all (硬失败)
            spf0 = result["spf"][0]
            if "~all" in spf0:
                result["notes"].append("SPF ends with ~all (softfail) — 伪造邮件更易通过")
            elif " -all" not in spf0 and "-all" not in spf0:
                result["notes"].append("SPF 不含 -all — 末尾策略弱, 易被绕过")
        if not result["dmarc"]:
            result["notes"].append("no DMARC — 无报告/无拒绝策略")
        else:
            d0 = result["dmarc"][0].lower()
            if "p=none" in d0 or "p=monitor" in d0:
                result["notes"].append("DMARC p=none — 不拒绝不合规邮件 (监控模式)")
            elif "p=quarantine" in d0:
                result["notes"].append("DMARC p=quarantine — 隔离不合规邮件")
            elif "p=reject" in d0:
                result["notes"].append("DMARC p=reject — 完全拒绝不合规邮件 (最好)")

        # 安全分
        score = 0
        if result["spf"]:
            score += 1
        if result["dmarc"]:
            score += 1
            d0 = result["dmarc"][0].lower()
            if "p=reject" in d0:
                score += 1
        if result["mx"]:
            score += 1
        if score <= 1:
            result["security_grade"] = "missing"
        elif score == 2:
            result["security_grade"] = "weak"
        else:
            result["security_grade"] = "ok"
        return result

    # ── T44b: Wayback Machine 历史 URL ─────────────────

    async def wayback_urls(
        self,
        url: str,
        *,
        limit: int = 200,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """T44b: Wayback Machine 历史 URL 探测.

        查 web.archive.org/web/timemap/link/<url> — 返回该 URL 在历史上的所有快照的指向 URL.
        pen-tester 视角: 旧端点 / 旧 secret / 旧 API 常在历史快照里没清理.

        Returns {
          "url",
          "snapshot_count": int,
          "unique_targets": [url, ...],  # 去重
          "first_snapshot": str | None,  # 最早一条
          "last_snapshot": str | None,
          "samples": [url, ...] (前 10),
        }
        """
        from urllib.parse import quote as _q
        import httpx
        target = f"https://web.archive.org/web/timemap/link/{_q(url, safe='/:?=&')}"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                r = await client.get(target, headers={"User-Agent": "semantic-browser-recon/1.0"})
            if r.status_code != 200:
                return {"url": url, "snapshot_count": 0, "unique_targets": [],
                        "first_snapshot": None, "last_snapshot": None, "samples": [],
                        "error": f"status={r.status_code}"}
            lines = r.text.splitlines()
            # timemap 格式: <timestamp> <original_url> <mimetype> "<target_url>"
            # 跳过 header (前 2 行)
            targets: list[str] = []
            for line in lines[2:]:
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    target_url = parts[3].strip('"').strip()
                    if target_url:
                        targets.append(target_url[:500])
            uniq = list(dict.fromkeys(targets))  # 保序去重
            uniq_limited = uniq[:limit]
            return {
                "url": url,
                "snapshot_count": len(targets),
                "unique_targets": uniq_limited,
                "unique_target_count": len(uniq),
                "first_snapshot": targets[0] if targets else None,
                "last_snapshot": targets[-1] if targets else None,
                "samples": uniq_limited[:10],
            }
        except Exception as e:
            return {"url": url, "snapshot_count": 0, "unique_targets": [],
                    "first_snapshot": None, "last_snapshot": None, "samples": [],
                    "error": str(e)[:200]}

    # ── T44c: DOM XSS sinks in JS source ──────────────

    async def find_xss_sinks(
        self,
        *,
        max_scripts: int = 15,
        max_body: int = 100_000,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        r"""T44c: 扫页面所有 <script src> 源码, 找 DOM XSS sinks.

        检测 sinks:
          - eval(                          (eval arbitrary string)
          - new Function(                  (function constructor)
          - innerHTML\s*=                  (HTML injection)
          - outerHTML\s*=
          - document.write(                (DOM write)
          - document.writeln(
          - setTimeout("...", )            (string form, not function)
          - setInterval("...", )           (string form)
          - .insertAdjacentHTML(
          - location\s*=                   (location override)
          - window.location\s*=
          - location.href\s*=
          - document.cookie                (sensitive read)
          - .src\s*=\s*location            (URL injection)

        Returns {
          "page_url", "scripts_scanned", "scripts_failed",
          "findings": [{sink, count, script, samples: [snippet, ...]}],
          "by_sink": {sink: total_count},
          "sink_count": int,
        }
        """
        import re as _re
        from urllib.parse import urljoin
        import httpx

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        SINK_PATTERNS = [
            ("eval",                   r"\beval\s*\("),
            ("function_constructor",   r"\bnew\s+Function\s*\("),
            ("innerHTML",              r"\.innerHTML\s*="),
            ("outerHTML",              r"\.outerHTML\s*="),
            ("document.write",         r"\bdocument\.write(?:ln)?\s*\("),
            ("setTimeout_string",      r"\bsetTimeout\s*\(\s*['\"]"),
            ("setInterval_string",     r"\bsetInterval\s*\(\s*['\"]"),
            ("insertAdjacentHTML",     r"\.insertAdjacentHTML\s*\("),
            ("location_assignment",    r"\b(?:window\.)?location(?:\.href)?\s*="),
            ("document.cookie_read",   r"\bdocument\.cookie\b"),
            ("src_from_location",      r"\.src\s*=\s*(?:window\.)?location"),
        ]

        findings: list[dict[str, Any]] = []
        scripts_scanned = 0
        scripts_failed = 0
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:max_body]
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue
                for name, pat in SINK_PATTERNS:
                    matches = list(_re.finditer(pat, body))
                    if not matches:
                        continue
                    samples = []
                    for m in matches[:3]:
                        start = max(0, m.start() - 30)
                        end = min(len(body), m.end() + 30)
                        samples.append(body[start:end].replace("\n", " ")[:120])
                    findings.append({
                        "sink": name,
                        "count": len(matches),
                        "script": url,
                        "samples": samples,
                    })

        by_sink: dict[str, int] = {}
        for f in findings:
            by_sink[f["sink"]] = by_sink.get(f["sink"], 0) + f["count"]
        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "findings": findings,
            "by_sink": by_sink,
            "sink_count": len(findings),
        }

    # ── T44d: CAPTCHA + OAuth provider detection ─────────

    async def detect_auth_methods(self) -> dict[str, Any]:
        """T44d: 检测页面里出现的 auth/CAPTCHA/OAuth 组件.

        检测:
          - reCAPTCHA v2/v3 (grecaptcha.render / google.com/recaptcha)
          - hCaptcha
          - Cloudflare Turnstile
          - FunCaptcha / Arkose Labs
          - Google OAuth
          - GitHub OAuth
          - Facebook OAuth
          - Apple OAuth
          - Microsoft OAuth
          - Twitter/X OAuth
          - WebAuthn / Passkey
          - Magic link / passwordless (含 "magic link" 文字)
          - SAML (saml/acs/SingleSignOn)

        Returns {
          "page_url",
          "captcha": [name, ...],
          "oauth_providers": [name, ...],
          "mfa": [name, ...],   # 2FA / MFA 信号 (WebAuthn, TOTP, SMS, backup)
          "sso": [name, ...],   # SAML / OIDC generic
          "signals": [{kind, name, hint}],
        }
        """
        import re as _re
        page = await self._ensure_page()
        try:
            content = await page.content()
        except Exception:
            content = ""
        # 也看脚本 src (CDN 引用可能没在 inline HTML 里)
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        script_blob = " ".join(scripts)
        combined = (content + " " + script_blob)[:50000]

        captcha_sigs = [
            (r"grecaptcha\.render|google\.com/recaptcha|Recaptcha\.create", "reCAPTCHA v2/v3"),
            (r"hcaptcha\.com|hcaptcha\.render|h-captcha", "hCaptcha"),
            (r"challenges\.cloudflare\.com/turnstile|cf-turnstile", "Cloudflare Turnstile"),
            (r"funcaptcha|arkoselabs|arkose\.com", "FunCaptcha/Arkose"),
        ]
        oauth_sigs = [
            (r"Sign in with Google|accounts\.google\.com/o/oauth|gsi/client", "Google"),
            (r"Sign in with GitHub|github\.com/login/oauth", "GitHub"),
            (r"Sign in with Facebook|facebook\.com/v\d+\.\d+/dialog/oauth|fbcdn\.net", "Facebook"),
            (r"Sign in with Apple|appleid\.apple\.com|appleid\.sdk", "Apple"),
            (r"Sign in with Microsoft|login\.microsoftonline\.com|msal", "Microsoft"),
            (r"Sign in with Twitter|Sign in with X|twitter\.com/oauth|x\.com/oauth", "Twitter/X"),
            (r"Sign in with Discord|discord\.com/oauth2", "Discord"),
            (r"Sign in with LinkedIn|linkedin\.com/oauth", "LinkedIn"),
        ]
        mfa_sigs = [
            (r"webauthn|public[-_]key[-_]credential|navigator\.credentials", "WebAuthn/Passkey"),
            (r"totp|google[-_]authenticator|authy|1password|2fa|two[-_]factor|authenticator", "TOTP-based 2FA"),
            (r"sms[-_]code|verification[-_]code|2fa[-_]sms", "SMS 2FA"),
            (r"backup[-_]code|recovery[-_]code", "Backup codes"),
            (r"duo[-_]factor|duo\.com", "Duo 2FA"),
        ]
        sso_sigs = [
            (r"/saml/acs|/|saml2|SAMLResponse|SPEntityID|IdPEntityID", "SAML SSO"),
            (r"/oidc|/oauth2/authorize|openid-connect", "OIDC/OAuth2 generic"),
        ]

        captcha = [name for pat, name in captcha_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        oauth = [name for pat, name in oauth_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        mfa = [name for pat, name in mfa_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        sso = [name for pat, name in sso_sigs if _re.search(pat, combined, _re.IGNORECASE)]

        signals: list[dict[str, str]] = []
        for n in captcha:
            signals.append({"kind": "captcha", "name": n, "hint": "Bot protection"})
        for n in oauth:
            signals.append({"kind": "oauth", "name": n, "hint": "OAuth provider"})
        for n in mfa:
            signals.append({"kind": "mfa", "name": n, "hint": "Multi-factor auth"})
        for n in sso:
            signals.append({"kind": "sso", "name": n, "hint": "SSO protocol"})
        return {
            "page_url": page.url,
            "captcha": captcha,
            "oauth_providers": oauth,
            "mfa": mfa,
            "sso": sso,
            "signals": signals,
        }

    # ── T44e: CSRF 覆盖率检查 ─────────────────────

    async def check_csrf_coverage(self) -> dict[str, Any]:
        """T44e: 对当前页每个 form 检查 CSRF token 是否存在.

        CSRF token 字段名: csrf_token, authenticity_token, _csrf, csrfmiddlewaretoken,
                           antiforgerytoken, __requestverificationtoken, _token, csrfToken
        只对会改变状态的 form (login/signup/checkout/contact/upload) 报警.

        Returns {
          "page_url",
          "form_count": int,
          "forms_with_csrf": int,
          "forms_without_csrf": int,
          "vulnerable": [{action, method, kind, field_names}],
        }
        """
        page = await self._ensure_page()
        snap = await SnapshotEngine(page).capture(base_url=page.url, detail_level="full")
        CSRF_NAMES = {
            "csrf_token", "authenticity_token", "_csrf", "csrfmiddlewaretoken",
            "antiforgerytoken", "__requestverificationtoken", "_token", "csrftoken",
            "csrfToken", "anti_csrf_token", "x-csrf-token", "csrf", "_csrf_token",
        }
        STATE_CHANGING = {"login", "signup", "checkout", "contact", "upload", "search", "unknown"}
        vulnerable: list[dict[str, Any]] = []
        for f in snap.forms:
            has_csrf = any(
                h.get("name", "").lower() in CSRF_NAMES
                for h in f.hidden_fields
            )
            if not has_csrf and f.classification in STATE_CHANGING:
                vulnerable.append({
                    "action": f.action[:200],
                    "method": f.method or "get",
                    "kind": f.classification,
                    "field_names": f.input_names[:10],
                })
        return {
            "page_url": page.url,
            "form_count": len(snap.forms),
            "forms_with_csrf": sum(
                1 for f in snap.forms
                if any(h.get("name", "").lower() in CSRF_NAMES for h in f.hidden_fields)
            ),
            "forms_without_csrf": len(vulnerable),
            "vulnerable": vulnerable,
        }

    # ── T44f: IDOR-prone URLs ──────────────────────

    async def find_idor_urls(self) -> dict[str, Any]:
        """T44f: 扫链接 + form action 找 IDOR-prone URLs.

        模式: /user/{N}, /users/{N}, /order/{N}, /orders/{N}, /invoice/{N},
              /account/{N}, /profile/{N}, /api/v1/users/{N}, /api/v1/orders/{N}, ...
        数字 ID (1-12 位) 视为可疑.
        Returns {
          "page_url",
          "idor_urls": [{href, kind, id}],
          "idor_count": int,
        }
        """
        import re as _re
        page = await self._ensure_page()
        snap = await SnapshotEngine(page).capture(base_url=page.url, detail_level="full")
        IDOR_RE = _re.compile(
            r"/(users?|orders?|invoices?|accounts?|profiles?|customers?|tickets?)"
            r"/(\d{1,12})(?:\b|/)",
            _re.IGNORECASE,
        )
        # 也加上常见的 API 路径
        API_RE = _re.compile(
            r"/api/v\d+/(users|orders|invoices|accounts)/(\d{1,12})(?:\b|/)",
            _re.IGNORECASE,
        )
        idor: list[dict[str, Any]] = []
        seen = set()
        for link in snap.links:
            for m in IDOR_RE.finditer(link.href or ""):
                key = (m.group(1).lower(), m.group(2), link.href[:200])
                if key in seen:
                    continue
                seen.add(key)
                idor.append({"href": link.href[:300], "kind": m.group(1).lower(), "id": m.group(2)})
        for f in snap.forms:
            for m in IDOR_RE.finditer(f.action or ""):
                key = (m.group(1).lower(), m.group(2), f.action[:200])
                if key in seen:
                    continue
                seen.add(key)
                idor.append({"href": f.action[:300], "kind": m.group(1).lower(), "id": m.group(2), "in_form": True})
        return {
            "page_url": page.url,
            "idor_urls": idor[:100],
            "idor_count": len(idor),
        }

    # ── T44g: 云资源泄露 (S3 / Azure / GCP / Heroku / Firebase) ─

    async def find_cloud_resources(self) -> dict[str, Any]:
        """T44g: 扫 page source + script srcs, 找云资源 URL 泄露.

        检测:
          - AWS S3:            <bucket>.s3.amazonaws.com / s3-<region>.amazonaws.com/<bucket>
          - Azure Blob:        <account>.blob.core.windows.net
          - Azure Files:       <account>.file.core.windows.net
          - GCP Storage:       storage.googleapis.com/<bucket>
          - Heroku:            <app>.herokuapp.com
          - Firebase DB:       <app>.firebaseio.com
          - Firebase Hosting:  <app>.web.app / <app>.firebaseapp.com
          - CloudFront:        <id>.cloudfront.net
          - DigitalOcean:      <bucket>.nyc3.digitaloceanspaces.com

        Returns {
          "page_url",
          "resources": [{provider, url, kind}],
          "by_provider": {aws_s3: N, azure_blob: M, ...},
        }
        """
        import re as _re
        page = await self._ensure_page()
        try:
            content = await page.content()
        except Exception:
            content = ""
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            for (const l of document.querySelectorAll('link[href]')) {
                const h = l.getAttribute('href');
                if (h) out.push(h);
            }
            return out;
        }""")
        blob = content + "\n" + "\n".join(scripts)
        PATTERNS = [
            ("aws_s3",         r"https?://[a-z0-9.\-]+\.s3(?:\.[a-z0-9\-]+)?\.amazonaws\.com[^\s\"'<>]*", "S3 bucket"),
            ("aws_s3_path",    r"https?://s3(?:\.[a-z0-9\-]+)?\.amazonaws\.com/[a-z0-9.\-]+[^\s\"'<>]*", "S3 path-style"),
            ("azure_blob",     r"https?://[a-z0-9]+\.blob\.core\.windows\.net[^\s\"'<>]*", "Azure Blob"),
            ("azure_file",     r"https?://[a-z0-9]+\.file\.core\.windows\.net[^\s\"'<>]*", "Azure Files"),
            ("gcp_storage",    r"https?://storage\.googleapis\.com/[a-z0-9.\-]+[^\s\"'<>]*", "GCP Storage"),
            ("heroku_app",     r"https?://[a-z0-9\-]+\.herokuapp\.com[^\s\"'<>]*", "Heroku app"),
            ("firebase_db",    r"https?://[a-z0-9\-]+\.firebaseio\.com[^\s\"'<>]*", "Firebase DB"),
            ("firebase_host",  r"https?://[a-z0-9\-]+\.(?:web\.app|firebaseapp\.com)[^\s\"'<>]*", "Firebase Hosting"),
            ("cloudfront",     r"https?://[a-z0-9]+\.cloudfront\.net[^\s\"'<>]*", "CloudFront"),
            ("do_spaces",      r"https?://[a-z0-9\-]+\.[a-z0-9]+\.digitaloceanspaces\.com[^\s\"'<>]*", "DigitalOcean Spaces"),
        ]
        resources: list[dict[str, str]] = []
        seen = set()
        for prov, pat, kind in PATTERNS:
            for m in _re.finditer(pat, blob, _re.IGNORECASE):
                url = m.group(0).rstrip(".,);\"'")
                if url in seen:
                    continue
                seen.add(url)
                resources.append({"provider": prov, "url": url[:300], "kind": kind})
        by_provider: dict[str, int] = {}
        for r in resources:
            by_provider[r["provider"]] = by_provider.get(r["provider"], 0) + 1
        return {
            "page_url": page.url,
            "resources": resources[:200],
            "by_provider": by_provider,
            "resource_count": len(resources),
        }

    # ── T44h: HTTP methods probe (OPTIONS / Allow) ───────────

    async def probe_http_methods(
        self,
        base_url: str | None = None,
        *,
        paths: list[str] | None = None,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T44h: OPTIONS 请求探测每个 path 的 Allow header, 看是否支持危险方法.

        Returns {
          "base_url",
          "results": [
            {"path", "allow" (parsed), "dangerous" (bool, 含 PUT/DELETE/PATCH/CONNECT/TRACE)},
            ...
          ],
        }
        """
        import httpx
        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"
        if not paths:
            paths = ["/", "/api", "/api/v1", "/users", "/admin", "/login"]
        DANGEROUS = {"PUT", "DELETE", "PATCH", "CONNECT", "TRACE"}
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=False,
        ) as client:
            for path in paths:
                url = origin + path
                try:
                    r = await client.request("OPTIONS", url)
                except Exception as e:
                    results.append({"path": path, "allow": [], "dangerous": False, "error": str(e)[:200]})
                    continue
                allow = r.headers.get("allow") or r.headers.get("Allow") or ""
                methods = [m.strip().upper() for m in allow.split(",") if m.strip()]
                dangerous = any(m in DANGEROUS for m in methods)
                results.append({
                    "path": path,
                    "status": r.status_code,
                    "allow": methods,
                    "dangerous": dangerous,
                })
        return {
            "base_url": base_url,
            "results": results,
        }

    # ── T44i: 2FA / MFA detection (alias of T44d mfa section) ───

    async def detect_2fa(self) -> dict[str, Any]:
        """T44i: 专门检测 2FA / MFA 信号 (WebAuthn / TOTP / SMS / backup code / Duo)."""
        r = await self.detect_auth_methods()
        return {
            "page_url": r["page_url"],
            "mfa": r["mfa"],
            "mfa_count": len(r["mfa"]),
            "has_webauthn": "WebAuthn/Passkey" in r["mfa"],
            "has_totp": any("TOTP" in m for m in r["mfa"]),
            "has_sms": any("SMS" in m for m in r["mfa"]),
            "has_backup_code": any("Backup" in m for m in r["mfa"]),
        }

    # ── T44j: External resource inventory ───────────────

    async def inventory_external_resources(self) -> dict[str, Any]:
        """T44j: 当前页所有外部资源分组 (供 trust boundary 分析).

        分组维度:
          - 外部 link domain: <a href> 指向外站的 host
          - 外部 script host: <script src> 外站 host
          - 外部 iframe: <iframe src> 外站 host
          - 跨域 form action: <form action> 外站 host
          - 跨域 redirect: 链接中含其他 host 的 redirect target

        Returns {
          "page_url",
          "external_link_domains": [{domain, count}],
          "external_script_hosts": [{host, urls}],
          "external_iframes": [{host, src}],
          "cross_origin_forms": [{host, action}],
        }
        """
        from urllib.parse import urlparse, urljoin
        from collections import Counter
        page = await self._ensure_page()
        page_url = page.url
        page_host = urlparse(page_url).hostname
        snap = await SnapshotEngine(page).capture(base_url=page_url, detail_level="full")
        # 1) 外部 link 域名
        link_domains: Counter[str] = Counter()
        for link in snap.links:
            href = link.href or ""
            if not href.startswith("http"):
                continue
            h = urlparse(href).hostname
            if h and h != page_host:
                link_domains[h] += 1
        # 2) 外部 script hosts
        script_hosts: dict[str, list[str]] = {}
        for s in snap.scripts:
            if not (s.has_src and s.src):
                continue
            try:
                u = urlparse(s.src)
                if u.hostname and u.hostname != page_host:
                    script_hosts.setdefault(u.hostname, []).append(s.src[:300])
            except Exception:
                pass
        # 3) iframe: snapshot 里没有直接拿, 走 page.evaluate
        iframes = await page.evaluate("""() => {
            const out = [];
            for (const f of document.querySelectorAll('iframe[src]')) {
                out.push(f.getAttribute('src'));
            }
            return out;
        }""")
        external_iframes: list[dict[str, str]] = []
        for src in iframes:
            try:
                full = urljoin(page_url, src)
                h = urlparse(full).hostname
                if h and h != page_host:
                    external_iframes.append({"host": h, "src": full[:300]})
            except Exception:
                pass
        # 4) 跨域 form action
        cross_origin_forms: list[dict[str, str]] = []
        for f in snap.forms:
            try:
                full = urljoin(page_url, f.action or "")
                h = urlparse(full).hostname
                if h and h != page_host:
                    cross_origin_forms.append({"host": h, "action": full[:300]})
            except Exception:
                pass
        return {
            "page_url": page_url,
            "external_link_domains": [
                {"domain": d, "count": c} for d, c in link_domains.most_common(50)
            ],
            "external_script_hosts": [
                {"host": h, "urls": urls[:5]} for h, urls in list(script_hosts.items())[:30]
            ],
            "external_iframes": external_iframes[:30],
            "cross_origin_forms": cross_origin_forms[:30],
        }

    # ── T44k: CSP 指令解析 (deep) ────────────────────

    async def parse_csp(self) -> dict[str, Any]:
        """T44k: 把 CSP 头拆成 directive × source 列表, 标出危险配置.

        Returns {
          "page_url",
          "csp_raw": str | None,
          "directives": {directive_name: [source, ...], ...},
          "flags": [str, ...],   # 危险配置: unsafe-inline, unsafe-eval, * wildcard, data:, ...
          "missing_recommended": [str, ...],  # 缺失建议的 directive (script-src, frame-ancestors, base-uri)
        }
        """
        page = await self._ensure_page()
        hdrs = await self.get_security_headers(page.url)
        csp = hdrs.get("csp")
        if not csp:
            return {"page_url": page.url, "csp_raw": None, "directives": {},
                    "flags": ["no_csp"], "missing_recommended": ["script-src", "frame-ancestors", "base-uri"]}
        raw = csp.get("raw", "") if isinstance(csp, dict) else str(csp)
        directives: dict[str, list[str]] = {}
        flags: list[str] = []
        for d in raw.split(";"):
            d = d.strip()
            if not d:
                continue
            parts = d.split(None, 1)
            name = parts[0].lower()
            sources = parts[1].split() if len(parts) > 1 else []
            directives[name] = sources
            for s in sources:
                sl = s.lower()
                if "'unsafe-inline'" in sl or s == "*":
                    flags.append(f"{name} contains {s}")
                if "'unsafe-eval'" in sl:
                    flags.append(f"{name} allows eval()")
                if s == "data:":
                    flags.append(f"{name} allows data: URI")
        recommended = ["script-src", "default-src", "frame-ancestors", "base-uri", "form-action"]
        missing = [r for r in recommended if r not in directives]
        return {
            "page_url": page.url,
            "csp_raw": raw,
            "directives": directives,
            "flags": flags,
            "missing_recommended": missing,
        }

    # ── T44l: Subdomain takeover signal ────────────────

    async def check_subdomain_takeover(
        self,
        host: str | None = None,
        subdomains: list[str] | None = None,
        *,
        doh_endpoint: str = "https://dns.google/resolve",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """T44l: 对每个子域查 CNAME, 跟已知"易被接管"服务签名比对.

        签名表 (fingerprint → risk):
          - s3.amazonaws.com / s3-website → "S3 bucket (check ownership)"
          - .herokuapp.com               → "Heroku app (check deletion)"
          - .azurewebsites.net           → "Azure Web App (check deletion)"
          - .cloudfront.net              → "CloudFront (check distribution)"
          - .elasticbeanstalk.com        → "Elastic Beanstalk"
          - .github.io                   → "GitHub Pages (check repo)"
          - .pantheonsite.io             → "Pantheon"
          - .tumblr.com                  → "Tumblr (custom domain)"
          - .wordpress.com               → "WordPress.com"
          - .shopify.com                 → "Shopify (check claim)"

        Returns {
          "host",
          "checked": int,
          "risky": [{subdomain, cname, provider, risk, http_status (if any)}],
        }
        """
        import re as _re
        import socket
        import httpx
        if not host:
            try:
                page = await self._ensure_page()
                from urllib.parse import urlparse
                host = urlparse(page.url).hostname
            except Exception:
                pass
        if not subdomains:
            # 默认查常见子域
            subdomains = [f"{prefix}.{host}" for prefix in (
                "www", "api", "staging", "dev", "test", "beta", "admin",
                "blog", "shop", "mail", "cdn", "static", "app",
            )]
        SIGS = [
            (r"\.s3(?:\-[a-z0-9\-]+)?\.amazonaws\.com",    "AWS S3",      "check if bucket exists / is yours"),
            (r"\.s3-website(?:\-[a-z0-9\-]+)?\.amazonaws\.com", "AWS S3 website", "check bucket ownership"),
            (r"\.herokuapp\.com",                          "Heroku",      "check if app is deleted"),
            (r"\.azurewebsites\.net",                      "Azure Web App", "check if app is deleted"),
            (r"\.cloudfront\.net",                         "CloudFront",  "check distribution ownership"),
            (r"\.elasticbeanstalk\.com",                   "Elastic Beanstalk", "check environment"),
            (r"\.github\.io",                               "GitHub Pages", "check repo exists"),
            (r"\.pantheonsite\.io",                        "Pantheon",    "check site status"),
            (r"\.tumblr\.com$",                            "Tumblr",      "check blog claim"),
            (r"\.wordpress\.com$",                         "WordPress.com", "check site claim"),
            (r"\.shopify\.com$",                           "Shopify",     "check store claim"),
        ]
        risky: list[dict[str, Any]] = []
        checked = 0

        async def _get_cname(sub: str) -> str | None:
            """Try CNAME via DoH, fallback to socket.gethostbyname (A record)."""
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.get(f"{doh_endpoint}?name={sub}&type=CNAME",
                                         headers={"Accept": "application/dns-json"})
                if r.status_code == 200:
                    for ans in r.json().get("Answer", []):
                        if ans.get("type") == 5:
                            return ans.get("data", "").rstrip(".")
            except Exception:
                pass
            return None

        async def _http_status(sub: str) -> int | None:
            try:
                async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
                    for scheme in ("https", "http"):
                        try:
                            r = await client.get(f"{scheme}://{sub}/", headers={"User-Agent": "semantic-browser-recon/1.0"})
                            return r.status_code
                        except Exception:
                            continue
            except Exception:
                pass
            return None

        for sub in subdomains:
            checked += 1
            cname = await _get_cname(sub)
            target = cname or sub
            matched_provider: str | None = None
            matched_risk: str | None = None
            for pat, provider, risk in SIGS:
                if _re.search(pat, target, _re.IGNORECASE):
                    matched_provider = provider
                    matched_risk = risk
                    break
            if matched_provider:
                # 拿 HTTP 状态辅助判断
                status = await _http_status(sub)
                # 404 / 503 / NXDOMAIN-like 强烈提示可接管
                suspicious_status = status in (404, 503) or status is None
                risky.append({
                    "subdomain": sub,
                    "cname": cname,
                    "provider": matched_provider,
                    "risk": matched_risk,
                    "http_status": status,
                    "suspicious_status": suspicious_status,
                })
        return {
            "host": host,
            "checked": checked,
            "risky": risky,
        }

    # ── T12: 通用 retry ─────────────────────────────────────────────

    # 这些异常 / 错误信号被识别为"短暂错误" — 自动 retry 一次
    _TRANSIENT_PHRASES = (
        "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_RESET", "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED",
        "net::", "Navigation timeout", "TimeoutError",
        "Element is not visible", "Element is detached",
        "Target page, context or browser has been closed",
    )

    def is_transient_error(self, exc: BaseException) -> bool:
        """判断一个异常是否属于短暂错误 (可 retry)."""
        msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return any(p in msg for p in self._TRANSIENT_PHRASES)

    async def with_retry(
        self,
        action: Callable[[], Awaitable[Any]],
        *,
        max_retries: int = 2,
        base_delay: float = 0.5,
        what: str = "action",
    ) -> Any:
        """T12: 包裹 async action, 短暂错误自动 retry (指数 backoff).

        max_retries=2 表示: 1 次主调用 + 最多 2 次 retry = 3 次机会。
        base_delay 每次 * 2 (0.5s, 1s)。

        返回 action 的结果; 不可恢复错误原样抛出。
        返回值包装: 如果 agent 想要知道 retry 次数, 看 controller.retry_count (最后一次值).
        """
        last_exc: Optional[BaseException] = None
        self.retry_count = 0
        for attempt in range(max_retries + 1):
            try:
                return await action()
            except Exception as e:
                if not self.is_transient_error(e) or attempt == max_retries:
                    raise
                last_exc = e
                self.retry_count = attempt + 1
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s 触发短暂错误 (第 %d 次重试, %.1fs 后): %s",
                    what, attempt + 1, delay, e,
                )
                await asyncio.sleep(delay)
        # 不会到这里 (最后那次若失败会 raise), 但类型检查器要 unbind
        assert last_exc is not None
        raise last_exc


# ── T42c: CORS 风险评估 (module-level helper) ─────────────

def _assess_cors_risk(allow_origin: str | None, allow_credentials: bool) -> str:
    """CORS misconfig 风险分级 — pen-tester 第一眼看.

    high:   ACAO=* + credentials=true — 浏览器实际会拒绝, 但说明后端配置混乱 / 可能绕过
    medium: ACAO=* (无 credentials) — 任意 origin 可读 (取决于 content 敏感性)
    low:    ACAO 是具体 origin (e.g. https://app.example.com) — 正常情况
    none:   没有 ACAO 头 — 浏览器 same-origin 保护
    """
    if not allow_origin:
        return "none"
    if allow_origin == "*":
        return "high" if allow_credentials else "medium"
    if allow_origin == "null":
        return "high"  # null origin + 沙箱文件 / data: URI 是攻击向量
    return "low"


# ── T42b: 版本号比较 (module-level helper) ─────────────

def _version_lt(a: str, b: str) -> bool:
    """简单 semver-like 比较: a < b ? True."""
    try:
        ap = tuple(int(x) for x in a.split("."))
        bp = tuple(int(x) for x in b.split("."))
        while len(ap) < len(bp):
            ap = ap + (0,)
        while len(bp) < len(ap):
            bp = bp + (0,)
        return ap < bp
    except Exception:
        return False


# ── T43a: TLS cert SAN helper (module-level) ─────────────

def _tls_subdomains(host: str, port: int = 443, timeout: float = 5.0) -> list[str]:
    """连 host:port 取证书, 解析 SAN 列表, 过滤出 host 的子域.

    返回 lowercased 去重子域列表 (含 host 本身如果出现在 SAN 里).
    """
    import re as _re
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert(binary_form=False) or {}
                der = ss.getpeercert(binary_form=True) or b""
        sans: list[str] = []
        for entry in cert.get("subjectAltName", []):
            if entry and entry[0].lower() == "dns":
                sans.append(entry[1].lower())
        if not sans and der:
            text = der.decode("latin-1", errors="ignore")
            for m in _re.finditer(r"DNS:([A-Za-z0-9._-]+)", text):
                sans.append(m.group(1).lower())
        # 过滤 host 的子域
        return sorted({s for s in sans if s == host or s.endswith("." + host)})
    except Exception:
        return []


# ── T43i: Server / Generator hint helpers ─────────────

_SERVER_HINTS = (
    (r"(?i)\bnginx\b",        "nginx"),
    (r"(?i)\bapache\b",       "Apache"),
    (r"(?i)\biis\b",          "IIS (Microsoft)"),
    (r"(?i)\benvoy\b",        "Envoy (often behind K8s)"),
    (r"(?i)\btraefik\b",      "Traefik"),
    (r"(?i)\bhaproxy\b",      "HAProxy"),
    (r"(?i)\bcaddy\b",        "Caddy"),
    (r"(?i)\bcloudfront\b",   "AWS CloudFront"),
    (r"(?i)\bgfe\b",          "Google Frontend (GFE)"),
    (r"(?i)\blite-?speed\b",  "LiteSpeed"),
    (r"(?i)\bgunicorn\b",     "gunicorn (Python)"),
    (r"(?i)\buwsgi\b",        "uWSGI (Python)"),
    (r"(?i)\bjetty\b",        "Jetty (Java)"),
    (r"(?i)\btomcat\b",       "Tomcat (Java)"),
    (r"(?i)\bopenresty\b",    "OpenResty (Lua/nginx)"),
    (r"(?i)\bvercel\b",       "Vercel"),
    (r"(?i)\bnetlify\b",      "Netlify"),
)


def _server_hint(server_header: str) -> str:
    """从 Server header 推断 web server. 失败 → ''."""
    import re as _re
    if not server_header:
        return ""
    for pat, name in _SERVER_HINTS:
        if _re.search(pat, server_header):
            return name
    return server_header[:60]


_GENERATOR_HINTS = (
    (r"(?i)wordpress\s*([\d.]+)?",  "WordPress"),
    (r"(?i)drupal\s*([\d.]+)?",     "Drupal"),
    (r"(?i)joomla\s*([\d.]+)?",     "Joomla"),
    (r"(?i)ghost\s*([\d.]+)?",      "Ghost"),
    (r"(?i)hugo\s*([\d.]+)?",       "Hugo (static)"),
    (r"(?i)jekyll\s*([\d.]+)?",     "Jekyll (static)"),
    (r"(?i)eleventy\s*([\d.]+)?",   "Eleventy (static)"),
    (r"(?i)next\.?js",              "Next.js"),
    (r"(?i)nuxt",                   "Nuxt.js"),
    (r"(?i)gatsby",                 "Gatsby"),
    (r"(?i)hexo",                   "Hexo"),
    (r"(?i)typecho",                "Typecho"),
    (r"(?i)mediawiki",              "MediaWiki"),
    (r"(?i)discuz",                 "Discuz!"),
)


def _generator_hint(generator: str) -> str:
    """从 <meta name='generator'> 内容推断 CMS / 框架."""
    import re as _re
    if not generator:
        return ""
    for pat, name in _GENERATOR_HINTS:
        m = _re.search(pat, generator)
        if m:
            ver = m.group(1) if m.lastindex and m.group(1) else ""
            return f"{name} {ver}".strip()
    return generator[:60]
