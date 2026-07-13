"""T67: tests/test_token_budget.py — TokenBudget 单测."""
from __future__ import annotations

import pytest

from semantic_browser.query import (
    TokenBudget, TokenUsage, BudgetExceeded, safe_add,
)


class TestTokenUsage:
    def test_initial_zero(self):
        u = TokenUsage()
        assert u.prompt == 0
        assert u.completion == 0
        assert u.total == 0

    def test_add_openai_format(self):
        u = TokenUsage()
        u.add({"prompt_tokens": 10, "completion_tokens": 5})
        assert u.prompt == 10 and u.completion == 5
        assert u.total == 15

    def test_add_anthropic_format(self):
        u = TokenUsage()
        u.add({"input_tokens": 20, "output_tokens": 15})
        assert u.prompt == 20 and u.completion == 15
        assert u.total == 35

    def test_add_mixed_compatible(self):
        # input_tokens + completion_tokens 混用 — 添加但分别累计
        u = TokenUsage()
        u.add({"prompt_tokens": 5, "output_tokens": 7})
        assert u.prompt == 5 and u.completion == 7

    def test_add_total_only(self):
        # 兜底: 只给 total
        u = TokenUsage()
        u.add({"total_tokens": 12})
        # 没有 prompt/completion 时, total 计入 completion
        assert u.prompt == 0 and u.completion == 12

    def test_to_dict(self):
        u = TokenUsage()
        u.add({"prompt_tokens": 3, "completion_tokens": 4})
        assert u.to_dict() == {"prompt": 3, "completion": 4, "total": 7}


class TestTokenBudget:
    def test_initial_state(self):
        b = TokenBudget(max_total=100)
        assert b.usage.total == 0
        assert b.remaining() == 100
        assert not b.exhausted()

    def test_add_within_budget(self):
        b = TokenBudget(max_total=100)
        b.add({"prompt_tokens": 30, "completion_tokens": 20})
        assert b.usage.total == 50
        assert b.remaining() == 50
        assert not b.exhausted()

    def test_add_overflow_raises(self):
        b = TokenBudget(max_total=100)
        b.add({"prompt_tokens": 50, "completion_tokens": 30})
        assert b.usage.total == 80
        # 这次加完会超
        with pytest.raises(BudgetExceeded) as exc_info:
            b.add({"prompt_tokens": 30, "completion_tokens": 0})
        assert exc_info.value.used == 110
        assert exc_info.value.limit == 100

    def test_exactly_at_limit(self):
        b = TokenBudget(max_total=100)
        b.add({"prompt_tokens": 60, "completion_tokens": 40})
        assert b.usage.total == 100
        assert b.exhausted()
        assert b.remaining() == 0

    def test_zero_or_negative_max_raises(self):
        with pytest.raises(ValueError):
            TokenBudget(max_total=0)
        with pytest.raises(ValueError):
            TokenBudget(max_total=-10)

    def test_to_dict_full(self):
        b = TokenBudget(max_total=200)
        b.add({"prompt_tokens": 30, "completion_tokens": 20})
        d = b.to_dict()
        assert d == {
            "max_total": 200,
            "used": {"prompt": 30, "completion": 20, "total": 50},
            "remaining": 150,
            "exhausted": False,
        }


class TestSafeAdd:
    def test_safe_add_normal(self):
        b = TokenBudget(max_total=200)
        added = safe_add(b, {"prompt_tokens": 30, "completion_tokens": 20})
        assert added == 50
        assert b.usage.total == 50

    def test_safe_add_no_budget(self):
        added = safe_add(None, {"prompt_tokens": 30})
        assert added == 0  # None 跳过

    def test_safe_add_overflow_returns_minus_one(self):
        b = TokenBudget(max_total=100)
        b.add({"prompt_tokens": 80, "completion_tokens": 10})
        # 已经 90, 再加 20 超
        added = safe_add(b, {"prompt_tokens": 20})
        assert added == -1  # 不会 raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
