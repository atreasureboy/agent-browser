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
    """T104 regression: 1 bad query 失败后, InfoQ 应能正常抓取.

    Amazon 不一定在 sandbox 网络下能被 anti-bot 挡 (有时直接返商品页),
    所以不强制断言 Amazon 失败 — 用任何会抛/失败的 URL 都可以.
    """
    from semantic_browser.engine import SemanticBrowser

    async def main():
        sb = SemanticBrowser()
        try:
            await sb.start()
            # 故意请求一个非常慢/失败的 URL — 触发 page state 问题
            try:
                await sb.browse(
                    "https://192.0.2.1:81/slow", extract_content=False
                )
            except Exception:
                pass
            # T104 fix: 失败后下一 query 仍正常
            r = await sb.browse(
                "https://www.infoq.com/news/2024/01/python-3.13-released/"
            )
            assert r is not None
            assert len(r.snapshot.text_blocks) > 0, (
                f"page in bad state after previous failure: "
                f"text_blocks={len(r.snapshot.text_blocks)}"
            )
            assert r.article is not None
            assert len(r.article.sections) >= 1, (
                f"no sections extracted after previous failure: "
                f"sections={len(r.article.sections) if r.article else 0}"
            )
        finally:
            await sb.close()

    asyncio.run(main())


def test_antibot_raises_on_blocked_page(monkeypatch):
    """T107: engine.browse 在 detect_antibot 命中时 raise RuntimeError.

    不起真 browser — 直接把 SnapshotEngine.capture mock 掉, 只验证
    page.content() → detect_antibot → raise 这条路径.
    """
    from semantic_browser.engine import SemanticBrowser
    from semantic_browser.snapshot.engine import PageSnapshot
    from unittest.mock import AsyncMock, MagicMock

    async def main():
        sb = SemanticBrowser()
        sb._started = True

        # mock controller.open 返回的 page
        mock_page = MagicMock()
        mock_page.url = "https://example.com/"
        mock_page.content = AsyncMock(return_value=(
            "<html><head><title>Just a moment</title></head>"
            "<body>Cloudflare Ray ID: 12345</body></html>"
        ))
        mock_page.goto = AsyncMock()
        mock_page.is_closed = MagicMock(return_value=False)

        from types import SimpleNamespace

        # mock SnapshotEngine.capture — 返回 SimpleNamespace 模拟 snapshot
        # (engine.browse 只在 antibot raise 后才用 snapshot, 所以内容无所谓)
        async def fake_capture(self, base_url=""):
            return SimpleNamespace(
                url=base_url, title="", page_type="unknown", domain="example.com",
                meta={}, text_blocks=[], links=[], controls=[],
                errors=[], to_json=lambda: "{}",
            )
        monkeypatch.setattr(
            "semantic_browser.snapshot.engine.SnapshotEngine.capture",
            fake_capture,
        )
        # classifier 不干活 — 让 snapshot 返回默认值就行
        async def fake_classify(self, snapshot):
            from types import SimpleNamespace as _NS
            return _NS(page_type="unknown", confidence=0.5, to_dict=lambda: {"page_type": "unknown"})
        monkeypatch.setattr(
            "semantic_browser.classifier.heuristic.PageClassifier.classify",
            fake_classify,
        )

        # mock controller
        sb.controller.open = AsyncMock(return_value=mock_page)
        sb.controller._page = mock_page

        # browse() — antibot 应 raise
        raised = False
        try:
            await sb.browse("https://example.com/")
        except RuntimeError as e:
            raised = "antibot" in str(e)
        except Exception:
            raised = False
        assert raised, "T107 antibot 应在 Cloudflare 拦时 raise RuntimeError"

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
