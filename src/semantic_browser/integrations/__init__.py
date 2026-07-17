"""T77+T89: Integrations with popular agent frameworks.

这些是 optional integrations — 不装 framework 也能用 semantic_browser.

用法:
    # LangChain (需 `pip install langchain-core`)
    from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
    tool = SemanticQueryTool()

    # AutoGen (需 `pip install pyautogen`)
    from semantic_browser.integrations.autogen_adapter import semantic_query_fn
    # 注册给 AutoGen agent 当 tool

    # Aider
    from semantic_browser.integrations.aider_adapter import semantic_query_tool
    # 给 Aider 当 tool 函数用
"""
from __future__ import annotations

# 统一暴露 — 每个模块自己 lazy import (langchain/pyautogen 不一定装了)
from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
from semantic_browser.integrations.autogen_adapter import semantic_query_fn, has_autogen
from semantic_browser.integrations.aider_adapter import semantic_query_tool

__all__ = [
    "SemanticQueryTool",         # LangChain
    "semantic_query_fn",         # AutoGen
    "has_autogen",
    "semantic_query_tool",       # Aider
]
