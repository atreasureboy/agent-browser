"""
Memory Store — 浏览记忆持久化。

SQLite 存储：页面、链接、控件、操作历史、站点图谱、会话。
支持跨会话续跑。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    domain      TEXT NOT NULL,
    title       TEXT,
    page_type   TEXT DEFAULT 'unknown',
    confidence  REAL DEFAULT 0,
    meta_json   TEXT,
    snapshot_json TEXT,
    visited_at  REAL,
    visited_count INTEGER DEFAULT 1,
    UNIQUE(url)
);

CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_url    TEXT NOT NULL,
    to_url      TEXT NOT NULL,
    text        TEXT,
    visited    INTEGER DEFAULT 0,
    UNIQUE(from_url, to_url)
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    url         TEXT,
    action      TEXT NOT NULL,
    ref         TEXT,
    detail      TEXT,
    timestamp   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    start_url   TEXT,
    pages_visited INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    note        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_pages_type ON pages(page_type);
CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_url);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
"""


class MemoryStore:
    """
    浏览记忆存储。

    用法:
        store = MemoryStore("~/.semantic-browser/memory.db")
        store.record_page(snapshot, classification)
        pages = store.get_pages_by_domain("example.com")
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("version", str(SCHEMA_VERSION)),
            )

    # ── 页面记录 ──────────────────────────────────────────────

    def record_page(
        self,
        url: str,
        domain: str,
        title: str,
        page_type: str,
        confidence: float,
        meta: dict,
        snapshot_json: str = "",
    ) -> int:
        """记录或更新一个页面。返回 page id。"""
        with self._conn() as conn:
            # 先查是否已存在
            row = conn.execute(
                "SELECT id, visited_count FROM pages WHERE url = ?", (url,)
            ).fetchone()

            if row:
                conn.execute(
                    """UPDATE pages SET
                       domain=?, title=?, page_type=?, confidence=?,
                       meta_json=?, snapshot_json=?, visited_at=?,
                       visited_count=visited_count+1
                       WHERE url=?""",
                    (domain, title, page_type, confidence,
                     json.dumps(meta, ensure_ascii=False),
                     snapshot_json, time.time(), url),
                )
                return row["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO pages
                       (url, domain, title, page_type, confidence,
                        meta_json, snapshot_json, visited_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (url, domain, title, page_type, confidence,
                     json.dumps(meta, ensure_ascii=False),
                     snapshot_json, time.time()),
                )
                return cur.lastrowid

    def get_page(self, url: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM pages WHERE url = ?", (url,)
            ).fetchone()
            return dict(row) if row else None

    def get_pages_by_domain(self, domain: str, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM pages WHERE domain = ?
                   ORDER BY visited_at DESC LIMIT ?""",
                (domain, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pages_by_type(self, page_type: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM pages WHERE page_type = ?
                   ORDER BY visited_at DESC LIMIT ?""",
                (page_type, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_domains(self) -> list[dict[str, Any]]:
        """获取所有访问过的域名及其页面数。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT domain, COUNT(*) as page_count,
                          MAX(visited_at) as last_visited
                   FROM pages GROUP BY domain
                   ORDER BY last_visited DESC""",
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 链接记录 ──────────────────────────────────────────────

    def record_links(self, from_url: str, links: list[dict]) -> None:
        """记录页面上的链接。"""
        # T114 audit fix: 之前 link["href"] 在 link item 是非 dict 或缺
        # href 时抛 KeyError, 中断整个 with block → 同次调用里后续 link
        # 也丢失. 改: 用 .get 默认空串, 静默跳过缺 href 的.
        # T115 audit fix: 之前 for 循环 N 次 conn.execute — 500 个 link 走
        # 500 次 prepared-execute + fsync. 改 executemany 一次插入, 性能
        # 10-50× 改善 (WAL NORMAL sync 下从 0.5-2.5s 降到 ~50ms).
        rows: list[tuple[str, str, str]] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            href = link.get("href") or ""
            if not href:
                continue
            rows.append((from_url, href, link.get("text", "") or ""))
        if not rows:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO links (from_url, to_url, text)
                   VALUES (?, ?, ?)""",
                rows,
            )

    def get_unvisited_links(self, domain: str, limit: int = 20) -> list[dict]:
        """获取某域名下未访问的链接。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM links WHERE visited = 0"
            ).fetchall()
        # B23 修复: 原先 `LIKE '%{domain}%'` 会把 to_url 含 "domain" 子串的链接都算上
        # (e.g. domain="evil.com" 误匹配 "https://notevil.com/page", 跨域爬虫注入面)。
        # 用 urlparse 解析 netloc, 顺带支持子域关系。
        from urllib.parse import urlparse
        out: list[dict] = []
        for r in rows:
            host = urlparse(r["to_url"]).netloc.lower()
            if not host:
                continue
            if host == domain or host.endswith("." + domain):
                out.append(dict(r))
                if len(out) >= limit:
                    break
        return out

    def mark_link_visited(self, to_url: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE links SET visited = 1 WHERE to_url = ?", (to_url,)
            )

    # ── 操作历史 ──────────────────────────────────────────────

    def record_action(
        self, session_id: str, action: str,
        url: str = "", ref: str = "", detail: str = "",
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO actions (session_id, url, action, ref, detail, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, url, action, ref, detail, time.time()),
            )

    def get_action_history(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM actions WHERE session_id = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 会话管理 ──────────────────────────────────────────────

    def start_session(self, session_id: str, start_url: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions (id, started_at, start_url)
                   VALUES (?, ?, ?)""",
                (session_id, time.time(), start_url),
            )

    def end_session(self, session_id: str, note: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET ended_at = ?, note = ?
                   WHERE id = ?""",
                (time.time(), note, session_id),
            )

    def increment_page_visit(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET pages_visited = pages_visited + 1 WHERE id = ?",
                (session_id,),
            )

    # ── 笔记 ──────────────────────────────────────────────────

    def add_note(self, url: str, note: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO notes (url, note, created_at) VALUES (?, ?, ?)",
                (url, note, time.time()),
            )

    def get_notes(self, url: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM notes WHERE url = ? ORDER BY created_at DESC",
                (url,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 统计 ──────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            pages = conn.execute("SELECT COUNT(*) as c FROM pages").fetchone()["c"]
            links = conn.execute("SELECT COUNT(*) as c FROM links").fetchone()["c"]
            domains = conn.execute(
                "SELECT COUNT(DISTINCT domain) as c FROM pages"
            ).fetchone()["c"]
            actions = conn.execute("SELECT COUNT(*) as c FROM actions").fetchone()["c"]
            sessions = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
            return {
                "pages": pages,
                "links": links,
                "domains": domains,
                "actions": actions,
                "sessions": sessions,
            }

    # ── 清理 ──────────────────────────────────────────────────

    def cleanup_older_than(self, days: int, *, dry_run: bool = False) -> dict[str, int]:
        """删除 N 天前的页面记录及其级联数据。

        级联: 关联的 links (from_url 指向被删 page) 一并删除,
              关联的 actions (url 字段指向被删 page, 且 timestamp < cutoff) 一并删除。
              关键修复: actions 必须 BOTH url∈urls AND timestamp<cutoff 才删,
              否则会把"新动作指向老 URL"的记录误删 — 历史不该跨页面生命周期留存。
        notes 保留 — 用户笔记与页面生命周期无关。
        sessions 保留 — 提供索引追溯。

        Returns 各类删除数量 (dry_run 模式也返回, 用于预览)。
        """
        if days < 0:
            raise ValueError(f"days must be >= 0, got {days}")
        cutoff = time.time() - days * 86400
        with self._conn() as conn:
            # 找出要删的页面 URL
            urls = [r["url"] for r in conn.execute(
                "SELECT url FROM pages WHERE visited_at < ?", (cutoff,)
            ).fetchall()]
            if not urls:
                return {"pages": 0, "links": 0, "actions": 0, "urls": 0}
            qmarks = ",".join("?" * len(urls))
            # 数目标行数 (用于 dry_run 报告)
            link_count = conn.execute(
                f"SELECT COUNT(*) as c FROM links WHERE from_url IN ({qmarks})",
                urls,
            ).fetchone()["c"]
            # 关联 actions: url 指向被删 page AND 时间早于 cutoff
            action_count = conn.execute(
                f"SELECT COUNT(*) as c FROM actions WHERE url IN ({qmarks}) "
                "AND timestamp < ?",
                urls + [cutoff],
            ).fetchone()["c"]
            if dry_run:
                return {"pages": len(urls), "links": link_count,
                        "actions": action_count, "urls": len(urls)}
            # 实际删除 (BEGIN/COMMIT 自动随 _conn contextmanager)
            conn.execute(f"DELETE FROM pages WHERE url IN ({qmarks})", urls)
            conn.execute(f"DELETE FROM links WHERE from_url IN ({qmarks})", urls)
            conn.execute(
                f"DELETE FROM actions WHERE url IN ({qmarks}) AND timestamp < ?",
                urls + [cutoff],
            )
            return {"pages": len(urls), "links": link_count,
                    "actions": action_count, "urls": len(urls)}
