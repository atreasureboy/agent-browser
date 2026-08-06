"""
Browser Controller — Playwright 封装层。

真实浏览器控制：open / back / forward / reload / scroll / wait / screenshot。
不做复杂逻辑，只保证稳定可靠。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,  # re-exported for pool.py
)

# Import mixins — each contributes a focused set of methods
from semantic_browser.browser.navigation import _NavigationMixin
from semantic_browser.browser.interact import _InteractMixin
from semantic_browser.browser.debug import _DebugMixin
from semantic_browser.browser.headers import _HeadersMixin, _parse_csp, _parse_hsts, _parse_permissions_policy, _parse_set_cookie
from semantic_browser.browser.security_tools import _SecurityToolsMixin
from semantic_browser.browser._utils import _redact_url_secrets, _assess_cors_risk, _tls_subdomains, _version_lt, _server_hint, _generator_hint

# Re-exports for backward compatibility — tests and other modules may import
# these module-level helpers from controller.py directly
__all__ = [
    "BrowserController",
    "BrowserConfig",
    "async_playwright",
    "_redact_url_secrets",
    "_assess_cors_risk",
    "_version_lt",
    "_server_hint",
    "_generator_hint",
    "_tls_subdomains",
    "_parse_csp",
    "_parse_hsts",
    "_parse_permissions_policy",
    "_parse_set_cookie",
]

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


class BrowserController(
    _NavigationMixin,
    _InteractMixin,
    _DebugMixin,
    _HeadersMixin,
    _SecurityToolsMixin,
):
    """
    Playwright 异步封装。

    用法:
        controller = BrowserController()
        await controller.start()
        page = await controller.open("https://example.com")
        snapshot = await controller.get_aria_snapshot()
        await controller.close()
    """

    # ── T12: 通用 retry ─────────────────────────────────────────────

    # 这些异常 / 错误信号被识别为"短暂错误" — 自动 retry 一次
    _TRANSIENT_PHRASES = (
        "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_RESET", "ERR_TIMED_OUT", "ERR_NETWORK_CHANGED",
        "net::", "Navigation timeout", "TimeoutError",
        "Element is not visible", "Element is detached",
        "Target page, context or browser has been closed",
    )

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
        # T118: 当前 context 的 fingerprint profile (UA / platform / languages /
        # plugins / sec-ch-ua). 注入到 init script, 让 STEALTH_JS 按 profile 覆盖.
        # config.user_agent 显式给定时, _profile=None, STEALTH_JS 不覆盖.
        self._profile = None

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
