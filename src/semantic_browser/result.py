"""T48: Typed Result envelope for all tool surfaces (daemon, MCP, CLI).

Convention:
  Success: {"ok": True,  "data": <result>, "error": None}
  Failure: {"ok": False, "data": None,     "error": {"code": str, "message": str, "retryable": bool}}

Used by daemon and MCP server. Controller methods continue to return whatever
they returned before — wrapping happens at the boundary layer, so existing
Python API users see no change.

Agents should:
  - match on `error.code` (stable), not on `error.message` (free-form)
  - use `error.retryable` to decide whether to retry vs escalate

Stable error codes (treat as enum):
  PAGE_NOT_OPENED  — operation needs a page but no page was loaded
  NETWORK_FAIL     — DNS / connect / timeout — agent may retry
  INVALID_URL      — URL parse failed — won't fix itself
  EMPTY_RESULT     — query returned no data — agent should not retry
  MISSING_PARAM    — required parameter missing from request
  INTERNAL         — unexpected exception — surface to operator
  NOT_IMPLEMENTED  — capability not yet wired
"""
from __future__ import annotations

from typing import Any


# Stable error codes
CODE_PAGE_NOT_OPENED = "PAGE_NOT_OPENED"
CODE_NETWORK_FAIL = "NETWORK_FAIL"
CODE_INVALID_URL = "INVALID_URL"
CODE_EMPTY_RESULT = "EMPTY_RESULT"
CODE_MISSING_PARAM = "MISSING_PARAM"
CODE_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
CODE_SSRF_BLOCKED = "SSRF_BLOCKED"
CODE_INTERNAL = "INTERNAL"


def ok(data: Any) -> dict[str, Any]:
    """Wrap a successful result."""
    return {"ok": True, "data": data, "error": None}


def err(code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    """Wrap a structured error."""
    return {"ok": False, "data": None,
            "error": {"code": code, "message": message, "retryable": retryable}}


def classify_exception(e: BaseException) -> dict[str, Any]:
    """Map common exception types to structured error codes.

    Conservative defaults: when in doubt → INTERNAL not retryable.
    """
    name = type(e).__name__
    msg = str(e) or name
    # Network-shaped errors → NETWORK_FAIL, retryable
    network_kw = (
        "Connection refused", "Connection reset", "DNS", "Name or service",
        "timeout", "Timeout", "ConnectError", "ReadError", "RemoteProtocolError",
        "Network", "unreachable", "Temporary failure",
    )
    if isinstance(e, (ConnectionError, TimeoutError)) or any(kw in msg for kw in network_kw):
        return err(CODE_NETWORK_FAIL, f"{name}: {msg}", retryable=True)
    if isinstance(e, KeyError):
        return err(CODE_MISSING_PARAM, f"missing parameter: {e.args[0] if e.args else name}",
                   retryable=False)
    # Invalid URL hints
    if "Invalid URL" in msg or "invalid url" in msg.lower():
        return err(CODE_INVALID_URL, msg, retryable=False)
    if isinstance(e, ValueError):
        # T58: SSRF error 是 ValueError 子类, 优先识别 (不在 else 走 MISSING_PARAM)
        cls_name = type(e).__name__
        if cls_name == "SSRFBlockedError" or "SSRF" in cls_name or "blocked" in msg.lower()[:50]:
            return err(CODE_SSRF_BLOCKED, f"{name}: {msg}", retryable=False)
        return err(CODE_MISSING_PARAM, msg, retryable=False)
    if isinstance(e, NotImplementedError):
        return err(CODE_NOT_IMPLEMENTED, msg, retryable=False)
    return err(CODE_INTERNAL, f"{name}: {msg}", retryable=False)