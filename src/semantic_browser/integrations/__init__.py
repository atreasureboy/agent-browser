"""T77: Integrations with popular agent frameworks.

这些是 optional integrations — 不装 framework 也能用 semantic_browser.

用法:
    # LangChain (需 `pip install langchain-core`)
    from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
    tool = SemanticQueryTool()
    # 然后 agent 可调 tool.run(query=...)

    # AutoGen (需 `pip install pyautogen`)
    # from semantic_browser.integrations.autogen_adapter import SemanticQueryFunction
"""
from __future__ import annotations

__all__ = []
