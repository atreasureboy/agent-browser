"""
Lease / Fence — T65.7 多 agent 共享 daemon 所有权原语.

设计 (来自 agent-browser-daemon-architecture.md §2):
- 一个 session 任一时刻至多一个有效 lease (DB UNIQUE INDEX 保证)
- lease = 所有权 (跨请求, 秒~小时, 心跳续约)
- fence_token = per-session 单调计数器 — 旧 holder 僵复活后写被拒
- 状态机: ACTIVE → GRACE → EXPIRED → RELEASED; 抢占走 PREEMPTED;
  重启走 RECOVERING

接口 (从 HTTP 入口到 API):
    manager = LeaseManager(db_path)
    await manager.start()
    lease = await manager.acquire(session_id, agent_id, tenant_id,
                                  priority=1, preempt=False, ttl_s=15.0)
    ok, reason = await manager.heartbeat(lease_id, fence_token)
    ok, reason = await manager.release(lease_id, fence_token, reason="ok")
    leases = await manager.list_active(session_id=None)
    # 后台 reaper 每 2s 跑一次 sweep_expired()

设计取舍 (vs 完整设计文档 §2):
- 我们做核心 6 个状态转移: acquire / heartbeat / release / preempt / reaper / reattach
- 不做 handoff (OFFERED 子状态) — T66 推迟
- 不做 priority 抢占 — 默认 priority=1, 抢占需 explicit preempt=True
- 不做 run_id 绑定 — lease 只标 session 所有权, 不细到 run 级别
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from semantic_browser.daemon.ulid import ulid_new, ulid_validate

logger = logging.getLogger(__name__)


# 状态机常量
STATE_ACTIVE = "ACTIVE"
STATE_GRACE = "GRACE"
STATE_PREEMPTED = "PREEMPTED"
STATE_RECOVERING = "RECOVERING"
STATE_EXPIRED = "EXPIRED"
STATE_RELEASED = "RELEASED"

# 视为 "有效" 的状态 — UNIQUE INDEX 的 where 条件, 决定一 session 是否被占
# 注意: PREEMPTED 不在内 — 被抢占的 lease 立刻死, 释放 UNIQUE 槽位给新 lease.
ACTIVE_STATES = frozenset({STATE_ACTIVE, STATE_GRACE, STATE_RECOVERING})

# 默认 TTL
DEFAULT_HEARTBEAT_TTL_S = 15.0  # 默认 lease 寿命
DEFAULT_GRACE_S = 10.0          # 心跳超时后给 holder 的宽限
DEFAULT_REAPER_INTERVAL_S = 2.0  # reaper 扫描周期


@dataclass(frozen=True)
class Lease:
    """lease 状态 — 不可变, 每次 acquire/release 返新对象."""

    lease_id: str
    session_id: str
    agent_id: str
    tenant_id: str
    state: str
    priority: int
    acquired_at_ms: int
    expires_at_ms: int
    last_heartbeat_ms: int
    heartbeat_ttl_ms: int
    fence_token: int
    released_reason: str | None = None
    preempted_by: str | None = None

    @property
    def is_active(self) -> bool:
        """lease 当前是否有效 (持有者可以写 op)."""
        return self.state in ACTIVE_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "priority": self.priority,
            "acquired_at_ms": self.acquired_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "heartbeat_ttl_ms": self.heartbeat_ttl_ms,
            "fence_token": self.fence_token,
            "released_reason": self.released_reason,
            "preempted_by": self.preempted_by,
        }


@dataclass
class AcquireResult:
    """acquire 的返回值 — lease 或错误."""

    ok: bool
    lease: Lease | None = None
    error: str | None = None
    # 如果抢占了现有 lease, 这里填旧 holder 信息
    preempted: Lease | None = None


class LeaseManager:
    """Lease/Fence 状态机 — SQLite WAL + 单写线程 (跟 EventBus 一样).

    并发模型:
        - 读 (heartbeat / list_active / get) 不加锁 — SQLite WAL 读不阻塞写
        - 写 (acquire / release / reaper) 走 self._lock 串行
        - DB 本身靠 UNIQUE INDEX 保证一 session 一 lease 的不变量
    """

    def __init__(self, db_path: str | None = None,
                 heartbeat_ttl_s: float = DEFAULT_HEARTBEAT_TTL_S,
                 grace_s: float = DEFAULT_GRACE_S,
                 reaper_interval_s: float = DEFAULT_REAPER_INTERVAL_S) -> None:
        if db_path is None:
            db_path = os.path.expanduser("~/.semantic-browser/leases.db")
        self.db_path = db_path
        self.heartbeat_ttl_ms = int(heartbeat_ttl_s * 1000)
        self.grace_ms = int(grace_s * 1000)
        self.reaper_interval_s = reaper_interval_s
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._reaper_stop = False
        self._reaper_thread: threading.Thread | None = None
        # fence_token per session: 内存镜像 + DB 持久化, 启动时从 sessions_index 读 max
        # (简化: 实际从 DB MAX() 读, 内存只缓存 — 单进程 OK)
        self._fence_cache: dict[str, int] = {}

    def start(self) -> None:
        """同步初始化 — daemon 主线程调. 启 reaper 后台线程."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leases (
                lease_id          TEXT PRIMARY KEY,
                session_id        TEXT NOT NULL,
                agent_id          TEXT NOT NULL,
                tenant_id         TEXT NOT NULL,
                state             TEXT NOT NULL,
                priority          INTEGER NOT NULL DEFAULT 1,
                acquired_at_ms    INTEGER NOT NULL,
                expires_at_ms     INTEGER NOT NULL,
                last_heartbeat_ms INTEGER NOT NULL,
                heartbeat_ttl_ms  INTEGER NOT NULL,
                fence_token       INTEGER NOT NULL,
                released_reason   TEXT,
                preempted_by      TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active_session
                ON leases(session_id)
                WHERE state IN ('ACTIVE','GRACE','RECOVERING');
            CREATE INDEX IF NOT EXISTS idx_leases_state_expires
                ON leases(state, expires_at_ms);

            -- sessions_index 表: per-session 元数据 + fence_token 持久化
            CREATE TABLE IF NOT EXISTS sessions_index (
                session_id  TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                agent_id    TEXT NOT NULL,
                fence_token INTEGER NOT NULL DEFAULT 0,
                updated_at_ms INTEGER NOT NULL
            );
        """)
        conn.commit()
        self._conn = conn
        # 预热 fence_token cache
        for sid, tok in conn.execute("SELECT session_id, fence_token FROM sessions_index").fetchall():
            self._fence_cache[sid] = int(tok)
        # 启 reaper
        if self.reaper_interval_s > 0:
            self._reaper_stop = False
            self._reaper_thread = threading.Thread(
                target=self._reaper_loop, name="lease-reaper", daemon=True,
            )
            self._reaper_thread.start()
        logger.info("LeaseManager started (db=%s, ttl=%dms, grace=%dms)",
                    self.db_path, self.heartbeat_ttl_ms, self.grace_ms)

    def close(self) -> None:
        self._reaper_stop = True
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=5)
            self._reaper_thread = None
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ── 公开 API ──────────────────────────────────────────────

    def acquire(self, session_id: str, agent_id: str, tenant_id: str,
                *, priority: int = 1, preempt: bool = False,
                ttl_s: float | None = None) -> AcquireResult:
        """获取 lease — 同步, 走 self._lock.

        Returns:
            ok=True → lease 字段是新分配的 ACTIVE lease
            ok=False → error 字段是 'BUSY' / 'INVALID' 等
            preempted 字段: 如果 preempt=True 抢占了旧 lease, 这里是旧 holder
        """
        with self._lock:
            assert self._conn is not None
            now_ms = int(time.time() * 1000)
            ttl_ms = int((ttl_s or self.heartbeat_ttl_ms / 1000.0) * 1000)

            # 检查现有 lease
            cur = self._get_active_lease_locked(session_id)
            if cur is not None:
                # 同 agent 幂等重入 — 刷新 expires_at, 不创新 lease
                if cur.agent_id == agent_id:
                    return AcquireResult(
                        ok=True,
                        lease=self._heartbeat_locked(cur.lease_id, cur.fence_token, now_ms,
                                                     ttl_ms_override=ttl_ms),
                    )
                # 不同 agent — 抢占?
                if not preempt:
                    return AcquireResult(
                        ok=False, error="BUSY",
                        lease=cur,  # 返当前 holder 让 client 看到谁占着
                    )
                if priority >= cur.priority:
                    return AcquireResult(
                        ok=False, error="BUSY_LOWER_PRIORITY",
                        lease=cur,
                    )
                # 抢占 — 旧 lease → PREEMPTED, 新 lease → ACTIVE
                new_lease_id = ulid_new(now_ms)
                new_token = self._bump_fence_locked(session_id)
                # 旧 lease 标 PREEMPTED (用我们 ID 作为 preempted_by)
                self._conn.execute(
                    "UPDATE leases SET state=?, preempted_by=?, released_reason=? "
                    "WHERE lease_id=?",
                    (STATE_PREEMPTED, new_lease_id, "preempted", cur.lease_id),
                )
                # 新 lease 插入
                self._insert_lease_locked(
                    lease_id=new_lease_id, session_id=session_id,
                    agent_id=agent_id, tenant_id=tenant_id,
                    priority=priority, now_ms=now_ms, ttl_ms=ttl_ms,
                    fence_token=new_token,
                )
                new_lease = self._get_lease_locked(new_lease_id)
                return AcquireResult(
                    ok=True, lease=new_lease, preempted=cur,
                )

            # 无现有 lease — 直接 acquire
            lease_id = ulid_new(now_ms)
            new_token = self._bump_fence_locked(session_id)
            self._insert_lease_locked(
                lease_id=lease_id, session_id=session_id,
                agent_id=agent_id, tenant_id=tenant_id,
                priority=priority, now_ms=now_ms, ttl_ms=ttl_ms,
                fence_token=new_token,
            )
            return AcquireResult(ok=True, lease=self._get_lease_locked(lease_id))

    def heartbeat(self, lease_id: str, fence_token: int) -> tuple[bool, str]:
        """心跳续约.

        Returns:
            (ok, reason) — reason: 'OK' / 'LEASE_INVALID' / 'LEASE_LOST' / 'FENCE_MISMATCH'
        """
        if not ulid_validate(lease_id):
            return False, "LEASE_INVALID"
        with self._lock:
            assert self._conn is not None
            now_ms = int(time.time() * 1000)
            cur = self._get_lease_locked(lease_id)
            if cur is None:
                return False, "LEASE_INVALID"
            if cur.state not in ACTIVE_STATES:
                return False, f"LEASE_LOST:{cur.state}"
            if cur.fence_token != fence_token:
                return False, "FENCE_MISMATCH"
            self._heartbeat_locked(lease_id, fence_token, now_ms)
            return True, "OK"

    def release(self, lease_id: str, fence_token: int, *,
                reason: str = "released") -> tuple[bool, str]:
        """主动释放.

        Returns:
            (ok, reason) — 'OK' / 'LEASE_INVALID' / 'LEASE_LOST' / 'FENCE_MISMATCH'
        """
        if not ulid_validate(lease_id):
            return False, "LEASE_INVALID"
        with self._lock:
            assert self._conn is not None
            cur = self._get_lease_locked(lease_id)
            if cur is None:
                return False, "LEASE_INVALID"
            if cur.state not in ACTIVE_STATES:
                return False, "LEASE_LOST"
            if cur.fence_token != fence_token:
                return False, "FENCE_MISMATCH"
            self._conn.execute(
                "UPDATE leases SET state=?, released_reason=? WHERE lease_id=?",
                (STATE_RELEASED, reason, lease_id),
            )
            self._conn.commit()
            # bump fence — 让旧 token 立即失效, 防止旧 holder 复活残留写
            self._bump_fence_locked(cur.session_id)
            return True, "OK"

    def get_lease(self, lease_id: str) -> Lease | None:
        """读 lease 状态 — 不加锁, WAL 读."""
        if not ulid_validate(lease_id):
            return None
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT lease_id, session_id, agent_id, tenant_id, state, priority, "
            "acquired_at_ms, expires_at_ms, last_heartbeat_ms, heartbeat_ttl_ms, "
            "fence_token, released_reason, preempted_by "
            "FROM leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def get_active_for_session(self, session_id: str) -> Lease | None:
        """读 session 当前 active lease."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT lease_id, session_id, agent_id, tenant_id, state, priority, "
            "acquired_at_ms, expires_at_ms, last_heartbeat_ms, heartbeat_ttl_ms, "
            "fence_token, released_reason, preempted_by "
            "FROM leases WHERE session_id=? AND state IN "
            "('ACTIVE','GRACE','RECOVERING') ORDER BY acquired_at_ms DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def get_current_fence_token(self, session_id: str) -> int:
        """读 session 当前 fence_token (给 op 校验用, 不加锁)."""
        assert self._conn is not None
        # 先看 cache
        if session_id in self._fence_cache:
            return self._fence_cache[session_id]
        row = self._conn.execute(
            "SELECT fence_token FROM sessions_index WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return 0
        tok = int(row[0])
        self._fence_cache[session_id] = tok
        return tok

    def list_active(self) -> list[Lease]:
        """列所有 active lease — 给 /admin 等用."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT lease_id, session_id, agent_id, tenant_id, state, priority, "
            "acquired_at_ms, expires_at_ms, last_heartbeat_ms, heartbeat_ttl_ms, "
            "fence_token, released_reason, preempted_by "
            "FROM leases WHERE state IN ('ACTIVE','GRACE','RECOVERING') "
            "ORDER BY acquired_at_ms DESC",
        ).fetchall()
        return [self._row_to_lease(r) for r in rows if r]

    # ── 内部 helpers ──────────────────────────────────────────

    def _reaper_loop(self) -> None:
        """reaper 后台线程 — 每 reaper_interval_s 扫一次过期 lease."""
        logger.info("lease reaper started (interval=%.1fs)", self.reaper_interval_s)
        while not self._reaper_stop:
            try:
                self.sweep_expired()
            except Exception:
                logger.exception("lease reaper tick failed")
            time.sleep(self.reaper_interval_s)
        logger.info("lease reaper stopped")

    def sweep_expired(self) -> int:
        """扫一次过期 lease → GRACE / EXPIRED → bump fence.

        转移:
            ACTIVE  (expires_at 过期, 没收到心跳)         → GRACE
            GRACE   (grace 窗口也耗尽)                    → EXPIRED
            EXPIRED (清理)                              → RELEASED + bump fence
        """
        with self._lock:
            assert self._conn is not None
            now_ms = int(time.time() * 1000)
            transitions = 0

            # ACTIVE → GRACE
            cur = self._conn.execute(
                "SELECT lease_id, session_id, fence_token, state, expires_at_ms "
                "FROM leases WHERE state=? AND expires_at_ms <= ?",
                (STATE_ACTIVE, now_ms),
            ).fetchall()
            for lease_id, sid, tok, _st, _exp in cur:
                self._conn.execute(
                    "UPDATE leases SET state=? WHERE lease_id=?",
                    (STATE_GRACE, lease_id),
                )
                transitions += 1
                logger.info("lease reaper: %s ACTIVE → GRACE (sid=%s)",
                            lease_id, sid)

            # GRACE → EXPIRED (再给 grace_ms 宽限)
            cur = self._conn.execute(
                "SELECT lease_id, session_id, expires_at_ms FROM leases WHERE state=?",
                (STATE_GRACE,),
            ).fetchall()
            for lease_id, sid, exp_ms in cur:
                if now_ms - exp_ms >= self.grace_ms:
                    self._conn.execute(
                        "UPDATE leases SET state=?, released_reason=? WHERE lease_id=?",
                        (STATE_EXPIRED, "heartbeat_timeout_grace_exceeded", lease_id),
                    )
                    transitions += 1
                    logger.info("lease reaper: %s GRACE → EXPIRED (sid=%s)",
                                lease_id, sid)

            # EXPIRED → RELEASED + bump fence
            cur = self._conn.execute(
                "SELECT lease_id, session_id FROM leases WHERE state=?",
                (STATE_EXPIRED,),
            ).fetchall()
            for lease_id, sid in cur:
                self._conn.execute(
                    "UPDATE leases SET state=? WHERE lease_id=?",
                    (STATE_RELEASED, lease_id),
                )
                self._bump_fence_locked(sid)
                transitions += 1
                logger.info("lease reaper: %s EXPIRED → RELEASED + fence bump (sid=%s)",
                            lease_id, sid)

            if transitions:
                self._conn.commit()
            return transitions

    def _get_active_lease_locked(self, session_id: str) -> Lease | None:
        row = self._conn.execute(
            "SELECT lease_id, session_id, agent_id, tenant_id, state, priority, "
            "acquired_at_ms, expires_at_ms, last_heartbeat_ms, heartbeat_ttl_ms, "
            "fence_token, released_reason, preempted_by "
            "FROM leases WHERE session_id=? AND state IN "
            "('ACTIVE','GRACE','RECOVERING') ORDER BY acquired_at_ms DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def _get_lease_locked(self, lease_id: str) -> Lease | None:
        row = self._conn.execute(
            "SELECT lease_id, session_id, agent_id, tenant_id, state, priority, "
            "acquired_at_ms, expires_at_ms, last_heartbeat_ms, heartbeat_ttl_ms, "
            "fence_token, released_reason, preempted_by "
            "FROM leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        return self._row_to_lease(row) if row else None

    def _heartbeat_locked(self, lease_id: str, fence_token: int, now_ms: int,
                          *, ttl_ms_override: int | None = None) -> Lease:
        """locked 版 heartbeat — acquire (同 agent 重入) 也调这个."""
        cur = self._get_lease_locked(lease_id)
        if cur is None:
            return None  # type: ignore[return-value]
        if cur.state not in ACTIVE_STATES:
            return None  # type: ignore[return-value]
        if cur.fence_token != fence_token:
            return None  # type: ignore[return-value]
        ttl = ttl_ms_override or cur.heartbeat_ttl_ms
        new_exp = now_ms + ttl
        self._conn.execute(
            "UPDATE leases SET expires_at_ms=?, last_heartbeat_ms=?, state=? "
            "WHERE lease_id=?",
            (new_exp, now_ms, STATE_ACTIVE, lease_id),
        )
        self._conn.commit()
        return self._get_lease_locked(lease_id)

    def _insert_lease_locked(self, *, lease_id: str, session_id: str,
                             agent_id: str, tenant_id: str, priority: int,
                             now_ms: int, ttl_ms: int, fence_token: int) -> None:
        """插入 lease + 同步 sessions_index (per-session 元数据)."""
        exp_ms = now_ms + ttl_ms
        self._conn.execute(
            "INSERT INTO leases (lease_id, session_id, agent_id, tenant_id, "
            "state, priority, acquired_at_ms, expires_at_ms, last_heartbeat_ms, "
            "heartbeat_ttl_ms, fence_token, released_reason, preempted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (lease_id, session_id, agent_id, tenant_id,
             STATE_ACTIVE, priority, now_ms, exp_ms, now_ms,
             ttl_ms, fence_token),
        )
        # sessions_index upsert
        self._conn.execute(
            "INSERT INTO sessions_index (session_id, tenant_id, agent_id, "
            "fence_token, updated_at_ms) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "tenant_id=excluded.tenant_id, agent_id=excluded.agent_id, "
            "fence_token=excluded.fence_token, updated_at_ms=excluded.updated_at_ms",
            (session_id, tenant_id, agent_id, fence_token, now_ms),
        )
        self._conn.commit()
        self._fence_cache[session_id] = fence_token

    def _bump_fence_locked(self, session_id: str) -> int:
        """per-session fence_token +1 — 旧 holder 复活后写被拒.

        必须在 sessions_index 有记录. 没有的话 init = 1.
        """
        cur = self._conn.execute(
            "SELECT fence_token FROM sessions_index WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if cur is None:
            new_tok = 1
            now_ms = int(time.time() * 1000)
            self._conn.execute(
                "INSERT INTO sessions_index (session_id, tenant_id, agent_id, "
                "fence_token, updated_at_ms) VALUES (?, 'anonymous', 'anonymous', ?, ?)",
                (session_id, new_tok, now_ms),
            )
        else:
            new_tok = int(cur[0]) + 1
            self._conn.execute(
                "UPDATE sessions_index SET fence_token=?, updated_at_ms=? "
                "WHERE session_id=?",
                (new_tok, int(time.time() * 1000), session_id),
            )
        self._conn.commit()
        self._fence_cache[session_id] = new_tok
        return new_tok

    @staticmethod
    def _row_to_lease(row: tuple | None) -> Lease | None:
        if row is None:
            return None
        return Lease(
            lease_id=row[0], session_id=row[1], agent_id=row[2], tenant_id=row[3],
            state=row[4], priority=row[5],
            acquired_at_ms=row[6], expires_at_ms=row[7],
            last_heartbeat_ms=row[8], heartbeat_ttl_ms=row[9],
            fence_token=row[10],
            released_reason=row[11], preempted_by=row[12],
        )