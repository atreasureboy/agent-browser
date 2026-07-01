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
        page = await self._ensure_page()
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await page.locator(in_selector).filter(has_text=text).count()
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
        page = await self._ensure_page()
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
        page = await self._ensure_page()
        return await page.screenshot(path=path, full_page=False)

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
        page = await self._ensure_page()
        try:
            selector = self._ref_to_selector(ref)
            locator = page.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=5000)
            logger.info("Clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Click failed ref=%s: %s", ref, e)
            return False

    async def type_text(self, ref: str, text: str) -> bool:
        """通过 @ref 输入文本。"""
        page = await self._ensure_page()
        try:
            selector = self._ref_to_selector(ref)
            locator = page.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.fill(text, timeout=5000)
            logger.info("Typed into ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Type failed ref=%s: %s", ref, e)
            return False

    async def fill_form(self, fields: dict[str, str]) -> dict[str, bool]:
        """T11: 一次性填多个字段 (人类填表的"批量"动作)。

        Returns {ref: ok} — agent 能立即看出哪些字段没填上, 再针对性 retry。
        """
        out: dict[str, bool] = {}
        for ref, text in fields.items():
            out[ref] = await self.type_text(ref, text)
        return out

    async def press_key(self, key: str) -> None:
        """按键，如 Enter, Tab, Escape。"""
        page = await self._ensure_page()
        await page.keyboard.press(key)

    # ── T13: 文件上传 ────────────────────────────────────────

    async def set_files(self, ref: str, paths: list[str]) -> dict[str, Any]:
        """T13: 通过 ref 给 file input 设置文件路径 (人类"上传附件"动作).

        Returns {"ok": bool, "ref": str, "file_count": int, "error": Optional[str]}.
        """
        page = await self._ensure_page()
        try:
            selector = self._ref_to_selector(ref)
            locator = page.locator(selector).first
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
        """获取页面 HTML。"""
        page = await self._ensure_page()
        return await page.content()

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
