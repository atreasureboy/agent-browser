"""Agent route handlers — extracted from server.py _dispatch.

POST /agent/run         → run agent (blocking)
POST /agent/run/stream  → run agent with SSE streaming
POST /agent/plan        → dry-run plan preview
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


def handle_agent_run(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /agent/run — run agent (blocking).

    T66.8: SSRF guard — start_url is checked before running the agent.
    """
    start_url = args.get("start_url", "")
    if start_url:
        daemon._check_url(start_url, where="agent_run")
    return daemon.owner.run(daemon._run_agent(args))


def handle_agent_run_stream(daemon: Any, args: dict[str, Any], req: Any) -> str:
    """POST /agent/run/stream — run agent with SSE streaming.

    T53: Reuses on_step hook to push step-by-step progress via SSE.
    T66.8: SSRF guard — start_url is checked before running the agent.
    """
    if req is None:
        raise ValueError("/agent/run/stream requires req context")
    start_url = args.get("start_url", "")
    if start_url:
        daemon._check_url(start_url, where="agent_run.stream")
    daemon._stream_agent_run(req, args)
    return "_SSE_HANDLED"


def handle_agent_plan(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /agent/plan — dry-run plan preview.

    T29: Returns the agent's execution plan without actually running it.
    """
    return daemon.owner.run(daemon._plan_agent(args))


_register("POST", "/agent/run", handle_agent_run)
_register("POST", "/agent/run/stream", handle_agent_run_stream)
_register("POST", "/agent/plan", handle_agent_plan)
