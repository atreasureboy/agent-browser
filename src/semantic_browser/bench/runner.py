"""
T35: Agent benchmark suite — golden tasks 量化评估.

JSON schema:
[
  {
    "name": "extract_h1_example",
    "goal": "extract the page h1 text",
    "start_url": "https://example.com",
    "expected": {
      "answer_contains": "Example Domain",  # answer 包含这个 substring 算过
      "max_steps": 5,                        # 步数上限 (soft limit)
    },
    "tags": ["smoke", "fast"],
  },
  ...
]

跑完输出 success rate + 平均步数 + 失败原因分布.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from semantic_browser.agent.loop import GoalAgent, GoalResult
from semantic_browser.browser.controller import BrowserController, BrowserConfig
from semantic_browser.llm.service import LLMService

logger = logging.getLogger(__name__)


@dataclass
class GoldenTask:
    name: str
    goal: str
    start_url: str = ""
    expected_answer_contains: str = ""
    expected_max_steps: int = 20
    tags: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    task: GoldenTask
    success: bool
    actual_answer: str = ""
    actual_steps: int = 0
    duration_sec: float = 0.0
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.task.name,
            "success": self.success,
            "answer": self.actual_answer,
            "steps": self.actual_steps,
            "duration_sec": self.duration_sec,
            "failure_reason": self.failure_reason,
            "tags": self.task.tags,
        }


@dataclass
class BenchmarkReport:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    avg_steps: float = 0.0
    avg_duration_sec: float = 0.0
    results: list[TaskResult] = field(default_factory=list)
    failure_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_rate": self.success_rate,
            "avg_steps": self.avg_steps,
            "avg_duration_sec": self.avg_duration_sec,
            "results": [r.to_dict() for r in self.results],
            "failure_reasons": self.failure_reasons,
        }


def load_tasks(path: str | Path) -> list[GoldenTask]:
    """从 JSON 加载 golden tasks."""
    p = Path(path)
    raw = json.loads(p.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"expected list, got {type(raw)}")
    out: list[GoldenTask] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"task #{i} not a dict")
        expected = item.get("expected", {})
        out.append(GoldenTask(
            name=item.get("name", f"task_{i}"),
            goal=item["goal"],
            start_url=item.get("start_url", ""),
            expected_answer_contains=expected.get("answer_contains", ""),
            expected_max_steps=expected.get("max_steps", 20),
            tags=item.get("tags", []),
        ))
    return out


def _grade(task: GoldenTask, result: GoalResult) -> tuple[bool, str]:
    """评估一个 task 是否通过."""
    if not result.success:
        return False, f"agent failed: {result.reason}"
    if not task.expected_answer_contains:
        # 没指定 expected → 跑完就算过
        return True, ""
    answer = result.answer or ""
    if task.expected_answer_contains.lower() not in answer.lower():
        return False, f"answer {answer!r} doesn't contain {task.expected_answer_contains!r}"
    if result.total_steps > task.expected_max_steps:
        return False, f"steps {result.total_steps} > max {task.expected_max_steps}"
    return True, ""


async def run_benchmark(
    tasks: list[GoldenTask],
    *,
    llm_service: LLMService | None = None,
    controller: BrowserController | None = None,
    tier: str = "smart",
    max_steps: int = 20,
    use_memory: bool = False,  # benchmark 时关掉 cache, 真实测能力
) -> BenchmarkReport:
    """跑一组 golden task, 返 BenchmarkReport.

    controller / llm_service 可复用 — benchmark 不会主动 close 它们.
    """
    if llm_service is None:
        llm_service = LLMService()
    owns_controller = controller is None
    if owns_controller:
        controller = BrowserController(BrowserConfig())

    report = BenchmarkReport(total=len(tasks))
    for task in tasks:
        agent = GoalAgent(
            controller,
            llm_service=llm_service,
            tier=tier,
            max_steps=max_steps,
            use_memory=use_memory,
        )
        start_t = time.time()
        try:
            result = await agent.run(task.goal, start_url=task.start_url or None)
        except Exception as e:
            result = GoalResult(
                goal=task.goal, success=False,
                reason=f"{type(e).__name__}: {e}"[:200],
            )
        duration = time.time() - start_t
        ok, reason = _grade(task, result)
        tr = TaskResult(
            task=task, success=ok,
            actual_answer=result.answer or "",
            actual_steps=result.total_steps,
            duration_sec=duration,
            failure_reason="" if ok else reason,
        )
        report.results.append(tr)
        if ok:
            report.succeeded += 1
        else:
            report.failed += 1
            report.failure_reasons[reason] = report.failure_reasons.get(reason, 0) + 1

    if report.results:
        report.avg_steps = sum(r.actual_steps for r in report.results) / len(report.results)
        report.avg_duration_sec = sum(r.duration_sec for r in report.results) / len(report.results)
    logger.info(
        "benchmark done: %d/%d (%.0f%%) in %.1fs",
        report.succeeded, report.total,
        report.success_rate * 100, report.avg_duration_sec * report.total,
    )
    return report