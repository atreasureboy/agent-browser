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
from semantic_browser.memory.goal_memory import GoalMemory
from semantic_browser.safety.guard import check_action
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


_PLAN_SYSTEM_PROMPT = """You are a planning agent. Given a goal, you observe the current page and return a SEQUENCE of planned actions (NOT executed).

You must respond with valid JSON only:
{
  "thought": "overall strategy",
  "plan": [
    {"step": 1, "action": "open", "args": {"url": "https://example.com"}, "why": "navigate to start"},
    {"step": 2, "action": "click", "args": {"ref": "e5"}, "why": "open contact section"},
    {"step": 3, "action": "extract_text", "args": {"max_chars": 2000}, "why": "read content"},
    {"step": 4, "action": "done", "args": {"answer": "<expected>"}, "why": "goal achieved"}
  ]
}

Allowed actions: open(url), click(ref), type(ref, text), extract_text(max_chars), done(answer)
Rules:
1. Plan 3-8 steps total. Don't over-engineer.
2. First step: either open(start_url) or extract_text() if page already has content.
3. If you can't determine the goal is achievable, end the plan with done(answer="unable to plan").
4. Use refs ONLY if you can guess them confidently; otherwise plan extract_text() first to discover refs.
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
        # T27: 跨 session goal memory. 默认开启.
        use_memory: bool = True,
        goal_memory: GoalMemory | None = None,
        # T31: 流式进度回调 — 每步完成时调用 (StepRecord). 可传 async callable.
        on_step: "Optional[callable]" = None,
        # T32: 危险动作守卫 — 默认开启 (需要人类 confirm)
        safety_guard: bool = True,
        allow_destructive: bool = False,
        # T34: ARIA 语义树注入 snapshot. 默认开 (LLM 拿到更丰富的语义信息).
        include_aria: bool = True,
        aria_max_chars: int = 2000,
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
        self.use_memory = use_memory
        self.goal_memory = goal_memory or (GoalMemory() if use_memory else None)
        self.on_step = on_step
        self.safety_guard = safety_guard
        self.allow_destructive = allow_destructive
        self.include_aria = include_aria
        self.aria_max_chars = aria_max_chars
        self.history: list[StepRecord] = []
        # T26: 失败诊断累积 (LLM 下一步看)
        self.last_failure_diag: Optional[str] = None
        # T27: 本次 run 是否命中 cache (调试用)
        self.last_memory_hit: Optional[dict[str, Any]] = None

    def _is_available(self) -> bool:
        return self.llm.is_available()

    async def _capture_snapshot_excerpt(self, goal: str = "") -> tuple[str, str]:
        """拿当前 snapshot (URL + title + 主要 ref 列表); 超大时截断.

        T26: 如果 use_smart_slicing=True, 用 cheap 模型给 goal 挑 top-K refs.
        T34: include_aria=True 时, 把 raw_aria 也喂给 LLM (语义树, role/name/state).
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

        # T34: ARIA 语义树注入 (header 后, body 前 — LLM 同时看到 ref 列表和语义)
        aria_block = ""
        if self.include_aria and snap.raw_aria:
            # 截断到 aria_max_chars 防止 prompt 爆炸
            aria_block = f"\nARIA semantic tree:\n{snap.raw_aria[:self.aria_max_chars]}\n"

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
                    body = build_smart_snapshot_excerpt(snap, useful) + aria_block
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
        excerpt += aria_block
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

    async def plan(self, goal: str, *, max_steps: int = 8) -> dict[str, Any]:
        """T29: 让 LLM 一次性给完整 plan (不执行). 返回 dict 含 thought/plan 列表.

        用于 dry-run 模式 — 用户先看 plan 再决定要不要执行.
        max_steps: 计划步数上限 (避免 LLM 写无限长 plan).
        """
        if not self._is_available():
            return {"thought": "", "plan": [],
                    "error": "LLM not configured"}
        snapshot_header, snapshot_excerpt = await self._capture_snapshot_excerpt(goal=goal)
        user_prompt = f"""Goal: {goal}

Current page snapshot:
{snapshot_header}
{snapshot_excerpt}

Return a JSON plan with at most {max_steps} steps."""
        try:
            result = await self.llm.complete_json(
                messages=[
                    {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                tier=self.tier,
                temperature=0.3,
                max_tokens=1500,
            )
        except Exception as e:
            return {"thought": "", "plan": [],
                    "error": f"{type(e).__name__}: {e}"[:200]}
        # 截断到 max_steps (LLM 可能超长)
        plan = result.get("plan", [])
        if isinstance(plan, list):
            plan = plan[:max_steps]
        return {
            "thought": result.get("thought", ""),
            "plan": plan,
            "goal": goal,
        }

    async def _emit_step(self, record: StepRecord) -> None:
        """T31: 调 on_step 回调 (每步完成). 异常不打断 agent."""
        if self.on_step is None:
            return
        try:
            cb = self.on_step(record)
            if hasattr(cb, "__await__"):
                await cb
        except Exception as e:
            logger.warning("on_step callback raised: %s", e)

    async def _execute_action(self, action: str, args: dict[str, Any]) -> tuple[bool, str]:
        """执行 LLM 选的动作. Returns (success, error_or_output).

        click/type 默认用 self-healing 版本 (T22) — 失败自动 force / JS.
        T32: 守卫在 _run_loop 里跑, 这里只做实际执行.
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
        self.last_memory_hit = None  # T27: 重置 cache 标记

        # T27: 跨 session memory — 先查 cache
        if self.use_memory and self.goal_memory is not None:
            cached = self.goal_memory.lookup(goal)
            if cached and cached.get("success") and cached.get("answer"):
                self.last_memory_hit = cached
                return GoalResult(
                    goal=goal, success=True,
                    answer=cached["answer"],
                    steps=[], total_steps=0,
                    reason=f"from memory (hit_count={cached.get('hit_count', 0)})",
                )

        # 可选: 自动 open start_url
        if start_url:
            await self.controller.open(start_url)

        result = await self._run_loop(goal)
        # T27: 记录到 memory (无论成败)
        if self.use_memory and self.goal_memory is not None:
            try:
                self.goal_memory.record(
                    goal=goal,
                    success=result.success,
                    answer=result.answer,
                    steps=result.total_steps,
                    reason=result.reason,
                )
            except Exception as e:
                logger.warning("failed to record goal to memory: %s", e)
        return result

    async def _run_loop(self, goal: str) -> GoalResult:
        """核心执行循环 — run() 包装了 memory lookup/record."""
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
                await self._emit_step(record)
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
                await self._emit_step(record)
                return GoalResult(
                    goal=goal, success=True, answer=answer,
                    steps=self.history, total_steps=len(self.history),
                )

            # 其他 action: 执行
            # T32: 守卫在执行前拦截危险动作 (单独运行, 不在 _execute_action 里)
            if self.safety_guard and not self.allow_destructive:
                check = check_action(action, args)
                if check.needs_confirm:
                    ok, output = False, f"BLOCKED by safety guard: {check.reason}"
                else:
                    ok, output = await self._execute_action(action, args)
            else:
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
            await self._emit_step(record)

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