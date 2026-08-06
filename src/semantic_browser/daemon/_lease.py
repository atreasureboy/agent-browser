"""Lease/Handoff mixin extracted from TransparentBrowserDaemon.

Contains lease acquire/renew/release/get, reattach, handoff offer/accept,
and storage_state read handlers.  The daemon class multi-inherits this.

Also defines ``_LeaseError`` (moved from server.py) to avoid circular imports.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from semantic_browser.daemon.routers import DEFAULT_AGENT, DEFAULT_TENANT, SessionError

logger = logging.getLogger(__name__)


# ── Lease exception ────────────────────────────────────────────────

class _LeaseError(Exception):
    """T65.7: Lease 操作失败的业务异常 — 带 code + optional holder info."""

    def __init__(self, code: str, message: str, *,
                 holder: dict[str, Any] | None = None,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.holder = holder
        self.status_code = status_code


# ── LeaseMixin ─────────────────────────────────────────────────────

class _LeaseMixin:
    """Lease/fence/handoff handlers — mixed into TransparentBrowserDaemon."""

    # Type hints for attributes provided by TransparentBrowserDaemon
    lease_manager: Any
    owner: Any
    event_bus: Any
    snapshot_store: Any

    # ── Lease acquire ──────────────────────────────────────────────

    def _handle_lease_acquire(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/lease — 获取/抢占 lease."""
        m = re.match(r"^/sessions/([^/]+)/lease$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        agent_id = args.get("agent_id") or ""
        if not agent_id:
            raise _LeaseError("MISSING_PARAM", "agent_id required")
        requested_tenant = args.get("tenant_id") or DEFAULT_TENANT
        existing = self.lease_manager.get_session_meta(session_name)
        if existing is not None and existing[0] != DEFAULT_TENANT:
            tenant_id = existing[0]
            if requested_tenant != tenant_id:
                raise _LeaseError(
                    "TENANT_IMMUTABLE",
                    f"session {session_name!r} belongs to tenant {tenant_id!r}; "
                    f"cannot acquire under {requested_tenant!r}",
                    status_code=403,
                )
        else:
            tenant_id = requested_tenant
        try:
            priority = int(args.get("priority", "1"))
        except ValueError:
            priority = 1
        preempt = str(args.get("preempt", "")).lower() in ("1", "true", "yes")
        ttl_s: float | None = None
        if args.get("ttl_s"):
            try:
                ttl_s = float(args["ttl_s"])
            except ValueError:
                pass

        result = self.lease_manager.acquire(
            session_id=session_name, agent_id=agent_id, tenant_id=tenant_id,
            priority=priority, preempt=preempt, ttl_s=ttl_s,
        )
        if not result.ok:
            raise _LeaseError(result.error or "UNKNOWN", f"acquire failed: {result.error}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=409)
        out: dict[str, Any] = {"lease": result.lease.to_dict()}
        if result.preempted:
            out["preempted"] = result.preempted.to_dict()
        meta = self.owner.get_session_meta(session_name) or {}
        if not meta or meta.get("tenant_id") == DEFAULT_TENANT:
            self.owner.set_session_meta(session_name, tenant_id=tenant_id, agent_id=agent_id)
        try:
            self.event_bus.publish(
                "session.lease.acquired",
                {"session_id": session_name,
                 "lease_id": result.lease.lease_id,
                 "fence_token": result.lease.fence_token,
                 "agent_id": agent_id,
                 "preempted_lease_id": (
                     result.preempted.lease_id if result.preempted else None),
                 "priority": priority,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=tenant_id,
                producer_kind="agent", producer_id=agent_id,
                dedup_key=f"lease_acquired:{result.lease.lease_id}",
                persistent=True,
            )
        except Exception:
            logger.exception("lease acquire: failed to publish event")
        return out

    # ── Lease renew ────────────────────────────────────────────────

    def _handle_lease_renew(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/lease/{lease_id}/renew — 心跳续约."""
        m = re.match(r"^/sessions/([^/]+)/lease/([^/]+)/renew$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        lease_id = m.group(2)
        fence_token_str = args.get("fence_token", "0")
        try:
            fence_token = int(fence_token_str)
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        ok, reason = self.lease_manager.heartbeat(lease_id, fence_token)
        if not ok:
            raise _LeaseError(reason, f"heartbeat failed: {reason}", status_code=409)
        cur = self.lease_manager.get_lease(lease_id)
        return {"lease": cur.to_dict() if cur else None}

    # ── Lease release ──────────────────────────────────────────────

    def _handle_lease_release(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """DELETE /sessions/{name}/lease/{lease_id} — 主动释放."""
        m = re.match(r"^/sessions/([^/]+)/lease/([^/]+)$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        lease_id = m.group(2)
        fence_token_str = args.get("fence_token", "0")
        try:
            fence_token = int(fence_token_str)
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        reason = args.get("reason") or "released"
        ok, r = self.lease_manager.release(lease_id, fence_token, reason=reason)
        if not ok:
            raise _LeaseError(r, f"release failed: {r}", status_code=409)
        cur = self.lease_manager.get_lease(lease_id)
        try:
            self.event_bus.publish(
                "session.lease.released",
                {"session_id": session_name,
                 "lease_id": lease_id,
                 "fence_token": fence_token,
                 "reason": reason,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=(cur.tenant_id if cur else DEFAULT_TENANT),
                producer_kind="agent",
                producer_id=(cur.agent_id if cur else "anonymous"),
                dedup_key=f"lease_released:{lease_id}",
                persistent=True,
            )
        except Exception:
            logger.exception("lease release: failed to publish event")
        return {"lease_id": lease_id, "state": "RELEASED", "reason": reason}

    # ── Lease get ──────────────────────────────────────────────────

    def _handle_lease_get(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """GET /sessions/{name}/lease — 看当前 active lease."""
        m = re.match(r"^/sessions/([^/]+)/lease$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        cur = self.lease_manager.get_active_for_session(session_name)
        return {
            "session_id": session_name,
            "lease": cur.to_dict() if cur else None,
        }

    # ── Reattach ───────────────────────────────────────────────────

    def _handle_lease_reattach(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/reattach — daemon 重启后恢复所有权."""
        m = re.match(r"^/sessions/([^/]+)/reattach$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        lease_id = args.get("lease_id") or ""
        if not lease_id:
            raise _LeaseError("MISSING_PARAM", "lease_id required")
        try:
            fence_token = int(args.get("fence_token", "0"))
        except ValueError:
            raise _LeaseError("MISSING_PARAM", "fence_token required (int)")
        agent_id = args.get("agent_id") or ""

        cur = self.lease_manager.get_lease(lease_id)
        if cur is None:
            raise _LeaseError("LEASE_INVALID", f"lease {lease_id!r} not found",
                              status_code=404)
        tenant_id = cur.tenant_id
        effective_agent = agent_id or cur.agent_id
        result = self.lease_manager.reattach(
            lease_id=lease_id, fence_token=fence_token,
            agent_id=effective_agent, tenant_id=tenant_id,
        )
        if not result.ok:
            err = result.error or "UNKNOWN"
            status = 410 if err in ("LEASE_LOST", "LEASE_INVALID") else 409
            raise _LeaseError(err, f"reattach failed: {err}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=status)
        now_ms = int(time.time() * 1000)
        age_ms = max(0, now_ms - result.lease.acquired_at_ms)
        advice: str | None = None
        if age_ms > 300_000:
            advice = "re_verify_auth"
        try:
            self.owner.run(self.owner.aget_controller(session_name))
        except Exception:
            pass
        self.event_bus.publish(
            "session.restored",
            {"session_id": session_name, "lease_id": lease_id,
             "fence_token": result.lease.fence_token,
             "agent_id": effective_agent, "age_ms": age_ms, "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=effective_agent,
            dedup_key=f"restore:{lease_id}:{result.lease.fence_token}",
            persistent=True,
        )
        return {
            "recovered": True,
            "lease": result.lease.to_dict(),
            "age_ms": age_ms,
            "advice": advice,
        }

    # ── Handoff offer ──────────────────────────────────────────────

    def _handle_handoff_offer(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/handoff — 当前 holder A 主动让渡给 B."""
        m = re.match(r"^/sessions/([^/]+)/handoff$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        to_agent = args.get("agent_id") or ""
        if not to_agent:
            raise _LeaseError("MISSING_PARAM", "agent_id required (the recipient)")
        ttl_s = 30.0
        if args.get("ttl_s"):
            try:
                ttl_s = float(args["ttl_s"])
            except ValueError:
                pass
        cur = self.lease_manager.get_active_for_session(session_name)
        if cur is None:
            raise _LeaseError("LEASE_INVALID",
                              f"no active lease for session {session_name!r}",
                              status_code=404)
        tenant_id = args.get("tenant_id") or cur.tenant_id
        ok, offer_token, err, deadline_ms = self.lease_manager.offer(
            session_id=session_name, from_agent=cur.agent_id, to_agent=to_agent,
            tenant_id=tenant_id, ttl_s=ttl_s,
        )
        if not ok:
            status = 409 if err == "BUSY" else 410
            raise _LeaseError(err or "UNKNOWN",
                              f"handoff offer failed: {err}",
                              holder=cur.to_dict(), status_code=status)
        try:
            self.event_bus.publish(
                "session.handoff.offered",
                {"session_id": session_name,
                 "from_agent": cur.agent_id,
                 "to_agent": to_agent,
                 "offer_token": offer_token,
                 "deadline_ms": deadline_ms,
                 "ttl_s": ttl_s,
                 "ts": time.time()},
                scope="session", scope_id=session_name,
                tenant_id=tenant_id,
                producer_kind="agent", producer_id=cur.agent_id,
                dedup_key=f"handoff_offered:{offer_token}",
                persistent=True,
            )
        except Exception:
            logger.exception("handoff offer: failed to publish event")
        return {
            "offer_token": offer_token,
            "expires_at_ms": deadline_ms,
            "offered_to": to_agent,
        }

    # ── Handoff accept ─────────────────────────────────────────────

    def _handle_handoff_accept(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """POST /sessions/{name}/handoff/accept — B 用 offer_token 接受."""
        m = re.match(r"^/sessions/([^/]+)/handoff/accept$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        offer_token = args.get("offer_token") or ""
        if not offer_token:
            raise _LeaseError("MISSING_PARAM", "offer_token required")
        to_agent = args.get("agent_id") or ""
        if not to_agent:
            raise _LeaseError("MISSING_PARAM", "agent_id required (must match offer_to)")
        cur = self.lease_manager.get_active_for_session(session_name)
        if cur is None:
            raise _LeaseError("LEASE_INVALID",
                              f"no active offer for session {session_name!r}",
                              status_code=404)
        tenant_id = cur.tenant_id

        result = self.lease_manager.accept_handoff(
            session_id=session_name, to_agent=to_agent,
            offer_token=offer_token, tenant_id=tenant_id,
        )
        if not result.ok:
            err = result.error or "UNKNOWN"
            status = 410 if err in ("LEASE_LOST", "OFFER_NOT_FOUND",
                                   "OFFER_EXPIRED", "LEASE_INVALID") else 409
            raise _LeaseError(err,
                              f"handoff accept failed: {err}",
                              holder=result.lease.to_dict() if result.lease else None,
                              status_code=status)
        out: dict[str, Any] = {"lease": result.lease.to_dict()}
        if result.preempted:
            out["acquired_from"] = result.preempted.lease_id
        self.owner.set_session_meta(session_name,
                                    tenant_id=result.lease.tenant_id,
                                    agent_id=to_agent)
        self.event_bus.publish(
            "session.handed_off",
            {"session_id": session_name,
             "from_agent": result.preempted.agent_id if result.preempted else None,
             "to_agent": to_agent,
             "new_lease_id": result.lease.lease_id,
             "fence_token": result.lease.fence_token,
             "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=result.lease.tenant_id,
            producer_kind="agent", producer_id=to_agent,
            dedup_key=f"handoff:{result.lease.lease_id}",
            persistent=True,
        )
        return out

    # ── Storage state read ─────────────────────────────────────────

    def _handle_storage_state(self, path: str, args: dict[str, Any]) -> dict[str, Any]:
        """GET /sessions/{name}/storage_state — 读最新 storage_state 快照."""
        m = re.match(r"^/sessions/([^/]+)/storage_state$", path)
        if not m:
            raise ValueError(f"bad path: {path}")
        session_name = m.group(1)
        snap = self.snapshot_store.latest_snapshot(session_name)
        if snap is None:
            raise SessionError("SNAPSHOT_NOT_FOUND",
                                f"no snapshot for session {session_name!r}")
        content_bytes = json.dumps(snap["content"], ensure_ascii=False,
                                   sort_keys=True).encode("utf-8")
        content_sha = hashlib.sha256(content_bytes).hexdigest()
        idx = self.lease_manager.get_session_meta(session_name)
        if idx is not None:
            tenant_id, agent_id = idx
        else:
            meta = self.owner.get_session_meta(session_name) or {}
            tenant_id = meta.get("tenant_id", DEFAULT_TENANT)
            agent_id = meta.get("agent_id", DEFAULT_AGENT)
        self.event_bus.publish(
            "session.storage_state.exported",
            {"session_id": session_name, "snapshot_id": snap["snapshot_id"],
             "size_bytes": snap["size_bytes"], "content_sha256": content_sha[:16],
             "ts": time.time()},
            scope="session", scope_id=session_name,
            tenant_id=tenant_id,
            producer_kind="agent", producer_id=agent_id,
            dedup_key=f"ss_export:{session_name}:{content_sha[:16]}",
            persistent=True,
        )
        return snap
