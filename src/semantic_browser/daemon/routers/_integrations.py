"""Integration catalog route handler (super_plan Round 2d).

The LangChain / AutoGen / Aider adapters previously had no entry point at
all — they were only reachable by importing Python modules directly. This
endpoint exposes a machine-readable catalog so agents and operators can
discover available adapters, their entry points, and install status over
HTTP.
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


def handle_integrations(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /v1/integrations — list framework adapters with metadata."""
    from semantic_browser.integrations import integration_catalog
    adapters = integration_catalog()
    return {
        "adapters": adapters,
        "count": len(adapters),
        "installed_count": sum(1 for a in adapters if a["installed"]),
    }


_register("GET", "/v1/integrations", handle_integrations)
