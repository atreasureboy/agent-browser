"""Semantic query, LLM helper, and goal-memory route handlers — extracted from server.py _dispatch.

Route coverage:
  /v1/query          POST   — semantic query entry (model-driven browser semantic layer)
  /v1/query/stream   POST   — SSE streaming progress
  /v1/query/stats    GET    — monitoring endpoint
  /v1/query/log      GET    — recent query log
  /v1/query/cache/clear POST — flush shared SemanticQuery cache
  /memory/stats      GET    — cross-session goal memory stats
  /memory/list       GET    — recent goal-memory entries
  /memory/clear      POST   — clear goal memory
  /llm/stats         GET    — LLM service stats
  /llm/slice         POST   — slice a page into LLM-compatible chunks
  /llm/summarize     POST   — summarise a page
  /llm/extract       POST   — extract structured data
  /llm/find-ref      POST   — find references on a page
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


# ── /v1/query endpoints ──────────────────────────────────────────────

def handle_v1_query(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /v1/query — semantic query entry (model-driven browser semantic layer)."""
    # T67: /v1/query
    start_url = args.get("start_url", "") or ""
    if start_url:
        # T66.8 SSRF: 跟 /agent/run /discover 一样检查 start_url
        daemon._check_url(start_url, where="v1_query")
    return daemon.owner.run(daemon._run_semantic_query(args))


def handle_v1_query_stream(daemon: Any, args: dict[str, Any], req: Any) -> str:
    """POST /v1/query/stream — SSE streaming progress."""
    # T67+ T68: /v1/query/stream
    start_url = args.get("start_url", "") or ""
    if start_url:
        daemon._check_url(start_url, where="v1_query.stream")
    if req is None:
        raise ValueError("/v1/query/stream requires req context")
    daemon._stream_semantic_query(req, args)
    return "_SSE_HANDLED"


def handle_v1_query_stats(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /v1/query/stats — monitoring endpoint."""
    # T68: /v1/query/stats
    return daemon.owner.run(daemon._run_query_stats())


def handle_v1_query_log(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /v1/query/log — recent N query log entries (limit defaults to 50)."""
    # T76: GET /v1/query/log
    limit = int(args.get("limit", 50)) if str(args.get("limit", "")).isdigit() else 50
    return daemon._run_query_log(limit=limit)


def handle_v1_query_cache_clear(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /v1/query/cache/clear — flush shared SemanticQuery cache."""
    # T69: POST /v1/query/cache/clear
    if daemon._semantic_query is not None:
        return daemon._semantic_query.clear_cache()
    return {"cleared": 0, "remaining": 0, "note": "no shared SemanticQuery configured"}


# ── /llm endpoints ───────────────────────────────────────────────────

def handle_llm_stats(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /llm/stats — return LLM service counters."""
    from semantic_browser.llm import get_default_service
    return get_default_service().stats()


def handle_llm_slice(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /llm/slice — slice a page into LLM-compatible chunks."""
    return daemon.owner.run(daemon._llm_slice(args))


def handle_llm_summarize(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /llm/summarize — summarise a page."""
    return daemon.owner.run(daemon._llm_summarize(args))


def handle_llm_extract(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /llm/extract — extract structured data."""
    return daemon.owner.run(daemon._llm_extract(args))


def handle_llm_find_ref(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /llm/find-ref — find references on a page."""
    return daemon.owner.run(daemon._llm_find_ref(args))


# ── /memory endpoints ────────────────────────────────────────────────

def handle_memory_stats(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /memory/stats — cross-session goal memory stats."""
    from semantic_browser.memory.goal_memory import GoalMemory
    return GoalMemory().stats()


def handle_memory_list(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /memory/list — recent goal-memory entries."""
    from semantic_browser.memory.goal_memory import GoalMemory
    limit = int(args.get("limit", 20))
    return {"entries": GoalMemory().list_recent(limit)}


def handle_memory_clear(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /memory/clear — clear goal memory."""
    from semantic_browser.memory.goal_memory import GoalMemory
    GoalMemory().clear()
    return {"cleared": True}


# ── Registration ─────────────────────────────────────────────────────

_register("POST", "/v1/query", handle_v1_query)
_register("POST", "/v1/query/stream", handle_v1_query_stream)
_register("GET", "/v1/query/stats", handle_v1_query_stats)
_register("GET", "/v1/query/log", handle_v1_query_log)
_register("POST", "/v1/query/cache/clear", handle_v1_query_cache_clear)

_register("GET", "/llm/stats", handle_llm_stats)
_register("POST", "/llm/slice", handle_llm_slice)
_register("POST", "/llm/summarize", handle_llm_summarize)
_register("POST", "/llm/extract", handle_llm_extract)
_register("POST", "/llm/find-ref", handle_llm_find_ref)

_register("GET", "/memory/stats", handle_memory_stats)
_register("GET", "/memory/list", handle_memory_list)
_register("POST", "/memory/clear", handle_memory_clear)
