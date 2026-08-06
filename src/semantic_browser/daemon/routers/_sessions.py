"""Session lease, handoff, reattach, and storage_state route handlers.

These endpoints use dynamic path segments (session names embedded in URLs),
so they are dispatched by regex matching in server.py's _dispatch fallback.

Each handler receives the raw path via args["_dispatch_path"] and delegates
to the corresponding daemon method which extracts the session name.

Route coverage:
  POST   /sessions/{name}/lease              — acquire/preempt lease
  POST   /sessions/{name}/lease/{id}/renew   — renew lease
  DELETE /sessions/{name}/lease/{id}         — release lease
  GET    /sessions/{name}/lease              — read lease state
  POST   /sessions/{name}/reattach           — reattach after crash/restart
  POST   /sessions/{name}/handoff            — offer handoff to peer
  POST   /sessions/{name}/handoff/accept     — accept handoff
  GET    /sessions/{name}/storage_state      — read storage state snapshot
  DELETE /sessions/{name}                    — release session
"""
from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


def handle_lease_acquire(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions/{name}/lease — acquire or preempt a lease."""
    return daemon._handle_lease_acquire(args.get("_dispatch_path", ""), args)


def handle_lease_renew(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions/{name}/lease/{id}/renew — renew a lease."""
    return daemon._handle_lease_renew(args.get("_dispatch_path", ""), args)


def handle_lease_release(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """DELETE /sessions/{name}/lease/{id} — release a lease."""
    return daemon._handle_lease_release(args.get("_dispatch_path", ""), args)


def handle_lease_get(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /sessions/{name}/lease — read lease state."""
    return daemon._handle_lease_get(args.get("_dispatch_path", ""), args)


def handle_reattach(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions/{name}/reattach — reattach after crash/restart."""
    return daemon._handle_lease_reattach(args.get("_dispatch_path", ""), args)


def handle_handoff_offer(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions/{name}/handoff — offer handoff to peer."""
    return daemon._handle_handoff_offer(args.get("_dispatch_path", ""), args)


def handle_handoff_accept(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions/{name}/handoff/accept — accept handoff."""
    return daemon._handle_handoff_accept(args.get("_dispatch_path", ""), args)


def handle_storage_state(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /sessions/{name}/storage_state — read storage state snapshot."""
    return daemon._handle_storage_state(args.get("_dispatch_path", ""), args)


def handle_session_delete(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """DELETE /sessions/{name} — release a session."""
    import time, logging
    from semantic_browser.daemon.routers import DEFAULT_TENANT, DEFAULT_AGENT, DEFAULT_SESSION, SessionError
    path = args.get("_dispatch_path", "")
    name = path[len("/sessions/"):]
    if not name:
        raise SessionError("MISSING_PARAM", "session name required after /sessions/")
    if name == DEFAULT_SESSION:
        raise SessionError("CANNOT_DELETE_DEFAULT", "cannot delete default session")
    idx = daemon.lease_manager.get_session_meta(name)
    if idx is not None:
        tenant_id, agent_id = idx
    else:
        meta = daemon.owner.get_session_meta(name) or {}
        tenant_id = meta.get("tenant_id", DEFAULT_TENANT)
        agent_id = meta.get("agent_id", DEFAULT_AGENT)
    released = daemon.owner.release_session(name)
    if not released:
        raise SessionError("SESSION_NOT_FOUND", f"session {name!r} not found")
    try:
        daemon.event_bus.publish(
            "session.deleted",
            {"session_id": name, "tenant_id": tenant_id,
             "agent_id": agent_id, "ts": time.time()},
            scope="session", scope_id=name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=agent_id,
            dedup_key=f"session_deleted:{name}",
            persistent=True,
        )
    except Exception:
        logging.getLogger(__name__).exception("session delete: failed to publish event")
    return {"name": name, "released": True, "active": daemon.owner.list_sessions()}


# Registration — these are used by the dynamic route fallback in _dispatch.
# {name} → session name, {lease_id} → lease UUID (present in renew + delete-lease).
_register("POST",   "/sessions/{name}/lease",              handle_lease_acquire)
_register("POST",   "/sessions/{name}/lease/{lease_id}/renew", handle_lease_renew)
_register("DELETE", "/sessions/{name}/lease/{lease_id}",   handle_lease_release)
_register("GET",    "/sessions/{name}/lease",              handle_lease_get)
_register("POST",   "/sessions/{name}/reattach",           handle_reattach)
_register("POST",   "/sessions/{name}/handoff",            handle_handoff_offer)
_register("POST",   "/sessions/{name}/handoff/accept",     handle_handoff_accept)
_register("GET",    "/sessions/{name}/storage_state",      handle_storage_state)
_register("DELETE", "/sessions/{name}",                    handle_session_delete)
