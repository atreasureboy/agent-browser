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


# T116 audit fix: classify_exception 用 — 把 str(e) 里的绝对路径 / URL
# query / Authorization / token 之类先 mask 掉再返 client. 防 FileNotFoundError
# 露 /root/.semantic-browser/..., 露 ?key=..., 露 Bearer xxx.
import re as _re_redact
_REDACT_PATH = _re_redact.compile(r"(/[\w./\-]+){2,}")
_REDACT_QUERY = _re_redact.compile(r"([?&])(token|key|api_key|apikey|api-key|password|secret|signature|sig|access_token|auth)=([^&\s\"']+)", _re_redact.IGNORECASE)
_REDACT_HEADER = _re_redact.compile(r"(?i)(authorization|x-api-key|x-goog-api-key|cookie|set-cookie):\s*[^\s\"',;}]+")
_REDACT_ENV = _re_redact.compile(r"\b(/root|/home/[^/\s\"',;}]+|\./~)\S*")


def _redact_message(msg: str) -> str:
    """T116: 脱敏 — path / query / header / user 路径都 mask 掉.

    不追求完美 (e.g. SQL 错误里的 schema 名可能漏) — 目标是常见的 str(e)
    不再泄绝对文件路径 / 完整 URL / 凭据. 残余泄漏通过别的方式 (e.g. 不要
    把 str(e) 透传给 client) 补.
    """
    if not msg:
        return msg
    out = _REDACT_PATH.sub("<path>", msg)
    out = _REDACT_QUERY.sub(r"\1\2=<redacted>", out)
    out = _REDACT_HEADER.sub(r"\1: <redacted>", out)
    out = _REDACT_ENV.sub("<redacted-path>", out)
    return out


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

    T116 audit fix: 之前直接把 str(e) 露给 HTTP client — FileNotFoundError
    / sqlite3.OperationalError / OSError 里会含绝对路径 (e.g.
    "/root/.semantic-browser/snapshots/foo.json"), httpx error 会含
    query string (?key=...) / Authorization 头. 修: _redact_message 把
    path / key / token / cookie 全部 mask 掉再返.
    """
    name = type(e).__name__
    msg = str(e) or name
    msg = _redact_message(msg)
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