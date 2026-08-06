"""SSE events route handler — extracted from server.py _dispatch.

GET /events streams daemon events (EventBus) via Server-Sent Events.
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


def handle_events(daemon: Any, args: dict[str, Any], req: Any) -> str:
    """GET /events — stream EventBus events via SSE.

    Delegates to daemon._stream_events which handles:
      - topic pattern filtering (query param ?topics=)
      - tenant filtering (?tenant_id=)
      - historical replay via Last-Event-ID header
      - live event bridging via asyncio poll loop
    """
    if req is None:
        raise ValueError("/events requires req context")
    daemon._stream_events(req, args)
    return "_SSE_HANDLED"


_register("GET", "/events", handle_events)
