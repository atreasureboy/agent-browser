"""
Token Budget — 跟踪 + 强制上限 token 消耗.

本项目的 token 经济核心:
- 顶层 agent 给 max_tokens 预算
- SemanticQuery 累计每次 LLM 调用的 prompt + completion
- 达到上限立即抛 BudgetExceeded, 让上层停止继续浏览/抓取
- 透明披露给顶层 agent (tokens_used 字段)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(Exception):
    """token 预算超出. 调用方应停止继续 LLM 调用, 走 synthesize fallback."""

    def __init__(self, used: int, limit: int):
        self.used = used
        self.limit = limit
        super().__init__(f"token budget exceeded: {used} > {limit}")


@dataclass
class TokenUsage:
    """累计的 token 消耗."""
    prompt: int = 0
    completion: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def add(self, usage_dict: dict[str, Any]) -> None:
        """从 LLMResponse.usage dict 累计 (兼容 provider 间字段差异)."""
        # OpenAI 兼容格式 + Anthropic 格式都覆盖
        p = int(usage_dict.get("prompt_tokens", 0) or usage_dict.get("input_tokens", 0))
        c = int(usage_dict.get("completion_tokens", 0) or usage_dict.get("output_tokens", 0))
        # 兜底: 某些 provider 只给 total
        if not p and not c:
            total = int(usage_dict.get("total_tokens", 0))
            if total:
                c = total
        self.prompt += p
        self.completion += c

    def to_dict(self) -> dict[str, int]:
        return {"prompt": self.prompt, "completion": self.completion, "total": self.total}


class TokenBudget:
    """Hard cap on total tokens (prompt + completion).

    用法:
        budget = TokenBudget(max_total=2000)
        # 每次 LLM 调用后:
        budget.add(resp.usage)
        if budget.exhausted():
            ... break loop ...
    """

    def __init__(self, max_total: int = 2000):
        if max_total <= 0:
            raise ValueError(f"max_total must be positive, got {max_total}")
        self.max_total = max_total
        self.usage = TokenUsage()

    def add(self, usage_dict: dict[str, Any]) -> None:
        """累计一次调用的 token. 超上限抛 BudgetExceeded."""
        self.usage.add(usage_dict)
        if self.usage.total > self.max_total:
            raise BudgetExceeded(self.usage.total, self.max_total)

    def remaining(self) -> int:
        return max(0, self.max_total - self.usage.total)

    def exhausted(self) -> bool:
        return self.usage.total >= self.max_total

    def headroom_for_completion(self, expected_max_completion: int = 500) -> int:
        """预估还能不能完成一次 max_tokens 调用. 留 30% 余量."""
        # 假设 prompt 也消耗, 留 buffer; 用户可调
        return self.remaining() - expected_max_completion

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_total": self.max_total,
            "used": self.usage.to_dict(),
            "remaining": self.remaining(),
            "exhausted": self.exhausted(),
        }


def safe_add(budget: TokenBudget | None, usage: dict[str, Any]) -> int:
    """累计 usage 到 budget; budget=None 时跳过. 返回新增 token 数.

    用于不希望 raise BudgetExceeded 打断主流程的回调场景 — 返回 -1 表示超限.
    """
    if budget is None:
        return 0
    try:
        before = budget.usage.total
        budget.add(usage)
        return budget.usage.total - before
    except BudgetExceeded:
        return -1
