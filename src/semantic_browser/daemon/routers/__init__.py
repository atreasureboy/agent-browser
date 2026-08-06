"""Daemon route handlers — extracted from server.py _dispatch.

Each submodule registers handlers via ``_register``. ``build_route_table()``
lazy-loads all submodules and returns a ``{(method, path): handler}`` dict.

Shared constants and exceptions are re-exported here so router modules can
import them without pulling in ``server.py`` (which would create a cycle).
"""
from __future__ import annotations

import re
from typing import Any, Callable

# ── Handler signature ──────────────────────────────────────────────────
HandlerFn = Callable[..., Any]

# ── Shared constants (originally _AsyncOwner class attributes) ─────────
DEFAULT_SESSION = "default"
DEFAULT_TENANT   = "anonymous"
DEFAULT_AGENT    = "anonymous"


# ── Shared exceptions ──────────────────────────────────────────────────

class SessionError(Exception):
    """T54: session 操作失败的业务异常 — 带 code 用于 HTTP 状态映射."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ── Route registry ─────────────────────────────────────────────────────

_ROUTES: list[tuple[str, str, HandlerFn]] = []
# Dynamic entries: (method, re.Pattern, handler)
_DYNAMIC_PATTERNS: list[tuple[str, re.Pattern[str], HandlerFn]] = []


def _register(method: str, path: str, handler: HandlerFn) -> HandlerFn:
    """Register a handler for (method, path). Returns handler for decorator-style use.

    Paths containing ``{name}`` / ``{lease_id}`` brackets are treated as
    dynamic patterns and are matched via regex fallback rather than exact
    dictionary lookup.
    """
    _ROUTES.append((method, path, handler))
    return handler


def _path_to_pattern(path: str) -> str:
    """Convert a path with ``{name}`` / ``{id}`` placeholders to a regex pattern.

    ``{name}`` and ``{lease_id}`` → ``([^/]+)`` (single path segment).
    """
    result = path
    result = result.replace("{name}", r"([^/]+)")
    result = result.replace("{lease_id}", r"([^/]+)")
    return "^" + result + "$"


_ROUTE_TABLE_CACHE: dict[tuple[str, str], HandlerFn] | None = None


def build_route_table() -> dict[tuple[str, str], HandlerFn]:
    """Lazy-build (and cache) the dispatch table from registered routes.

    Only rebuilds on the first call; subsequent calls return the cached
    table.  Paths with bracket placeholders (``{name}``, ``{lease_id}``)
    are added to the dynamic-pattern list instead of the exact-match table.
    """
    global _ROUTE_TABLE_CACHE
    if _ROUTE_TABLE_CACHE is not None:
        return _ROUTE_TABLE_CACHE

    # Import submodules to trigger their _register calls (cached by Python)
    from semantic_browser.daemon.routers import _admin      # noqa: F401
    from semantic_browser.daemon.routers import _events     # noqa: F401
    from semantic_browser.daemon.routers import _sessions   # noqa: F401
    from semantic_browser.daemon.routers import _browser    # noqa: F401
    from semantic_browser.daemon.routers import _security   # noqa: F401
    from semantic_browser.daemon.routers import _query      # noqa: F401
    from semantic_browser.daemon.routers import _agent      # noqa: F401
    from semantic_browser.daemon.routers import _discover   # noqa: F401

    table: dict[tuple[str, str], HandlerFn] = {}
    for method, path, handler in _ROUTES:
        if "{" in path:
            # Dynamic path — build regex pattern, keyed by (method, pattern)
            _DYNAMIC_PATTERNS.append(
                (method, re.compile(_path_to_pattern(path)), handler)
            )
        else:
            table[(method, path)] = handler
    _ROUTE_TABLE_CACHE = table
    return table


def resolve_dynamic_route(
    method: str, path: str,
) -> tuple[HandlerFn, str] | None:
    """Try to match *path* against the dynamic session patterns.

    Both HTTP method and path pattern must match.

    Returns ``(handler, matched_path)`` on success, or ``None`` if no
    pattern matches.
    """
    for _method, pattern, handler in _DYNAMIC_PATTERNS:
        if _method == method and pattern.match(path):
            return handler, path
    return None
