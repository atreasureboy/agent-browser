"""Admin, health, and session CRUD route handlers — extracted from server.py _dispatch.

Route coverage:
  /health              GET  — full health check with context
  /healthz             GET  — liveness probe
  /readyz              GET  — readiness probe
  /queue               GET  — current op queue state
  /state               GET  — current session state
  /capacity            GET  — capacity + degradation state
  /metrics             GET  — Prometheus metrics (raw text)
  /admin/degrade       POST — set degradation level
  /admin/restore       POST — restore to L0
  /admin/drain         POST — trigger graceful drain
  /admin/drain/cancel  POST — cancel drain
  /sessions            GET  — list sessions (+ tenant filter + detail mode)
  /sessions            POST — create session
  /sessions/{name}     DELETE — release session
  /stats               GET  — memory store stats
  /notes               GET  — notes (with optional url filter)
  /note                POST — save note
"""
from __future__ import annotations

import time as _time
from typing import Any

from semantic_browser.daemon.routers import _register


# ── Health / probes ──────────────────────────────────────────────

def handle_health(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /health — full health check with context."""
    return daemon._handle_health_full()


def handle_healthz(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /healthz — liveness probe."""
    import os
    return {
        "alive": True,
        "pid": os.getpid(),
        "uptime_seconds": round(_time.time() - daemon.started_at, 1),
    }


def handle_readyz(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /readyz — readiness probe."""
    return daemon._handle_readyz()


# ── Queue / state / capacity / metrics ──────────────────────────

def handle_queue(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /queue — current op queue state."""
    now = _time.time()
    with daemon._op_waiters_lock:
        waiters_snap = daemon._op_waiters
    return {
        "current_op": daemon._current_op,
        "running_for_s": round(now - daemon._op_started_at, 2) if daemon._op_started_at else None,
        "lock_held": daemon.owner.op_lock.locked(),
        "waiters": waiters_snap,
        "lock_timeout_s": 30.0,  # _OP_LOCK_TIMEOUT_S
    }


def handle_state(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /state — current session state."""
    return daemon.owner.run(daemon._state(session=args.get("session")))


def handle_capacity(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /capacity — capacity + degradation state."""
    sessions = daemon.owner.list_sessions()
    M = daemon._capacity_m_browsers
    K = daemon._capacity_max_contexts
    slots_total = M * K
    mem_per_browser_mb, mem_total_mb, mem_high_watermark_mb = daemon._compute_mem_budget()
    with daemon._classify_cache_lock:
        cache_size_snapshot = len(daemon._classify_cache)
    return {
        "sessions_active": len(sessions),
        "sessions_max": K,
        "capacity_ratio": round(len(sessions) / max(slots_total, 1), 3),
        "degradation_level": daemon._degradation_level,
        "degradation_label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][daemon._degradation_level],
        "pressure_level": daemon._pressure_level or "normal",
        "M": M,
        "K": K,
        "slots_total": slots_total,
        "mem_per_browser_estimate_mb": mem_per_browser_mb,
        "mem_total_estimate_mb": mem_total_mb,
        "mem_high_watermark_mb": mem_high_watermark_mb,
        "watchdog_heartbeat_age_s": round(_time.time() - daemon._last_heartbeat_ts, 1)
            if daemon._last_heartbeat_ts else None,
        "llm_classify_calls": daemon._classify_llm_calls,
        "llm_classify_failures": daemon._classify_llm_failures,
        "llm_classify_failure_rate": (
            round(daemon._classify_llm_failures / max(daemon._classify_llm_calls, 1), 3)
            if daemon._classify_llm_calls else None
        ),
        "classify_cache_size": cache_size_snapshot,
        "classify_cache_hits": daemon._classify_cache_hits,
        "tenants": {
            tid: sum(1 for m in daemon.owner._session_meta.values()
                     if m["tenant_id"] == tid)
            for tid in {m["tenant_id"] for m in daemon.owner._session_meta.values()}
        },
    }


def handle_metrics(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /metrics — Prometheus metrics (raw text)."""
    body = daemon.metrics.render_prometheus()
    uptime = _time.time() - daemon.started_at
    body += f"tb_daemon_uptime_seconds {uptime:.2f}\n"
    daemon._send_raw(req, 200, body, "text/plain; version=0.0.4; charset=utf-8")
    return "_RAW_HANDLED"


# ── Admin: degrade / restore / drain ─────────────────────────────

def handle_admin_degrade(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /admin/degrade — set degradation level."""
    import logging
    level = int(args.get("level", 1))
    if level < 1 or level > 4:
        raise ValueError(f"degradation level must be 1..4, got {level}")
    daemon._degradation_level = level
    logging.getLogger(__name__).warning("DegradationController: admin set to L%d", level)
    pressure = {1: "high", 2: "high", 3: "critical", 4: "critical"}.get(level, "high")
    daemon._emit_pressure_event(pressure, reason=f"admin_degrade_L{level}")
    return {"level": level, "label": ["L0_healthy", "L1_reject_new", "L2_preempt_low", "L3_readonly", "L4_full"][level]}


def handle_admin_restore(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /admin/restore — restore to L0."""
    import logging
    daemon._degradation_level = 0
    logging.getLogger(__name__).info("DegradationController: admin restored to L0")
    daemon._emit_pressure_event("normal", reason="admin_restore")
    return {"level": 0, "label": "L0_healthy"}


def handle_admin_drain(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /admin/drain — trigger graceful drain."""
    daemon._begin_drain()
    return {"draining": True, "in_flight": daemon._current_op,
            "drain_started_at": daemon._drain_started_at,
            "drain_timeout_s": daemon._drain_timeout_s}


def handle_admin_drain_cancel(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /admin/drain/cancel — cancel drain."""
    import logging
    import time
    was_draining = daemon._draining
    daemon._draining = False
    daemon._drain_started_at = None
    try:
        daemon.event_bus.publish(
            "daemon.drain.cancelled",
            {"ts": time.time(), "was_draining": was_draining},
            scope="global", tenant_id="anonymous",
            dedup_key=f"drain_cancel:{int(time.time())}",
            persistent=True,
        )
    except Exception:
        logging.getLogger(__name__).exception("drain cancel: failed to publish event")
    return {"draining": False, "was_draining": was_draining}


# ── Sessions CRUD ────────────────────────────────────────────────

def handle_sessions_list(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /sessions — list sessions (+ tenant filter + detail mode)."""
    tenant_filter = args.get("tenant_id")
    if tenant_filter:
        sessions = daemon.owner.list_sessions_for_tenant(tenant_filter)
    else:
        sessions = daemon.owner.list_sessions()
    detail = str(args.get("detail", "")).lower() in ("1", "true", "yes")
    if detail:
        items = []
        for s in sessions:
            entry: dict[str, Any] = {"name": s}
            meta = daemon.owner.get_session_meta(s)
            if meta:
                entry["tenant_id"] = meta["tenant_id"]
                entry["agent_id"] = meta["agent_id"]
                entry["created_at"] = meta["created_at"]
            try:
                ctrl = daemon.owner.run(daemon.owner.aget_controller(s))
                entry["url"] = daemon.owner.run(ctrl.get_url())
                entry["title"] = daemon.owner.run(ctrl.get_title())
            except Exception:
                entry["url"] = None
                entry["title"] = None
            items.append(entry)
        resp: dict[str, Any] = {
            "sessions": items, "active_count": len(items), "detail": True,
        }
        if tenant_filter:
            resp["tenant_id"] = tenant_filter
        return resp
    metadata: dict[str, dict[str, Any]] = {}
    for s in sessions:
        meta = daemon.owner.get_session_meta(s)
        if meta:
            metadata[s] = {
                "tenant_id": meta["tenant_id"],
                "agent_id": meta["agent_id"],
            }
    resp_simple: dict[str, Any] = {
        "sessions": sessions, "active_count": len(sessions),
        "metadata": metadata,
    }
    if tenant_filter:
        resp_simple["tenant_id"] = tenant_filter
    return resp_simple


def handle_sessions_create(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /sessions — create a new session."""
    import time
    import logging
    from semantic_browser.daemon.routers import DEFAULT_TENANT, DEFAULT_AGENT, SessionError
    name = args.get("name") or f"agent-{len(daemon.owner.list_sessions()) + 1}"
    requested_tenant = args.get("tenant_id") or DEFAULT_TENANT
    requested_agent = args.get("agent_id") or DEFAULT_AGENT
    try:
        _ = daemon.owner.get_controller(name)
    except Exception as e:
        raise SessionError("SESSION_CREATE_FAILED", f"{type(e).__name__}: {e}") from None
    existing = daemon.lease_manager.get_session_meta(name)
    if existing is not None and existing[0] != DEFAULT_TENANT:
        tenant_id, agent_id = existing
        if requested_tenant != tenant_id:
            raise SessionError(
                "TENANT_IMMUTABLE",
                f"session {name!r} belongs to tenant {tenant_id!r}; "
                f"cannot rebind to {requested_tenant!r}",
            )
        agent_id = requested_agent
    else:
        tenant_id, agent_id = requested_tenant, requested_agent
    daemon.owner.set_session_meta(name, tenant_id=tenant_id, agent_id=agent_id)
    try:
        daemon.event_bus.publish(
            "session.created",
            {"session_id": name, "tenant_id": tenant_id,
             "agent_id": agent_id, "ts": time.time()},
            scope="session", scope_id=name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=agent_id,
            dedup_key=f"session_created:{name}",
            persistent=True,
        )
    except Exception:
        logging.getLogger(__name__).exception("session create: failed to publish event")
    return {
        "name": name, "created": True,
        "tenant_id": tenant_id, "agent_id": agent_id,
        "active": daemon.owner.list_sessions(),
    }


# ── Stats / Notes ────────────────────────────────────────────────

def handle_stats(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /stats — memory store stats."""
    return daemon.memory_store.stats()


def handle_notes(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /notes — notes (with optional url filter)."""
    url = args.get("url", "")
    limit = int(args.get("limit", 50))
    if url:
        rows = daemon.memory_store.get_notes(url)[:limit]
        return {"count": len(rows), "notes": rows}
    with daemon.memory_store._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        notes_list = [dict(r) for r in rows]
    return {"count": len(notes_list), "notes": notes_list}


def handle_note(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /note — save a note."""
    url = args["url"]
    note = args["note"]
    daemon.memory_store.add_note(url, note)
    return {"saved": True, "url": url}


# ── Registration ─────────────────────────────────────────────────

_register("GET", "/health", handle_health)
_register("GET", "/healthz", handle_healthz)
_register("GET", "/readyz", handle_readyz)
_register("GET", "/queue", handle_queue)
_register("GET", "/state", handle_state)
_register("GET", "/capacity", handle_capacity)
_register("GET", "/metrics", handle_metrics)
_register("POST", "/admin/degrade", handle_admin_degrade)
_register("POST", "/admin/restore", handle_admin_restore)
_register("POST", "/admin/drain", handle_admin_drain)
_register("POST", "/admin/drain/cancel", handle_admin_drain_cancel)
_register("GET", "/sessions", handle_sessions_list)
_register("POST", "/sessions", handle_sessions_create)
_register("GET", "/stats", handle_stats)
_register("GET", "/notes", handle_notes)
_register("POST", "/note", handle_note)
