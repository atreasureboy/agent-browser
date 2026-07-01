"""
Workflow runner — T9: multi-step action sequences as first-class objects.

让 Agent 把"流程"作为 JSON 持久化、复用、版本控制 (vs 把每步硬编码进脚本)。

Schema (单个 step):
  {"action": "open", "url": "..."}
  {"action": "click", "ref": "e3"}
  {"action": "type", "ref": "e4", "text": "..."}
  {"action": "wait", "kind": "text|ref|url", "target": "...", "timeout_ms": 10000}
  {"action": "scroll", "direction": "down", "amount": 500}
  {"action": "press", "key": "Enter"}
  {"action": "back"} / {"action": "forward"} / {"action": "reload"}
  {"action": "snapshot"}             → 把 snapshot 存入 result.snapshots[N]
  {"action": "extract"}              → 把 article markdown 存入 result.articles[N]
  {"action": "screenshot", "path": "..."}   → 存 bytes 路径 (或省略)
  {"action": "note", "text": "..."}        → 附加到当前 URL
  {"action": "new_tab", "url": "..."}
  {"action": "switch_tab", "index": 0}
  {"action": "close_tab", "index": 0}

错误策略 (T9.1):
  默认 on_error: "stop" — 一步失败立即终止
  on_error: "continue" — 跳过失败的步, 把错误写入 step_result.error

返回 WorkflowResult:
  {
    "workflow": "name",
    "total_steps": 7,
    "executed_steps": 5,
    "status": "completed" | "failed" | "partial",
    "steps": [
      {"index": 0, "action": "open", "ok": true, "duration_ms": 234},
      ...
    ],
    "snapshots": [snap_dict, ...],   # 仅 snapshot 步
    "articles": [md, ...],           # 仅 extract 步
  }
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from semantic_browser.browser.controller import BrowserController

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStepResult:
    index: int
    action: str
    ok: bool
    duration_ms: float
    data: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class WorkflowResult:
    name: str
    total_steps: int
    executed_steps: int
    status: str  # "completed" | "failed" | "partial"
    steps: list[WorkflowStepResult] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)
    articles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.name,
            "total_steps": self.total_steps,
            "executed_steps": self.executed_steps,
            "status": self.status,
            "steps": [
                {
                    "index": s.index, "action": s.action, "ok": s.ok,
                    "duration_ms": round(s.duration_ms, 1),
                    **({"data": s.data} if s.data is not None else {}),
                    **({"error": s.error} if s.error else {}),
                }
                for s in self.steps
            ],
            "snapshots": self.snapshots,
            "articles": self.articles,
        }


class WorkflowRunner:
    """执行 workflow JSON。复用 SemanticBrowser 单例, 不开新浏览器。"""

    def __init__(self, controller: BrowserController) -> None:
        self.controller = controller

    async def run(self, workflow: dict[str, Any]) -> WorkflowResult:
        name = workflow.get("name", "unnamed")
        steps = workflow.get("steps", [])
        on_error = workflow.get("on_error", "stop")
        result = WorkflowResult(name=name, total_steps=len(steps), executed_steps=0, status="running")

        for i, step in enumerate(steps):
            action = step.get("action", "")
            t0 = time.time()
            ok, data, err = await self._exec_step(action, step, result)
            duration = (time.time() - t0) * 1000
            step_result = WorkflowStepResult(
                index=i, action=action, ok=ok, duration_ms=duration,
                data=data, error=err,
            )
            result.steps.append(step_result)
            if ok:
                result.executed_steps += 1
            elif on_error == "stop":
                result.status = "failed"
                logger.warning("Workflow %r stopped at step %d (%s): %s",
                               name, i, action, err)
                break
            # else: continue, 错误已记 step_result.error

        if result.status == "running":
            # 跑完所有步但可能有错
            any_fail = any(not s.ok for s in result.steps)
            result.status = "partial" if any_fail else "completed"
        return result

    async def _exec_step(self, action: str, step: dict[str, Any], result: WorkflowResult) -> tuple[bool, Any, Optional[str]]:
        """执行单步; 返回 (ok, data_for_step, error_msg)."""
        try:
            if action == "open":
                await self.controller.open(step["url"])
                return True, None, None
            if action == "click":
                ok = await self.controller.click(step["ref"])
                return ok, {"ref": step["ref"]}, None if ok else "click failed (ref may be invalid)"
            if action == "type":
                ok = await self.controller.type_text(step["ref"], step["text"])
                return ok, {"ref": step["ref"], "len": len(step["text"])}, None if ok else "type failed"
            if action == "press":
                await self.controller.press_key(step["key"])
                return True, {"key": step["key"]}, None
            if action == "wait":
                kind = step["kind"]
                target = step["target"]
                timeout = int(step.get("timeout_ms", 10000))
                if kind == "text":
                    ok = await self.controller.wait_for_text(target, timeout_ms=timeout)
                elif kind == "ref":
                    ok = await self.controller.wait_for_ref(target, timeout_ms=timeout)
                elif kind == "url":
                    ok = await self.controller.wait_for_url(target, timeout_ms=timeout)
                else:
                    return False, None, f"unknown wait kind: {kind}"
                return ok, {"kind": kind, "target": target}, None if ok else f"{kind} {target!r} timeout {timeout}ms"
            if action == "scroll":
                direction = step.get("direction", "down")
                amount = int(step.get("amount", 500))
                await self.controller.scroll(direction, amount)
                return True, {"direction": direction, "amount": amount}, None
            if action == "back":
                await self.controller.back()
                return True, None, None
            if action == "forward":
                await self.controller.forward()
                return True, None, None
            if action == "reload":
                await self.controller.reload()
                return True, None, None
            if action == "snapshot":
                # 拿 fresh snapshot; 复用 SemanticBrowser 路径更稳 (含 classification)
                from semantic_browser.engine import SemanticBrowser
                sb = SemanticBrowser.__new__(SemanticBrowser)
                sb.controller = self.controller
                # 但 sb.start_session 需要 db — 跳过, 直接调 SnapshotEngine
                from semantic_browser.snapshot.engine import SnapshotEngine
                page = self.controller.current_page
                if page is None:
                    return False, None, "no current page"
                snap = await SnapshotEngine(page).capture(base_url=page.url)
                snap_dict = snap.to_dict()
                result.snapshots.append(snap_dict)
                return True, {"text_blocks": len(snap.text_blocks),
                              "links": len(snap.links),
                              "controls": len(snap.controls)}, None
            if action == "extract":
                from semantic_browser.extractor.content import ContentExtractor
                page = self.controller.current_page
                if page is None:
                    return False, None, "no current page"
                article = await ContentExtractor(page).extract_article()
                md = article.to_markdown()
                result.articles.append(md)
                return True, {"title": article.title, "sections": len(article.sections),
                              "chars": article.text_length}, None
            if action == "screenshot":
                path = step.get("path")
                data = await self.controller.screenshot(path=path)
                return True, {"path": path, "bytes": len(data)}, None
            if action == "note":
                # 需 memory store; 通过 controller 自己注入; 暂简化为占位
                return True, {"text": step["text"], "note": "TODO: persist"}, None
            if action == "new_tab":
                await self.controller.new_tab(step.get("url", ""))
                return True, {"url": step.get("url", "")}, None
            if action == "switch_tab":
                await self.controller.switch_tab(int(step["index"]))
                return True, {"index": step["index"]}, None
            if action == "close_tab":
                idx = int(step["index"]) if "index" in step else None
                await self.controller.close_tab(idx)
                return True, {"index": idx}, None
            return False, None, f"unknown action: {action!r}"
        except Exception as e:
            return False, None, f"{type(e).__name__}: {e}"


def load_workflow(path: str | Path) -> dict[str, Any]:
    """从 JSON 文件加载 workflow。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"workflow file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("workflow root must be a JSON object")
    if "steps" not in data or not isinstance(data["steps"], list):
        raise ValueError("workflow must have a 'steps' array")
    return data