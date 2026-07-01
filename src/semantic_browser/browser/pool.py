"""
T33: ControllerPool — 多 agent 并发隔离.

设计:
- 共享一个 Browser (chromium 进程) — 节省资源
- 每个 agent 独立 BrowserContext — cookies/storage/cache 隔离
- acquire(name) → 拿一个 controller (懒创建 context); release() 归还
- context 自动命名 (e.g. "agent-1", "agent-2") 方便调试

为啥 separate module: ControllerPool 是 Browser 上面的多租户抽象,
不是 controller 的内部细节. 独立文件清晰.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from semantic_browser.browser.controller import BrowserConfig, BrowserController

logger = logging.getLogger(__name__)


class ControllerPool:
    """多 controller 池 — 共享 browser, 隔离 context.

    用法:
        pool = ControllerPool()
        await pool.start()
        try:
            ctrl_a = await pool.acquire("agent-a")
            ctrl_b = await pool.acquire("agent-b")
            # 各自跑独立任务, 互不干扰
            await asyncio.gather(
                ctrl_a.open("https://site-a.com"),
                ctrl_b.open("https://site-b.com"),
            )
        finally:
            await pool.close()

    自动 acquire 场景: 缺 controller 时直接给一个新的 (with auto-name).
    release() 关闭 context 但保留 controller 对象 (可重用).
    """

    def __init__(
        self,
        config: BrowserConfig | None = None,
        *,
        max_contexts: int = 5,
    ) -> None:
        self.config = config or BrowserConfig()
        self.max_contexts = max_contexts
        self._browser = None  # 共享 chromium 实例
        self._playwright = None
        self._controllers: dict[str, BrowserController] = {}
        self._lock = asyncio.Lock()  # 保护 _controllers 操作

    async def start(self) -> None:
        """启动 playwright + chromium (共享)."""
        if self._browser is not None:
            return
        from semantic_browser.browser.controller import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
        )
        logger.info("ControllerPool started (max_contexts=%d)", self.max_contexts)

    async def acquire(self, name: str | None = None) -> BrowserController:
        """拿一个 controller (懒创建 context).

        name: 名字 (调试用). 不传则自动生成 "agent-N".
        """
        if self._browser is None:
            await self.start()
        async with self._lock:
            if name is None:
                name = f"agent-{len(self._controllers) + 1}"
            if name in self._controllers:
                # 已存在 — 直接返回 (可重用)
                return self._controllers[name]
            if len(self._controllers) >= self.max_contexts:
                raise RuntimeError(
                    f"ControllerPool exhausted: {len(self._controllers)}/{self.max_contexts}",
                )
            ctrl = self._make_controller(name)
            self._controllers[name] = ctrl
            logger.info("ControllerPool acquired %s (total=%d)", name, len(self._controllers))
            return ctrl

    def _make_controller(self, name: str) -> BrowserController:
        """构造一个 controller, 复用共享 browser, 独立 context."""
        ctrl = BrowserController(self.config)
        # 直接注入共享 browser (不走 ctrl.start() 重新启动)
        ctrl._playwright = self._playwright
        ctrl._browser = self._browser
        # 给 controller 一个独立 context (复用 start() 里的 _start_context 逻辑)
        # 这里同步构造, 异步创建 context — 由 caller 在第一次 open() 前 await ctrl._ensure_context()
        ctrl._pool_name = name  # 标记来源
        return ctrl

    async def release(self, name: str) -> None:
        """归还并关闭 controller (关闭它自己的 context, 不影响共享 browser)."""
        async with self._lock:
            ctrl = self._controllers.pop(name, None)
        if ctrl is None:
            return
        try:
            if ctrl._context is not None:
                await ctrl._context.close()
        except Exception as e:
            logger.warning("release %s: close context failed: %s", name, e)
        logger.info("ControllerPool released %s (remaining=%d)", name, len(self._controllers))

    async def close(self) -> None:
        """关闭所有 controller + 共享 browser."""
        async with self._lock:
            names = list(self._controllers.keys())
        for n in names:
            await self.release(n)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning("close browser: %s", e)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("stop playwright: %s", e)
        self._browser = None
        self._playwright = None
        logger.info("ControllerPool closed")

    def list_active(self) -> list[str]:
        """列出当前活跃 controller 名."""
        return list(self._controllers.keys())

    async def __aenter__(self) -> "ControllerPool":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()