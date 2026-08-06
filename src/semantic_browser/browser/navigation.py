"""Navigation, tab management, and basic browser operations."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

from playwright.async_api import Page, async_playwright

from semantic_browser.browser._utils import _redact_url_secrets

logger = logging.getLogger(__name__)


class _NavigationMixin:
    """Navigation, tab management, and basic browser operations — mixed into BrowserController."""

    async def start(self) -> None:
        """启动浏览器。"""
        if self._browser is not None:
            return  # 已启动
        self._playwright = await async_playwright().start()
        # T106: 加 BROWSER_DISABLE_OPTIONS (参考 Crawl4AI) — 减分用
        from semantic_browser.safety.stealth import BROWSER_DISABLE_OPTIONS
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=BROWSER_DISABLE_OPTIONS,
        )
        await self._start_context()

    async def _start_context(self) -> None:
        """T33: 给当前 controller 创建独立 context. Pool 用 — 不重复启动 browser.

        T118: 选 profile 后所有可观察字段 (UA / platform / locale /
        Accept-Language / Client Hints) 都从 profile 派生, 自洽.
        config.user_agent / config.locale 仍可显式覆盖 — 此时按
        config 走, 不选 profile.
        """
        # T118: 选 profile, 让 UA / platform / Accept-Language / Client Hints
        # 全部对齐 (减分用 — 跨字段不一致本身就是 anti-bot 信号).
        from semantic_browser.safety.stealth import pick_profile
        profile = pick_profile() if not self.config.user_agent else None
        user_agent = self.config.user_agent or (profile.user_agent if profile else "Mozilla/5.0")
        locale = self.config.locale if self.config.user_agent else (profile.locale if profile else self.config.locale)
        accept_language = profile.accept_language if profile else "en-US,en;q=0.9"
        # sec-ch-ua-platform 必须是带引号的 quoted-string
        sec_ch_ua_platform = f'"{profile.platform_header}"' if profile else '"Linux"'
        sec_ch_ua = profile.sec_ch_ua if profile else '"Chromium";v="120", "Not_A Brand";v="24"'
        sec_ch_ua_mobile = profile.sec_ch_ua_mobile if profile else "?0"
        # 保存 profile 给 _ensure_page 用 — 注入到 init script 让 STEALTH_JS
        # 按 profile 字段覆盖 navigator.platform / languages / plugins
        self._profile = profile
        context_kwargs = {
            "viewport": self.config.viewport,
            "user_agent": user_agent,
            "locale": locale,
            # T118: profile-coherent headers — UA / Accept-Language / Client Hints
            # 必须三方对齐 (UA 主版本号 ↔ sec-ch-ua, locale ↔ Accept-Language ↔ languages)
            "extra_http_headers": {
                "Accept-Language": accept_language,
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-mobile": sec_ch_ua_mobile,
                "sec-ch-ua-platform": sec_ch_ua_platform,
            },
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
        # T117 audit fix: URL 里的 ?token= / ?api_key= / ?session= 不能落 INFO log.
        logger.info("Opened new tab: %s", _redact_url_secrets(url or "(blank)"))
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
        logger.info("Switched to tab %d: %s", index, _redact_url_secrets(page.url))
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
        """确保有 current_page — 必要时建一个。

        T104 fix: 不只查 is_closed() — 也查 is_crashed() (page.goto Page crashed
        后 is_closed=False 但 page 不可用). crashed 也当作需要重建.

        T107 fix: 原来 `self._page.evaluate(...)` 漏 await, 产生
        RuntimeWarning. 改成 await, 同时用 try/except 捕获 evaluate 抛
        (page crashed 时 evaluate 会抛). 完全替换之前那个错误表达式.
        """
        need_new = False
        if self._page is None:
            need_new = True
        elif self._page.is_closed():
            need_new = True
        else:
            # 试着 evaluate — page crashed 时会抛 (yielding coroutine 不会抛)
            try:
                await self._page.evaluate("() => 1")
            except Exception:
                need_new = True
        if (
            not need_new
            and hasattr(self._page, "is_crashed")
            and self._page is not None
        ):
            try:
                if self._page.is_crashed():
                    need_new = True
            except Exception:
                pass
        if self._page is None or need_new:
            if self._context is None:
                # T33: Pool 创建的 controller 共享 browser 但 context 还没建
                if self._browser is not None:
                    await self._start_context()
                else:
                    await self.start()
            self._page = await self._context.new_page()
            # T118: profile-coherent stealth — 先注入 profile, 再注入 STEALTH_JS.
            #   1) profile 写到 window.__SB_PROFILE__ (供 STEALTH_JS 读)
            #   2) STEALTH_JS 按 profile 覆盖 navigator.platform / languages / plugins
            # 顺序关键: profile 先 → STEALTH_JS 后, 后者在同一 microtask 内执行
            # 时, window.__SB_PROFILE__ 已就绪. addInitScript 按调用顺序追加.
            # config.user_agent 显式给定时, _profile=None, 不注入 profile,
            # STEALTH_JS 静默跳过 profile-dependent 覆盖 (只覆盖 webdriver + chrome).
            if self._profile is not None:
                import json as _json
                from semantic_browser.safety.stealth import STEALTH_JS
                _profile_payload = _json.dumps({
                    "platform": self._profile.platform,
                    "languages": list(self._profile.languages),
                    "plugins": list(self._profile.plugins),
                })
                await self._context.add_init_script(
                    f"window.__SB_PROFILE__ = {_profile_payload};"
                )
                await self._context.add_init_script(STEALTH_JS)
            else:
                # 没有 profile (config.user_agent 显式) — 只跑 STEALTH_JS 兜底
                # 覆盖 webdriver + chrome, 不动 platform / languages / plugins.
                from semantic_browser.safety.stealth import STEALTH_JS
                await self._context.add_init_script(STEALTH_JS)
            # T40i: WebSocket 监控 (per-page, open 握手触发)
            self._page.on("websocket", self._on_websocket)
            self._active_idx = 0
        return self._page

    async def open(self, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 30_000, human_like_wait: bool = True) -> Page:
        """打开 URL, 带超时降级与死锁防御。"""
        page = await self._ensure_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as e:
            if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                logger.warning("Page goto timed out with wait_until=%s for %s, retrying with commit fallback...", wait_until, _redact_url_secrets(url))
                try:
                    await page.goto(url, wait_until="commit", timeout=10_000)
                    await page.evaluate("window.stop()")
                except Exception as inner_e:
                    logger.warning("Commit fallback failed: %s", inner_e)
            else:
                raise e

        logger.info("Opened: %s (wait=%s)", _redact_url_secrets(url), wait_until)

        if human_like_wait:
            try:
                from semantic_browser.safety.antibot import detect_antibot
                content = await page.content()
                blocked, reason = detect_antibot(content)
                if blocked and ("Just a Moment" in content or "Checking your browser" in content or "cf-error-code" in content or "challenge-form" in content):
                    logger.info("Detected Anti-bot challenge shield (%s). Waiting gracefully up to 4s for browser verification...", reason)
                    await asyncio.sleep(4.0)
            except Exception as e:
                logger.debug("Anti-bot check ignored: %s", e)

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
        # T100 audit fix: 之前 line 404 引用 undefined `page` (NameError). 用 `target` 修.
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await target.locator(selector).count()
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

        返回的是 accessibility tree 的 YAML 表示, 类似:
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
