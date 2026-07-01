"""
T21: LLM-driven autonomous loop — agent 在浏览器里"达成目标".

用法:
    agent = GoalAgent(controller, model="deepseek-chat")
    result = await agent.run("打开 example.com 找到 contact email")

工作循环:
  1. 拿 snapshot (现状观察)
  2. 喂给 LLM: snapshot + 历史 action + 目标
  3. LLM 返回下一个 action: {tool, ref, text, ...} 或 done
  4. 执行 action
  5. 重复直到 LLM 输出 done 或达到 max_steps

设计上:
- 一次只让 LLM 选一个 action (而不是整条计划) — 容错性高, 出错能恢复
- 失败时把错误回喂给 LLM 让它重试
- snapshot 过大时截断 (LLM context 有限)
- 暴露 step 历史给 LLM 让它能"看回来"
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from semantic_browser.browser.controller import BrowserController
from semantic_browser.llm.service import LLMService, LLMUnavailableError, Tier
from semantic_browser.llm.helpers import slice_refs_for_goal, build_smart_snapshot_excerpt
from semantic_browser.llm.diagnostics import collect_diagnostics, format_diagnostics_for_llm
from semantic_browser.snapshot.engine import PageSnapshot, SnapshotEngine

logger = logging.getLogger(__name__)


# LLM 可用的 action 集合. 与 SnapshotEngine 的 ref 系统协同.
_ALLOWED_ACTIONS = (
    "open", "click", "type", "extract_text", "done",
)


_SYSTEM_PROMPT = """You are an autonomous web agent. Given a goal, you observe the page and pick the next single action.

You must respond with valid JSON only:
{
  "thought": "short reasoning",
  "action": "<one of the allowed actions>",
  "args": { ... action-specific args ... }
}

Allowed actions:
- open(url): navigate to URL
- click(ref): click element by ref (e.g. "e5")
- type(ref, text): type text into element
- extract_text(max_chars=2000): read page content
- done(answer, summary): goal achieved; answer is the final result

Rules:
1. Always extract_text first if you need page content (you can ONLY see snapshot of refs, not text content)
2. Use refs from the latest snapshot — never guess refs
3. If an action failed 3+ times, change strategy (try a different ref, or give up with done)
4. Be concise. goal achieved → done immediately, don't keep clicking.
"""


@dataclass
class StepRecord:
    """每一步的历史, 给 LLM 回看."""
    step: int
    thought: str
    action: str
    args: dict[str, Any]
    success: bool
    error: Optional[str] = None
    snapshot_excerpt: str = ""


@dataclass
class GoalResult:
    """GoalAgent.run() 的最终返回."""
    goal: str
    success: bool
    answer: Optional[str] = None
    steps: list[StepRecord] = field(default_factory=list)
    total_steps: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success": self.success,
            "answer": self.answer,
            "total_steps": self.total_steps,
            "reason": self.reason,
            "steps": [
                {
                    "step": s.step,
                    "thought": s.thought,
                    "action": s.action,
                    "args": s.args,
                    "success": s.success,
                    "error": s.error,
                    "snapshot_excerpt": s.snapshot_excerpt,
                }
                for s in self.steps
            ],
        }


class GoalAgent:
    """LLM-driven agent: 给定自然语言目标, 在浏览器里自主达成.

    T26 增强: 用 cheap 模型 slice snapshot (省 token), 失败自动 dump diagnostics.
    """

    def __init__(
        self,
        controller: BrowserController,
        *,
        llm_service: LLMService | None = None,
        tier: Tier = "smart",  # GoalAgent 是复杂决策, 默认用强模型
        max_steps: int = 20,
        snapshot_ref_limit: int = 80,
        # T26: 可选 tier-2 智能 — 切片 + 失败 dump. 默认开启.
        use_smart_slicing: bool = True,
        slice_tier: Tier = "cheap",  # 切片用便宜模型
        slice_max_refs: int = 15,  # 只给 LLM 最多 15 个 ref
        use_failure_diagnostics: bool = True,  # 失败自动 dump
    ) -> None:
        self.controller = controller
        if llm_service is None:
            self.llm = LLMService()
        else:
            self.llm = llm_service
        self.tier = tier
        self.max_steps = max_steps
        self.snapshot_ref_limit = snapshot_ref_limit
        self.use_smart_slicing = use_smart_slicing
        self.slice_tier = slice_tier
        self.slice_max_refs = slice_max_refs
        self.use_failure_diagnostics = use_failure_diagnostics
        self.history: list[StepRecord] = []
        # T26: 失败诊断累积 (LLM 下一步看)
        self.last_failure_diag: Optional[str] = None

    def _is_available(self) -> bool:
        return self.llm.is_available()

    async def _capture_snapshot_excerpt(self, goal: str = "") -> tuple[str, str]:
        """拿当前 snapshot (URL + title + 主要 ref 列表); 超大时截断.

        T26: 如果 use_smart_slicing=True, 用 cheap 模型给 goal 挑 top-K refs.
        返回 (header, body) 两段; 调用者拼起来给 LLM 看.
        """
        page = self.controller.current_page
        if page is None:
            return "(no page open)", ""
        try:
            url = page.url
            title = await page.title()
            engine = SnapshotEngine(page)
            snap = await engine.capture(base_url=url)
        except Exception as e:
            return f"(snapshot error: {e})", ""

        # T26: smart slicing (用 cheap 模型按 goal 切片)
        if self.use_smart_slicing and goal:
            try:
                useful = await slice_refs_for_goal(
                    snap, goal,
                    max_refs=self.slice_max_refs,
                    llm=self.llm,
                    tier=self.slice_tier,
                )
                if useful:
                    body = build_smart_snapshot_excerpt(snap, useful)
                    header = f"URL: {url}\nTitle: {title}\n\n(sliced: {len(useful)}/{len(snap.links) + len(snap.controls)} refs relevant)"
                    return header, body
            except Exception as e:
                logger.warning("smart slicing failed, falling back: %s", e)

        # 兜底: 原 flat 列表 (无 goal 或 slicing 失败)
        all_refs = list(snap.links) + list(snap.controls)
        refs = []
        for c in all_refs[:self.snapshot_ref_limit]:
            refs.append(f"  - {c.ref} {c.kind}: {c.label or c.href or ''}")
        excerpt = "\n".join(refs) if refs else "(no interactive elements)"
        header = f"URL: {url}\nTitle: {title}\n\nInteractive refs ({len(refs)} shown):"
        return header, excerpt

    async def _ask_llm(self, goal: str, snapshot_excerpt: str) -> dict[str, Any]:
        """问 LLM 下一步做什么. 默认走 tier=smart (复杂多步决策)."""
        history_lines = []
        for step in self.history[-5:]:  # 只给最近 5 步
            history_lines.append(
                f"Step {step.step}: {step.action} {step.args} → "
                f"{'✓' if step.success else '✗ ' + (step.error or 'failed')}"
            )
        history_block = (
            "\n".join(history_lines) if history_lines else "(first step)"
        )

        # T26: 把上次失败诊断塞 prompt
        failure_block = ""
        if self.last_failure_diag:
            failure_block = f"\nLast failure diagnostics:\n{self.last_failure_diag}\n"

        user_prompt = f"""Goal: {goal}

Current page snapshot:
{snapshot_excerpt}
{history_block}{failure_block}
What's the next single action? Respond with JSON only."""

        return await self.llm.complete_json(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tier=self.tier,
            temperature=0.2,
            max_tokens=500,
        )

    async def _execute_action(self, action: str, args: dict[str, Any]) -> tuple[bool, str]:
        """执行 LLM 选的动作. Returns (success, error_or_output).

        click/type 默认用 self-healing 版本 (T22) — 失败自动 force / JS.
        """
        try:
            if action == "open":
                url = args.get("url", "")
                if not url:
                    return False, "missing url"
                await self.controller.open(url)
                return True, ""
            if action == "click":
                ref = args.get("ref", "")
                if not ref:
                    return False, "missing ref"
                # T22: 用 self-healing, 大幅减少 brittle agent 行为
                result = await self.controller.click_with_healing(ref)
                return result["ok"], result.get("error") or ""
            if action == "type":
                ref = args.get("ref", "")
                text = args.get("text", "")
                if not ref:
                    return False, "missing ref"
                # T22: 用 self-healing
                result = await self.controller.type_with_healing(ref, text)
                return result["ok"], result.get("error") or ""
            if action == "extract_text":
                # 触发一次 snapshot, 让 LLM 看 ref 列表. 文本需要单独 extract
                from semantic_browser.extractor.content import ContentExtractor
                page = self.controller.current_page
                if page is None:
                    return False, "no page"
                article = await ContentExtractor(page).extract_article()
                # 把文本存到 step 历史
                max_chars = int(args.get("max_chars", 2000))
                text = (article.to_markdown() or "")[:max_chars]
                return True, text
            if action == "done":
                return True, ""  # 由 caller 处理 done
            return False, f"unknown action: {action!r}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:200]

    async def run(self, goal: str, *, start_url: str | None = None) -> GoalResult:
        """主循环: 自主达成 goal."""
        if not self._is_available():
            return GoalResult(
                goal=goal, success=False,
                reason="LLM not configured (need LLM_API_KEY + LLM_BASE_URL or OPENAI_API_KEY)",
            )
        self.history = []
        self.last_failure_diag = None  # T26: 重置失败诊断

        # 可选: 自动 open start_url
        if start_url:
            await self.controller.open(start_url)

        consecutive_failures = 0
        for step_num in range(1, self.max_steps + 1):
            snapshot_header, snapshot_excerpt = await self._capture_snapshot_excerpt(goal=goal)
            try:
                decision = await self._ask_llm(goal, snapshot_header + "\n" + snapshot_excerpt)
            except Exception as e:
                return GoalResult(
                    goal=goal, success=False,
                    steps=self.history, total_steps=len(self.history),
                    reason=f"LLM call failed at step {step_num}: {type(e).__name__}: {e}"[:200],
                )

            action = decision.get("action", "")
            args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")

            if action not in _ALLOWED_ACTIONS:
                record = StepRecord(
                    step=step_num, thought=thought, action=action, args=args,
                    success=False, error=f"action not in {_ALLOWED_ACTIONS}",
                    snapshot_excerpt=snapshot_excerpt[:200],
                )
                self.history.append(record)
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    return GoalResult(
                        goal=goal, success=False,
                        steps=self.history, total_steps=len(self.history),
                        reason="3 consecutive invalid actions",
                    )
                continue

            # done: LLM 认为目标完成
            if action == "done":
                answer = args.get("answer") or args.get("summary") or ""
                record = StepRecord(
                    step=step_num, thought=thought, action=action, args=args,
                    success=True, snapshot_excerpt=snapshot_excerpt[:200],
                )
                self.history.append(record)
                return GoalResult(
                    goal=goal, success=True, answer=answer,
                    steps=self.history, total_steps=len(self.history),
                )

            # 其他 action: 执行
            ok, output = await self._execute_action(action, args)
            record = StepRecord(
                step=step_num, thought=thought, action=action, args=args,
                success=ok, error=None if ok else output,
                snapshot_excerpt=snapshot_excerpt[:200],
            )
            # extract_text 的输出文本也存 (LLM 下一步要看)
            if action == "extract_text" and ok:
                record.snapshot_excerpt = output[:300]
            self.history.append(record)

            if ok:
                consecutive_failures = 0
                self.last_failure_diag = None  # 成功后清空
            else:
                consecutive_failures += 1
                # T26: 失败时收集 diagnostics, 让 LLM 下一步看到底发生了什么
                if self.use_failure_diagnostics:
                    try:
                        diag = await collect_diagnostics(
                            self.controller,
                            failed_action=action,
                            failed_args=args,
                            error=output or "unknown error",
                        )
                        self.last_failure_diag = format_diagnostics_for_llm(diag)
                        logger.info(
                            "Action %s failed; diagnostics:\n%s",
                            action, self.last_failure_diag[:300],
                        )
                    except Exception as e:
                        logger.warning("collect_diagnostics failed: %s", e)
                if consecutive_failures >= 5:
                    return GoalResult(
                        goal=goal, success=False,
                        steps=self.history, total_steps=len(self.history),
                        reason="5 consecutive action failures",
                    )

        return GoalResult(
            goal=goal, success=False,
            steps=self.history, total_steps=len(self.history),
            reason=f"max_steps ({self.max_steps}) reached without done",
        )