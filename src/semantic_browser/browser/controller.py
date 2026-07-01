"""
Browser Controller — Playwright 封装层。

真实浏览器控制：open / back / forward / reload / scroll / wait / screenshot。
不做复杂逻辑，只保证稳定可靠。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
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
        logger.info("BrowserController closed")

    async def _ensure_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            if self._context is None:
                await self.start()
            self._page = await self._context.new_page()
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

    async def press_key(self, key: str) -> None:
        """按键，如 Enter, Tab, Escape。"""
        page = await self._ensure_page()
        await page.keyboard.press(key)

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
