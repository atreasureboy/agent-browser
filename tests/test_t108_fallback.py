"""T108 regression: archive.org wayback fallback — 主路径失败时自动兜底.

验证: 主 browse raise 时, _try_fallback_browse 应自动试 archive.org,
命中则返回 BrowseResult 并把 source 标到 snapshot.meta['__fallback_source'].

需要联网 (调用 archive.org). 没网就 skip.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _have_network() -> bool:
    """Skip if can't reach archive.org."""
    try:
        from urllib.request import urlopen, Request
        req = Request("https://archive.org/wayback/available?url=example.com/")
        req.add_header("User-Agent", "pytest")
        with urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _have_network(), reason="no network to archive.org"
)


def test_archive_returns_snapshot_for_known_site():
    """try_archive('books.toscrape.com/') 应返回 (wayback_url, html)."""
    from semantic_browser.fetch.fallback import try_archive

    def go():
        return try_archive("https://books.toscrape.com/")

    fb = asyncio.run(asyncio.to_thread(go))
    assert fb is not None, "books.toscrape.com 应在 wayback 里有 snapshot"
    wayback_url, html = fb
    assert "web.archive.org" in wayback_url
    assert "id_/" in wayback_url, "应使用 id_ modifier 去掉 wayback toolbar"
    assert len(html) > 1000, f"HTML 太小: {len(html)}"


def test_archive_returns_none_for_unknown_site():
    """try_archive 对完全陌生 URL 应返 None (不是抛)."""
    from semantic_browser.fetch.fallback import try_archive

    def go():
        return try_archive("https://this-domain-definitely-does-not-exist-xyz123abc.invalid/")

    fb = asyncio.run(asyncio.to_thread(go))
    assert fb is None


def test_browse_falls_back_to_wayback_on_primary_failure():
    """T108: 主 browse raise 时, 自动 archive.org fallback.

    用 mock 强制 primary raise, 验证 result + meta 标记正确.
    """
    from semantic_browser.engine import SemanticBrowser

    async def main():
        sb = SemanticBrowser()
        sb._started = True

        # 强制第一次 controller.open raise — 模拟 timeout / antibot
        real_open = sb.controller.open
        count = [0]

        async def mock_open(url, **kw):
            count[0] += 1
            if count[0] == 1:
                raise TimeoutError("simulated primary failure")
            return await real_open(url, **kw)

        sb.controller.open = mock_open

        # books.toscrape.com 在 wayback 有 snapshot
        result = await sb.browse(
            "https://books.toscrape.com/", allow_fallback=True
        )

        assert result is not None
        assert result.snapshot is not None
        # wayback source 应标记
        assert result.snapshot.meta.get("__fallback_source") == "archive.org wayback", (
            f"应标记 fallback source, got: {result.snapshot.meta}"
        )
        assert "web.archive.org" in result.snapshot.meta.get("__fallback_url", "")
        assert len(result.snapshot.text_blocks) > 0, (
            "wayback snapshot 应有 text_blocks"
        )
        await sb.close()

    asyncio.run(main())


def test_browse_no_fallback_when_disabled():
    """allow_fallback=False 时, 主路径 raise 应直接 re-raise."""
    from semantic_browser.engine import SemanticBrowser

    async def main():
        sb = SemanticBrowser()
        sb._started = True

        real_open = sb.controller.open

        async def always_fail(url, **kw):
            raise TimeoutError("forced primary fail")

        sb.controller.open = always_fail

        raised = False
        try:
            await sb.browse(
                "https://example.com/", allow_fallback=False
            )
        except TimeoutError:
            raised = True
        except Exception:
            raised = False
        assert raised, "allow_fallback=False 时应 re-raise 主路径 error"

        sb.controller.open = real_open
        await sb.close()

    asyncio.run(main())
