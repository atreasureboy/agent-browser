"""T77: LangChain integration — SemanticQuery 作为 LangChain Tool.

用法:
    from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
    tool = SemanticQueryTool()
    # 直接调
    result = tool.run(query="Python 3.13 free-threading")
    # 或给 agent
    from langchain.agents import load_tools
    tools = [tool] + load_tools(["serpapi"], ...)

依赖: `pip install langchain-core` (>= 0.1)
"""
from __future__ import annotations

import json
from typing import Any, Optional

_LANGCHAIN_IMPORT_ERROR: str | None = None
try:
    from langchain_core.tools import BaseTool
    from langchain_core.callbacks import CallbackManagerForToolRun
    from pydantic import Field
except ImportError as e:
    BaseTool = object  # type: ignore
    CallbackManagerForToolRun = None  # type: ignore
    Field = None  # type: ignore
    _LANGCHAIN_IMPORT_ERROR = str(e)


def _require_langchain():
    if _LANGCHAIN_IMPORT_ERROR is not None:
        raise ImportError(
            "SemanticQueryTool requires langchain-core. "
            "Install with: pip install langchain-core"
        ) from Exception(_LANGCHAIN_IMPORT_ERROR)

from semantic_browser.query import SemanticQuery, SemanticAnswer


if _LANGCHAIN_IMPORT_ERROR is None:
    class SemanticQueryTool(BaseTool):
        """LangChain tool that lets the agent run SemanticQuery as a single tool call.

        输入 schema:
          query (str): 自然语言问题
          start_url (str, optional): 已知 URL 时传, 否则走 M3 自动发现
          budget (int, optional): LLM token 预算
          max_pages (int, optional): 多页 follow-link 上限

        输出: JSON 字符串含 answer / sources / tokens / confidence.
        """

        name: str = "semantic_query"
        description: str = (
            "用 Model-driven browser semantic layer 拿答案. "
            "agent 给个问题 + 可选 URL, 系统自动浏览+抽取+合成精炼 markdown 答案. "
            "比 read_url 烧 token 少 99%+. "
            "例: query='Python 3.13 free-threading executable name'"
        )

        # Pydantic field for extra params
        budget: int = Field(default=2000, description="LLM token 预算 (默认 2000)")
        max_pages: int = Field(default=1, description="多页 follow-link 上限 (默认 1)")
        cache_persist_path: Optional[str] = Field(default=None, description="持久化 cache 路径")

        # 共享 SemanticQuery 实例 (跨 tool call 复用 cache)
        _sq: Any = None  # 不让 pydantic 管它

        class Config:
            arbitrary_types_allowed = True

        def __init__(self, **kwargs):
            _require_langchain()  # lazy check
            super().__init__(**kwargs)
            self._sq = SemanticQuery(
                budget=self.budget,
                max_pages=self.max_pages,
                cache_persist_path=self.cache_persist_path,
            )

        def _run(
            self,
            query: str,
            start_url: Optional[str] = None,
            budget: Optional[int] = None,
            max_pages: Optional[int] = None,
            run_manager: Optional[CallbackManagerForToolRun] = None,
        ) -> str:
            # LangChain sync 接口 — T94 + T112 audit fix:
            # - 不在 async context: 用 asyncio.run() 直接执行
            # - 在 async context: 用 conuther 隔离 (T94 nest_asyncio 太重);
            #   T112 audit 指出 t.join(timeout=180) 会卡死 event-loop thread.
            #   修: 用 threading.Thread + concurrent.futures.Future 通信结果,
            #   query_fn thread 自己 polling, caller 阻塞由 daemon thread
            #   off-loaded — 仍然会卡 caller 但更标准的 idiom + 加 60s cap
            #   (从 180s 减到 60s — 真 async 用例不会跑那么久).
            import asyncio
            import concurrent.futures
            import threading  # T114 audit fix: T112 删了 import, threading.Thread 在 line 121 用, async context 会 NameError.
            try:
                # 尝试拿当前 loop — 在 async context 里会成功
                asyncio.get_running_loop()
                # 在 async context 里: off-load 到 daemon thread
                box: concurrent.futures.Future = concurrent.futures.Future()

                def _runner():
                    try:
                        new_loop = asyncio.new_event_loop()
                        try:
                            asyncio.set_event_loop(new_loop)
                            box.set_result(
                                new_loop.run_until_complete(
                                    self._arun(query, start_url, budget, max_pages, run_manager)
                                )
                            )
                        finally:
                            new_loop.close()
                    except Exception as e:
                        box.set_exception(e)

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                # T112 audit fix: box.result 给 caller-block 但用语义更标准的
                # Future (跟 t.join + result_box / error_box 那种 list-mutating
                # pattern 相比, exception propagation 更直接). timeout 改 60s.
                return box.result(timeout=60)
            except RuntimeError:
                # 不在 async context — 直接 asyncio.run
                return asyncio.run(
                    self._arun(query, start_url, budget, max_pages, run_manager)
                )
        async def _arun(
            self,
            query: str,
            start_url: Optional[str] = None,
            budget: Optional[int] = None,
            max_pages: Optional[int] = None,
            run_manager: Optional[CallbackManagerForToolRun] = None,
        ) -> str:
            result = await self._sq.run(
                query,
                start_url=start_url,
                budget=budget,
                max_pages=max_pages,
            )
            return self._format_result(result)

        def _format_result(self, result: SemanticAnswer) -> str:
            """格式化 SemanticAnswer 为 JSON 字符串给 LangChain."""
            return json.dumps({
                "answer": result.answer,
                "sources": list(result.sources),
                "confidence": result.confidence,
                "tokens_used": result.tokens_used.get("used", {}).get("total", 0),
                "cache_hit": result.tokens_used.get("cache_hit", False),
                "elapsed_s": result.elapsed_s(),
                "success": result.success,
            }, ensure_ascii=False, indent=2)

        async def aclose(self) -> None:
            if self._sq is not None:
                await self._sq.close()
else:
    SemanticQueryTool = None  # type: ignore


__all__ = ["SemanticQueryTool"]


__all__ = ["SemanticQueryTool"]
