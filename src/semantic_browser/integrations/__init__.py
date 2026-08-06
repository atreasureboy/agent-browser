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
from semantic_browser.integrations.autogen_adapter import (
    HAS_AUTOGEN,
    has_autogen,
    semantic_query_fn,
)
from semantic_browser.integrations.aider_adapter import semantic_query_tool

__all__ = [
    "SemanticQueryTool",         # LangChain
    "semantic_query_fn",         # AutoGen
    "has_autogen",
    "HAS_AUTOGEN",
    "semantic_query_tool",       # Aider
    "integration_catalog",
]


def _langchain_installed() -> bool:
    try:
        import langchain_core  # noqa: F401
        return True
    except ImportError:
        return False


def integration_catalog() -> list[dict]:
    """Machine-readable catalog of framework adapters (super_plan Round 2d).

    Each entry describes one integration: how to import it, what the tool
    call schema is, and whether the optional framework dependency is
    installed in this environment. Consumed by ``GET /v1/integrations``.
    """
    shared_params = [
        {"name": "query", "type": "string", "required": True,
         "description": "natural-language question"},
        {"name": "start_url", "type": "string", "required": False,
         "description": "known URL; omit to let M3 discover the site"},
        {"name": "budget", "type": "integer", "required": False,
         "description": "LLM token budget (default 2000)"},
        {"name": "max_pages", "type": "integer", "required": False,
         "description": "follow-link page cap (default 1)"},
    ]
    return [
        {
            "framework": "langchain",
            "installed": _langchain_installed(),
            "install_extra": "pip install langchain-core",
            "entry": "semantic_browser.integrations.langchain_adapter.SemanticQueryTool",
            "kind": "class (langchain_core.tools.BaseTool)",
            "description": "SemanticQuery as a LangChain tool (semantic_query).",
            "parameters": shared_params,
        },
        {
            "framework": "autogen",
            "installed": HAS_AUTOGEN,
            "install_extra": "pip install pyautogen",
            "entry": "semantic_browser.integrations.autogen_adapter.semantic_query_fn",
            "kind": "function (sync tool API)",
            "description": "SemanticQuery as an AutoGen callable for function_map/register_function.",
            "parameters": shared_params + [
                {"name": "cache_persist_path", "type": "string", "required": False,
                 "description": "persist query cache to this path across processes"},
            ],
        },
        {
            "framework": "aider",
            "installed": True,
            "install_extra": None,
            "entry": "semantic_browser.integrations.aider_adapter.semantic_query_tool",
            "kind": "function (signature-discovered tool)",
            "description": "SemanticQuery as an Aider tool function.",
            "parameters": shared_params,
        },
    ]
