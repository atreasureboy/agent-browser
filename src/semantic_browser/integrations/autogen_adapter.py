"""T89: AutoGen integration — SemanticQuery 作为 AutoGen callable.

用法:
    from semantic_browser.integrations.autogen_adapter import semantic_query_fn

    # 注册给 AutoGen agent
    from autogen import ConversableAgent, register_function
    agent = ConversableAgent("researcher", llm_config=..., function_map={"semantic_query": semantic_query_fn})
    register_function(
        semantic_query_fn,
        caller=agent,
        executor=agent,
        name="semantic_query",
        description="Query the web via M3 model, return markdown answer",
    )

依赖: `pip install pyautogen` (>= 0.2)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

_AUTOGEN_IMPORT_ERROR: str | None = None
try:
    # AutoGen 0.4+ 包名是 pyautogen; 0.7+ 是 autogen_agentchat (autogen 是 wrapper)
    import pyautogen as autogen  # noqa: F401
    HAS_AUTOGEN = True
except ImportError:
    # 0.7+ 的新名字
    try:
        import autogen_agentchat as autogen  # noqa: F401
        HAS_AUTOGEN = True
    except ImportError:
        HAS_AUTOGEN = False
    HAS_AUTOGEN = False


def semantic_query_fn(
    query: str,
    start_url: Optional[str] = None,
    budget: int = 2000,
    max_pages: int = 1,
    cache_persist_path: Optional[str] = None,
    cache_ttl_s: float = 600.0,
) -> str:
    """Sync wrapper for AutoGen (AutoGen 默认是 sync tool API).

    T96 修: 加 cache_persist_path / cache_ttl_s 参数 (跟 LangChain 一致,
    让 AutoGen 用户也能跨进程共享 cache).

    Returns:
        JSON 字符串 含 answer / sources / confidence / tokens_used / cache_hit / elapsed_s.

    Example:
        >>> semantic_query_fn("Python 3.13 free-threading",
        ...                    start_url="https://docs.python.org/3/whatsnew/3.13.html",
        ...                    budget=1500,
        ...                    cache_persist_path="/tmp/agent_cache.json")
    """
    if not HAS_AUTOGEN:
        return json.dumps({"_warning": "pyautogen not installed; install to enable AutoGen integration",
                           "answer": "autogen integration requires pyautogen package"}, ensure_ascii=False)
    from semantic_browser.query import run_query

    # T98: 跟 LangChain adapter 一样 — 独立线程跑新 loop, 避免 'event loop already running'
    import threading
    result_box: list = []
    error_box: list = []

    def runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result_box.append(
                loop.run_until_complete(
                    run_query(
                        query=query,
                        start_url=start_url,
                        budget=budget,
                        max_pages=max_pages,
                        cache_persist_path=cache_persist_path,
                        cache_ttl_s=cache_ttl_s,
                    )
                )
            )
        except Exception as e:
            error_box.append(e)
        finally:
            loop.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=180)
    if error_box:
        raise error_box[0]
    if not result_box:
        raise RuntimeError("AutoGen semantic_query_fn timed out (>180s)")
    result = result_box[0]
    return json.dumps({
        "answer": result.answer,
        "sources": list(result.sources),
        "confidence": result.confidence,
        "tokens_used": result.tokens_used.get("used", {}).get("total", 0),
        "cache_hit": result.tokens_used.get("cache_hit", False),
        "elapsed_s": result.elapsed_s(),
        "success": result.success,
    }, ensure_ascii=False, indent=2)


def has_autogen() -> bool:
    """检查 autogen 是否装了 (用于运行时降级)."""
    return HAS_AUTOGEN


__all__ = ["semantic_query_fn", "has_autogen"]
