"""
T27: Goal-level memory — 跨 session 缓存 goal 执行结果.

设计目标:
- 同一个 goal (或近义 goal) 跑第二次时, 命中 cache 直接返回答案.
- 失败时持久化失败模式, 下次类似 goal 可以提前避坑.
- 简单模糊匹配 (keyword overlap) — 不用 embedding, 避免引入重型依赖.

存储: JSON 文件 ~/.semantic_browser/goal_memory.json (轻量, 易调试).
容量上限 500 条, LRU 淘汰.

为啥单独建文件: MemoryStore 是 SQLite, 适合结构化 browsing history;
goal memory 是松散 key→value, JSON 简单够用.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / ".semantic_browser" / "goal_memory.json"
MAX_ENTRIES = 500  # 上限, 避免无限增长


def _normalize(text: str) -> set[str]:
    """Lowercase, 去标点, 按空格切, 留 ≥2 字符 token. 用于模糊匹配."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = {t for t in text.split() if len(t) >= 2}
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度: |A∩B| / |A∪B|. 0..1."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class GoalMemory:
    """跨 session 的 goal → answer / failure 缓存.

    用法:
        mem = GoalMemory()
        cached = mem.lookup("find contact email of example.com")
        if cached:
            return cached["answer"]
        result = await agent.run(goal)
        mem.record(goal, result)
    """

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open() as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("goal_memory.json not a list, ignoring")
                return []
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("failed to load goal_memory.json: %s", e)
            return []

    def _save(self) -> None:
        # 截断到 MAX_ENTRIES (LRU: 按 last_used 排序, 保留最近)
        if len(self._entries) > MAX_ENTRIES:
            self._entries.sort(key=lambda e: e.get("last_used", 0), reverse=True)
            self._entries = self._entries[:MAX_ENTRIES]
        try:
            with self.path.open("w") as f:
                json.dump(self._entries, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("failed to save goal_memory.json: %s", e)

    def lookup(self, goal: str, threshold: float = 0.7) -> Optional[dict[str, Any]]:
        """模糊匹配已有 goal. 返回最相似 entry (或 None).

        threshold: Jaccard 相似度阈值, 默认 0.7 (高 — 避免误命中).
        只返回 success=True 的 (失败记录用于避坑, 不直接返回).
        返回的 dict 含 goal/answer/success/steps/timestamp/last_used.
        """
        goal_tokens = _normalize(goal)
        if not goal_tokens:
            return None
        best: Optional[dict[str, Any]] = None
        best_score = 0.0
        for entry in self._entries:
            if not entry.get("success"):
                continue
            score = _jaccard(goal_tokens, _normalize(entry.get("goal", "")))
            if score > best_score:
                best_score = score
                best = entry
        if best is not None and best_score >= threshold:
            best["last_used"] = time.time()
            best["hit_count"] = best.get("hit_count", 0) + 1
            self._save()
            logger.info("goal_memory hit (score=%.2f): %s", best_score, best["goal"])
            return best
        return None

    def record(
        self,
        goal: str,
        success: bool,
        answer: Optional[str] = None,
        steps: int = 0,
        reason: str = "",
    ) -> None:
        """记录一次 goal 执行结果."""
        # 去重: 如果已有相似 goal, 更新 (而不是新增)
        existing = self.lookup(goal, threshold=0.9)
        if existing:
            existing["success"] = success
            existing["answer"] = answer
            existing["steps"] = steps
            existing["reason"] = reason
            existing["last_used"] = time.time()
            existing["timestamp"] = time.time()
        else:
            self._entries.append({
                "goal": goal,
                "success": success,
                "answer": answer,
                "steps": steps,
                "reason": reason,
                "timestamp": time.time(),
                "last_used": time.time(),
                "hit_count": 0,
            })
        self._save()

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """列出最近 N 条 (调试用)."""
        return sorted(
            self._entries, key=lambda e: e.get("last_used", 0), reverse=True,
        )[:limit]

    def stats(self) -> dict[str, Any]:
        total = len(self._entries)
        hits = sum(e.get("hit_count", 0) for e in self._entries)
        succ = sum(1 for e in self._entries if e.get("success"))
        return {
            "total": total,
            "success": succ,
            "failure": total - succ,
            "total_hits": hits,
            "path": str(self.path),
        }

    def clear(self) -> None:
        self._entries = []
        self._save()