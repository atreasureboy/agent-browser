"""T104 regression: Playwright state leak — 1 bad query 污染后续.

验证: Amazon 失败后, 下一个 site (InfoQ) 仍能正常取到 content.

跑: pytest tests/test_engine_browse_resilience.py -v
需要: ANTHROPIC_AUTH_TOKEN 或 OPENAI_API_KEY env
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# Skip if no LLM key
try:
    from semantic_browser.llm import LLMService
    if not LLMService().is_available():
        pytest.skip("no LLM key — LLM classifier path skipped")
except Exception:
    pytest.skip("LLM import failed")


def test_browse_failure_does_not_poison_next_query():
    """T104 regression: Amazon 失败后, InfoQ 应能正常抓取."""
    from semantic_browser.engine import SemanticBrowser

    async def main():
        sb = SemanticBrowser()
        try:
            await sb.start()
            # Amazon 抗 bot 挡
            amazon_failed = False
            try:
                await sb.browse("https://www.amazon.com/s?k=iphone+15")
            except Exception:
                amazon_failed = True
            assert amazon_failed, "Amazon 应失败 (anti-bot)"
            # T104 fix: 不应该污染下一 query
            r = await sb.browse("https://www.infoq.com/news/2024/01/python-3.13-released/")
            assert r is not None
            # 如果 page 处于半坏状态, sections 应是 0 (Readability 失败)
            # 修过后, 应能正常 extract
            assert len(r.snapshot.text_blocks) > 0, (
                f"page in bad state after Amazon failure: "
                f"text_blocks={len(r.snapshot.text_blocks)}"
            )
            assert r.article is not None
            assert len(r.article.sections) >= 1, (
                f"no sections extracted after Amazon failure: "
                f"sections={len(r.article.sections) if r.article else 0}"
            )
        finally:
            await sb.close()

    asyncio.run(main())


def test_browse_after_timeout_recovers():
    """T104 regression: 第一个 browse timeout, 第二个 OK (不卡 page 状态)."""
    from semantic_browser.engine import SemanticBrowser

    async def main():
        sb = SemanticBrowser()
        try:
            await sb.start()
            # 第一个 URL 用一个慢的或者不存在的 host
            try:
                await sb.browse("https://192.0.2.1:81/slow", extract_content=False)
            except Exception:
                pass
            # 第二个应 OK
            r = await sb.browse("https://www.infoq.com/news/2024/01/python-3.13-released/", extract_content=False)
            assert r is not None
            assert r.snapshot.url.startswith("https://www.infoq.com")
        finally:
            await sb.close()

    asyncio.run(main())
