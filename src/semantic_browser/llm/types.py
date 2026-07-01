"""
LLM 类型 — T36 解循环 import.

service.py 和 providers/*.py 都引用 LLMResponse / LLMUnavailableError.
providers/__init__.py 不能再 import service.py (会循环). 把类型放到 types.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LLMResponse:
    """统一的 LLM 响应."""
    content: str
    model: str
    tier: str
    usage: dict[str, int]  # prompt_tokens / completion_tokens / total_tokens
    raw: dict[str, Any]


class LLMUnavailableError(RuntimeError):
    """LLM 未配置或不可用."""
