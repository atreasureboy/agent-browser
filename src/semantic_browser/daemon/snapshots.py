"""T61: storage_state 自动快照 (fable §5.4).

设计:
- SnapshotStore 把每个 session 的 cookies/localStorage 周期性快照到文件系统 + SQLite 索引
- 触发 (debounce): navigate success + cookie change + 60s 定时 sweep (若 dirty)
- 单份上限 2MB (truncate 最大的 localStorage key)
- 每 session 保留 3 份 (再多 GC)
- 失败不重试, 只记 metrics
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# 单份上限 (fable §5.4): 2MB; 超限截断最大 localStorage key
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
# 每 session 保留 3 份
_RETENTION_COUNT = 3


class SnapshotStore:
    """T61: 持久化 session storage_state 快照.

    公开接口:
      store = SnapshotStore(root_dir, db_path)
      store.mark_dirty(session_id)        # 标记 session 有变更
      store.take_snapshot(session_id, controller)  # 抓取并落盘
      store.sweep_dirty(...)              # 一次扫所有 dirty
      store.list_snapshots(session_id)    # 列出该 session 的快照 (最新在前)
      store.gc_old_snapshots(session_id)  # GC 旧快照, 保留 3 份
      store.load_snapshot(snapshot_id)    # 读快照 (audit / 调试用)
    """

    def __init__(self, root_dir: str | None = None, db_path: str | None = None) -> None:
        if root_dir is None:
            root_dir = os.path.expanduser("~/.semantic-browser/snapshots")
        if db_path is None:
            db_path = os.path.expanduser("~/.semantic-browser/snapshot_index.db")
        self.root_dir = Path(root_dir)
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(self.root_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_snapshots (
                snapshot_id  TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                taken_at     REAL NOT NULL,
                trigger      TEXT NOT NULL,
                size_bytes   INTEGER NOT NULL,
                open_pages   TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                truncated    INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_session
                ON session_snapshots(session_id, taken_at DESC);
        """)
        self._conn.commit()
        # dirty 集合: in-memory + 持久化到 SQLite row, 避免重启丢失.
        self._dirty: set[str] = set()
        self._dirty_lock = threading.Lock()

    def mark_dirty(self, session_id: str) -> None:
        """T61: 标记 session 有变更 — 下次 sweep 该抓快照."""
        with self._dirty_lock:
            self._dirty.add(session_id)

    def is_dirty(self, session_id: str) -> bool:
        with self._dirty_lock:
            return session_id in self._dirty

    def clear_dirty(self, session_id: str) -> None:
        with self._dirty_lock:
            self._dirty.discard(session_id)

    def dirty_sessions(self) -> set[str]:
        with self._dirty_lock:
            return set(self._dirty)

    async def take_snapshot(self, session_id: str, controller: Any,
                            trigger: str = "auto_sweep") -> Optional[str]:
        """T61: 抓取 controller.storage_state() → 落盘 + 索引.

        Returns: snapshot_id (str) or None if failed / nothing to save.
        """
        try:
            state = await controller._context.storage_state()
        except Exception as e:
            logger.warning("snapshot: storage_state() failed for %s: %s", session_id, e)
            return None
        # open_pages: 视化恢复辅助, 记当前所有活跃 tab URL
        try:
            pages = controller._context.pages
            urls = []
            for p in pages:
                try:
                    if not p.is_closed():
                        urls.append(p.url)
                except Exception:
                    pass
            open_pages = json.dumps(urls, ensure_ascii=False)
        except Exception:
            open_pages = "[]"
        # 大小裁剪 — 超 2MB 截断最大 localStorage key
        truncated = 0
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        size_bytes = len(body)
        if size_bytes > _MAX_SNAPSHOT_BYTES:
            try:
                truncated, state = self._truncate_state(state, _MAX_SNAPSHOT_BYTES)
                body = json.dumps(state, ensure_ascii=False).encode("utf-8")
                size_bytes = len(body)
                logger.info(
                    "snapshot: truncated to %d bytes (orig > 2MB) for %s",
                    size_bytes, session_id,
                )
            except Exception:
                logger.exception("snapshot: truncate failed")
        # 写文件
        snapshot_id = f"ss_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
        sess_dir = self.root_dir / session_id
        sess_dir.mkdir(parents=True, exist_ok=True)
        file_path = sess_dir / f"{snapshot_id}.json"
        try:
            file_path.write_bytes(body)
        except Exception:
            logger.exception("snapshot: write failed %s", file_path)
            return None
        # 索引到 SQLite
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO session_snapshots"
                    "(snapshot_id, session_id, taken_at, trigger, size_bytes, open_pages, file_path, truncated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (snapshot_id, session_id, time.time(), trigger,
                     size_bytes, open_pages, str(file_path), int(truncated)),
                )
                self._conn.commit()
            except Exception:
                logger.exception("snapshot: index failed; removing file %s", file_path)
                try:
                    file_path.unlink()
                except OSError:
                    pass
                return None
        self.clear_dirty(session_id)
        logger.info("snapshot: saved %s for %s (size=%d, trigger=%s)",
                    snapshot_id, session_id, size_bytes, trigger)
        return snapshot_id

    @staticmethod
    def _truncate_state(state: dict, max_bytes: int) -> tuple[int, dict]:
        """T61: state 太大时截断 — 删最大 localStorage.origins[i].localStorage[j].value.
        Returns: (1 if truncated else 0, new_state)
        """
        if not isinstance(state.get("origins"), list):
            return 0, state
        # 找最大的 key+value
        biggest = (-1, -1, "")
        for oi, origin in enumerate(state["origins"]):
            lss = origin.get("localStorage") if isinstance(origin, dict) else None
            if not isinstance(lss, list):
                continue
            for ki, kv in enumerate(lss):
                if isinstance(kv, dict) and isinstance(kv.get("value"), str):
                    sz = len(kv["value"])
                    if sz > biggest[0]:
                        biggest = (sz, oi, ki)
        if biggest[0] < 0:
            return 0, state
        _, oi, ki = biggest
        try:
            state["origins"][oi]["localStorage"][ki]["value"] = ""
        except Exception:
            return 0, state
        return 1, state

    def gc_old_snapshots(self, session_id: str) -> int:
        """T61: GC — 仅保留 RETENTION_COUNT 份最新, 删其余."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot_id, file_path FROM session_snapshots "
                "WHERE session_id = ? ORDER BY taken_at DESC",
                (session_id,),
            ).fetchall()
            if len(rows) <= _RETENTION_COUNT:
                return 0
            to_remove = rows[_RETENTION_COUNT:]
            for sid, fpath in to_remove:
                try:
                    Path(fpath).unlink()
                except OSError:
                    pass
                try:
                    self._conn.execute("DELETE FROM session_snapshots WHERE snapshot_id = ?",
                                       (sid,))
                except Exception:
                    logger.exception("snapshot: GC delete failed for %s", sid)
            self._conn.commit()
        return len(to_remove)

    def list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot_id, taken_at, trigger, size_bytes, truncated, open_pages "
                "FROM session_snapshots WHERE session_id = ? ORDER BY taken_at DESC",
                (session_id,),
            ).fetchall()
        return [
            {"snapshot_id": sid, "taken_at": ts, "trigger": trig,
             "size_bytes": sz, "truncated": bool(trunc), "open_pages": pages}
            for sid, ts, trig, sz, trunc, pages in rows
        ]

    def load_snapshot(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, file_path FROM session_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        if not row:
            return None
        sid, fpath = row
        try:
            content = Path(fpath).read_bytes()
            return {"snapshot_id": snapshot_id, "session_id": sid,
                    "content": json.loads(content)}
        except Exception:
            return None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
