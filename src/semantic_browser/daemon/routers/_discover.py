"""Discover route handlers — extracted from server.py _dispatch.

POST /discover      — live site map discovery (T30)
GET  /discover/stream — SSE streaming discovery (T50)
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


def handle_discover(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /discover — live site map discovery (vs /graph which queries history).

    SSRF guard (T66.8): start_url is checked before dispatching to the
    controller, same as /agent/run and /v1/query.
    """
    start_url = args.get("start_url", "")
    if start_url:
        daemon._check_url(start_url, where="discover")
    return daemon.owner.run(daemon._discover(args))


def handle_discover_stream(daemon: Any, args: dict[str, Any], req: Any) -> str:
    """GET /discover/stream — SSE streaming discovery (T50).

    Each page / failure / done is emitted as a Server-Sent Event.
    SSRF guard (T66.8): start_url is checked before streaming begins.
    """
    if req is None:
        raise ValueError("/discover/stream requires req context")
    start_url = args.get("start_url", "")
    if start_url:
        daemon._check_url(start_url, where="discover.stream")
    daemon._stream_discover(req, args)
    return "_SSE_HANDLED"


_register("POST", "/discover", handle_discover)
_register("GET", "/discover/stream", handle_discover_stream)
