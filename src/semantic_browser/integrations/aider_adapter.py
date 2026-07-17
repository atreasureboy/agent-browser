"""T89: Aider integration — SemanticQuery 作为 Aider 工具.

Aider 通过其 function calling API 注册工具; 我们提供一个简单的 tool function.

用法:
    # 在 Aider 的 tool registry 里注册:
    from semantic_browser.integrations.aider_adapter import semantic_query_tool

    # 然后给 Aider 当 tool 用 (Aider 会自动读 function signature)
    # 参考: https://aider.chat/docs/tools.html
    # 示例:
    #   aider --model sonnet --edit-format whole --tools semantic_query_tool

依赖: 无 (Aider 是基于 Python 的; 但 Aider 本身需单独装)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional


def semantic_query_tool(
    query: str,
    start_url: Optional[str] = None,
    budget: int = 2000,
    max_pages: int = 1,
) -> dict[str, Any]:
    """Aider 工具 function.

    Aider 通过 inspect function signature + docstring 发现工具,
    所以这个函数签名决定了 Aider 调用时的参数 schema.

    Returns:
        dict 含 answer / sources / confidence / tokens_used / cache_hit / elapsed_s
    """
    from semantic_browser.query import run_query

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    result = loop.run_until_complete(
        run_query(
            query=query,
            start_url=start_url,
            budget=budget,
            max_pages=max_pages,
        )
    )
    return {
        "answer": result.answer,
        "sources": list(result.sources),
        "confidence": result.confidence,
        "tokens_used": result.tokens_used.get("used", {}).get("total", 0),
        "cache_hit": result.tokens_used.get("cache_hit", False),
        "elapsed_s": result.elapsed_s(),
        "success": result.success,
    }


__all__ = ["semantic_query_tool"]
