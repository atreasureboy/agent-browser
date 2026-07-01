"""
Browser Controller — Playwright 封装层。

真实浏览器控制：open / back / forward / reload / scroll / wait / screenshot。
不做复杂逻辑，只保证稳定可靠。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse

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
        self._max_event_buffer = 1000  # 防无限增长

    async def start(self) -> None:
        """启动浏览器。"""
        if self._browser is not None:
            return  # 已启动
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
        )
        import os
        context_kwargs = {
            "viewport": self.config.viewport,
            "user_agent": self.config.user_agent,
            "locale": self.config.locale,
        }
        if self.config.storage_state_path and os.path.exists(self.config.storage_state_path):
            context_kwargs["storage_state"] = self.config.storage_state_path
        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(self.config.timeout)
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
                await self.start()
            self._page = await self._context.new_page()
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
        """T19: 拖拽 from_ref 到 to_ref (人类拖文件 / 排序列表 / 移动卡片).

        HTML5 drag-and-drop + 鼠标手势都尝试, 用 mouse.down/move/up 兜底.
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
            logger.info("Dragged ref=%s -> ref=%s", from_ref, to_ref)
            return True
        except Exception as e:
            logger.warning("Drag failed ref=%s->%s: %s", from_ref, to_ref, e)
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
        """每个响应回填 status, 改最后一条同 url+method 的未完成 request."""
        try:
            url = resp.url
            status = resp.status
            method = resp.request.method if resp.request else None
        except Exception:
            return
        for entry in reversed(self._network_requests):
            if entry.get("url") == url and entry.get("method") == method and "status" not in entry:
                entry["status"] = status
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

    async def get_storage(self, kind: str = "local") -> dict[str, str]:
        """读 localStorage / sessionStorage. kind: 'local' or 'session'.

        Returns {key: value} (value 是 str; 复杂类型可能需要 agent 自己 parse).
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
        """T15: 列出所有 frame (顶层 + 所有 iframe).

        Returns [{"name": "main", "url": "...", "is_main": True},
                 {"name": "iframe[name=foo]", "url": "...", "is_main": False}, ...]
        """
        page = await self._ensure_page()
        out = [{"name": "main", "url": page.url, "is_main": True}]
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            out.append({
                "name": f"frame[{frame.name or '(unnamed)'}]",
                "url": frame.url,
                "is_main": False,
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
