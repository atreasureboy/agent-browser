"""D — 真 LLM bench.

LLMService tier routing + GoalAgent.plan dry-run + 端到端 page 摘要.
需要 OPENAI_API_KEY / OPENAI_BASE_URL (DeepSeek 兼容).

预算控制: 默认跳过除非 OPENAI_API_KEY 设置; 调用次数 < 20 次 (用 deepseek-chat
足够便宜的 $1.1 余额跑完). 探测一次确认 endpoint 真活着, 然后再 bench.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest


def _llm_env_ok() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


# 单次探测缓存 — 跟 test_llm_e2e.py 同样的策略
_probe_cache: dict[str, bool] = {}


async def _probe_api_live_async() -> bool:
    if "result" in _probe_cache:
        return _probe_cache["result"]
    if not _llm_env_ok():
        _probe_cache["result"] = False
        return False
    import httpx
    base = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or "https://api.openai.com/v1"
    )
    key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "deepseek-chat")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1, "temperature": 0},
            )
            live = r.status_code == 200
    except Exception:
        live = False
    _probe_cache["result"] = live
    return live


@pytest.fixture(scope="module")
def llm_service():
    """module-scope LLMService — 跨测试共享, 节省探测开销."""
    if not _llm_env_ok():
        pytest.skip("OPENAI_API_KEY not set; skip bench")
    if not asyncio.run(_probe_api_live_async()):
        pytest.skip("LLM endpoint unreachable or auth failed; skip bench")
    from semantic_browser.llm.service import LLMService
    return LLMService(provider="openai")  # openai-compat, 自动用 deepseek-chat


# ── tier routing ──────────────────────────────────────────


@pytest.mark.asyncio
class TestTierRouting:
    """LLMService 应能按 cheap/medium/smart 三档路由到不同 model.

    DeepSeek 单 model provider: 三档都路由到 deepseek-chat (默认配置).
    验证: model_for 真的返回配置的 model, call_counts 真的累加.
    """

    async def test_model_for_each_tier(self, llm_service):
        cheap = llm_service.model_for("cheap")
        medium = llm_service.model_for("medium")
        smart = llm_service.model_for("smart")
        # 至少应返回非空字符串
        assert cheap and medium and smart
        # provider="openai" (deepseek-compat) 默认全用 deepseek-chat
        # 如果用户配了不同 model 也 OK — 只要非空
        assert isinstance(cheap, str) and isinstance(medium, str) and isinstance(smart, str)

    async def test_call_counts_increment(self, llm_service):
        before = dict(llm_service.call_counts)
        # cheap
        await llm_service.complete(
            messages=[{"role": "user", "content": "Say 'ok'"}],
            tier="cheap", max_tokens=5, temperature=0,
        )
        # medium
        await llm_service.complete(
            messages=[{"role": "user", "content": "Say 'ok'"}],
            tier="medium", max_tokens=5, temperature=0,
        )
        # smart
        await llm_service.complete(
            messages=[{"role": "user", "content": "Say 'ok'"}],
            tier="smart", max_tokens=5, temperature=0,
        )
        after = llm_service.call_counts
        assert after["cheap"] == before["cheap"] + 1
        assert after["medium"] == before["medium"] + 1
        assert after["smart"] == before["smart"] + 1

    async def test_response_has_content(self, llm_service):
        resp = await llm_service.complete(
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            tier="cheap", max_tokens=5, temperature=0,
        )
        # LLMResponse 至少要有 content
        assert resp.content
        assert isinstance(resp.content, str)


# ── 端到端 page 摘要 ──────────────────────────────────────


@pytest.mark.asyncio
class TestPageSummaryBench:
    """bench: 给一段 page 内容, LLM 出 1-2 句摘要.

    验证 LLM 路径真能用 + 摘要质量合理 (含原页关键词).
    """

    PAGE_SAMPLE = """
    <h1>Releasing Semantic Browser v0.1.0</h1>
    <p>We're excited to announce the first public release of the
    Semantic Browser, a Playwright-based semantic layer designed for
    AI agents. The release includes snapshotting, classification, and
    extraction features.</p>
    <p>Try it out and let us know what you think about agent-driven
    web browsing!</p>
    """

    async def test_summarize_contains_keyword(self, llm_service):
        prompt = (
            "Summarize the following page in 1-2 sentences. "
            "Reply with ONLY the summary, no preamble.\n\n"
            f"PAGE:\n{self.PAGE_SAMPLE}"
        )
        resp = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            tier="cheap", max_tokens=120, temperature=0,
        )
        summary = resp.content.lower()
        # 摘要至少包含一个关键词 (semantic browser / agent / snapshot)
        keywords = ("semantic", "browser", "agent", "playwright")
        assert any(k in summary for k in keywords), (
            f"摘要缺关键词: {resp.content!r}"
        )
        # 摘要不应过长 (< 500 字符)
        assert len(resp.content) < 500, f"摘要过长: {resp.content!r}"

    async def test_extract_question(self, llm_service):
        """bench: 问 LLM "页面讲什么主题?" — 验证能正确理解问题."""
        prompt = (
            "What is the main topic of the following page?\n"
            "Reply in one short sentence.\n\n"
            f"PAGE:\n{self.PAGE_SAMPLE}"
        )
        resp = await llm_service.complete(
            messages=[{"role": "user", "content": prompt}],
            tier="cheap", max_tokens=80, temperature=0,
        )
        answer = resp.content.lower()
        # 答案应提到 release / announcing / browser / version
        assert any(k in answer for k in (
            "release", "browser", "semantic", "announc", "version",
        )), f"答案不合预期: {resp.content!r}"


# ── GoalAgent.plan dry-run ────────────────────────────────


@pytest.mark.asyncio
class TestGoalAgentPlanDryRun:
    """GoalAgent.plan() 不执行, 只让 LLM 出 step plan.

    验证: plan 返回 dict 含 steps 列表; 步骤里的 action 在 _ALLOWED_ACTIONS 中.
    不需要 Playwright (plan 只调 LLM, 不动 controller).
    """

    async def test_plan_returns_steps(self, llm_service):
        """GoalAgent.plan 用我们测试 fixture 提供的 LLMService.

        controller 是最小 fake (current_page=None), plan 走 LLM 推理路径,
        不实际开浏览器. use_smart_slicing 关掉 — 否则会用 cheap LLM 再切
        snapshot (双倍 token).
        """
        from semantic_browser.agent.loop import GoalAgent, _ALLOWED_ACTIONS

        class _FakeController:
            current_page = None  # 没 page, 跳过 snapshot capture

        agent = GoalAgent(
            controller=_FakeController(),
            llm_service=llm_service,
            tier="smart",
            max_steps=3,
            use_smart_slicing=False,  # 关掉省 token
        )
        plan = await agent.plan(
            goal="Open https://example.com and report the page title",
            max_steps=3,
        )
        # plan 必有 thought / plan 字段 (或 error)
        assert isinstance(plan, dict)
        assert "thought" in plan and "plan" in plan, (
            f"plan 缺关键字段: {list(plan.keys())}"
        )
        # 若 plan 非空, 验证每步的 action 在 _ALLOWED_ACTIONS 中
        for step in plan["plan"]:
            if isinstance(step, dict) and "action" in step:
                assert step["action"] in _ALLOWED_ACTIONS, (
                    f"plan step action 不在白名单: {step}"
                )

    async def test_safety_check_classifies_dangerous_action(self, llm_service):
        """safety_check 不调 LLM, 但要验证模块能加载且危险 action 被拦.

        guard 对 type 看 args.text; 对 click 看 ref_label (button label).
        这里测 type + 危险 text, 应该 needs_confirm=True.
        """
        from semantic_browser.safety.guard import check_action
        check = check_action("type", {"ref": "e1", "text": "Delete my account"})
        assert check.needs_confirm, f"type 含 'delete' 应被拦: {check}"
        assert check.risk_level == "dangerous"

    async def test_memory_lookup_miss_then_hit(self, llm_service, tmp_path):
        """GoalMemory: 第一次 miss, 记录后第二次 hit.

        DEFAULT_PATH 在模块加载时已解析为真实 HOME, 改 HOME 太晚.
        显式传 path 参数到 GoalMemory(path=) 走隔离路径.
        """
        from semantic_browser.memory.goal_memory import GoalMemory
        mem_path = tmp_path / "goal_memory.json"
        mem = GoalMemory(path=mem_path)
        goal = "Bench test goal for memory hit/miss cycle"
        # miss
        miss = mem.lookup(goal)
        assert miss is None, f"首次 lookup 应 miss, got {miss}"
        # 记录
        mem.record(goal=goal, success=True, answer="bench answer",
                   steps=2, reason="ok")
        # hit
        hit = mem.lookup(goal)
        assert hit is not None, "记录后 lookup 应 hit"
        assert hit.get("answer") == "bench answer"