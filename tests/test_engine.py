"""
BrowseResult.to_dict(full) 测试 — 验证摘要 vs 完整序列化。
"""
from semantic_browser.engine import BrowseResult
from semantic_browser.snapshot.engine import PageSnapshot, TextBlock, LinkInfo, ControlInfo
from semantic_browser.classifier.heuristic import ClassificationResult
from semantic_browser.extractor.content import ArticleContent, InterfaceSummary


def _make_result():
    snap = PageSnapshot(
        url="https://x.com/", title="T", domain="x.com",
        text_blocks=[
            TextBlock(tag="h1", text="Hello"),
            TextBlock(tag="p", text="World"),
            TextBlock(tag="p", text="multi\nline\ncontent"),  # 包含换行
        ],
        links=[LinkInfo(ref="e1", text="L", href="https://x.com/a")],
        controls=[ControlInfo(ref="e2", kind="button", label="Go")],
    )
    article = ArticleContent(
        title="T", word_count=100, text_length=450,
        sections=[{"heading": "S1", "level": 2, "paragraphs": ["p"], "code_blocks": [], "tables": [], "images": []}],
    )
    return BrowseResult(
        url="https://x.com/", snapshot=snap,
        classification=ClassificationResult(page_type="article", confidence=0.9, reason="r", signals=["a"]),
        article=article, interfaces=InterfaceSummary(), elapsed=1.0,
    )


class TestBrowseResultToDict:
    def test_summary_view_excludes_arrays(self):
        r = _make_result()
        d = r.to_dict()
        # 摘要视图: snapshot.text_blocks 不应出现
        assert "text_blocks" not in d["snapshot"]
        assert "links" not in d["snapshot"]
        # article: sections 也不应有
        assert "sections" not in d["article"]
        # 但 summary 字段必须有
        assert "summary" in d["article"]
        assert d["article"]["word_count"] == 100
        assert d["article"]["text_length"] == 450

    def test_full_view_includes_arrays(self):
        r = _make_result()
        d = r.to_dict(full=True)
        assert len(d["snapshot"]["text_blocks"]) == 3
        assert len(d["snapshot"]["links"]) == 1
        assert len(d["snapshot"]["controls"]) == 1
        assert len(d["article"]["sections"]) == 1
        # text_blocks 里有换行符, 但 dict 内部没问题 (json.dumps 会 escape)

    def test_full_view_newlines_in_text_preserved(self):
        r = _make_result()
        d = r.to_dict(full=True)
        ml = next(b for b in d["snapshot"]["text_blocks"] if "multi" in b["text"])
        assert "\n" in ml["text"]  # dict 内部保留; json.dumps 负责转义

    def test_summary_truncated_at_max(self):
        # 100 段够撑爆 100 字符限制
        article = ArticleContent(
            title="X",
            sections=[{"heading": f"S{i}", "level": 2, "paragraphs": [f"paragraph {i} " * 20],
                       "code_blocks": [], "tables": [], "images": []} for i in range(20)],
        )
        snap = PageSnapshot(url="u", title="T", domain="u")
        r = BrowseResult(url="u", snapshot=snap,
                         classification=ClassificationResult(page_type="article", confidence=0.5, reason="", signals=[]),
                         article=article, interfaces=None, elapsed=0)
        d = r.to_dict()
        assert len(d["article"]["summary"]) <= 1600  # 1500 + 余量
        if len(d["article"]["summary"]) >= 1500:
            assert d["article"]["summary"].endswith("…")

    def test_no_article(self):
        snap = PageSnapshot(url="u", title="T", domain="u")
        r = BrowseResult(url="u", snapshot=snap,
                         classification=ClassificationResult(page_type="article", confidence=0.5, reason="", signals=[]),
                         article=None, interfaces=None, elapsed=0)
        d = r.to_dict()
        assert "article" not in d


# ── B24: engine.find / extract_topic 空 keyword 抛错 ──────────

import pytest


class TestEngineFindRaises:
    """空 keyword 必须抛 ValueError (而不是返回 data.error 字符串)。"""

    async def test_find_empty_keyword_raises(self, monkeypatch):
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser.__new__(SemanticBrowser)  # 跳过 __init__
        with pytest.raises(ValueError, match="keyword is empty"):
            await sb.find("https://x.com", "")

    async def test_find_whitespace_keyword_raises(self):
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser.__new__(SemanticBrowser)
        with pytest.raises(ValueError):
            await sb.find("https://x.com", "   ")

    async def test_extract_topic_empty_keyword_raises(self):
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser.__new__(SemanticBrowser)
        with pytest.raises(ValueError):
            await sb.extract_topic("https://x.com", "")


# ── 启发式信号收紧验证 ──────────────────────────────────────

from semantic_browser.classifier.heuristic import PageClassifier


class TestHeuristicSignalTightening:
    def _make_snap(self, **kw):
        defaults = dict(url="https://x.com", title="T", domain="x.com")
        defaults.update(kw)
        return PageSnapshot(**defaults)

    def test_error_keyword_in_code_block_does_not_fire(self):
        """"Error" 出现在 <pre> code block 不应触发 error_content。"""
        snap = self._make_snap(
            url="https://docs.example.com/api",
            title="API Reference",
            text_blocks=[
                TextBlock(tag="h1", text="API Reference"),
                TextBlock(tag="p", text="This page documents the API."),
                TextBlock(tag="p", text="When you call this function:"),
                TextBlock(tag="pre", text="Traceback (most recent call last):\n  File 'x.py', line 1\n    raise Error"),
                TextBlock(tag="p", text="See the docs for more."),
            ],
        )
        cls = PageClassifier()
        result = cls.classify(snap)
        # 不应该判 error
        assert result.page_type != "error", f"误判 error: {result.signals}"
        assert "error_content" not in result.signals

    def test_dashboard_keyword_in_footer_does_not_fire(self):
        """"dashboard" 出现在前 5 个块之外不应触发。"""
        snap = self._make_snap(
            url="https://docs.example.com/guide",
            title="Getting Started Guide",
            text_blocks=[
                TextBlock(tag="h1", text="Getting Started"),
                TextBlock(tag="p", text="Welcome to our guide."),
                TextBlock(tag="p", text="Step 1: Install"),
                TextBlock(tag="p", text="Step 2: Configure"),
                TextBlock(tag="p", text="Step 3: Run"),
                # 第 6 个块之后才出现 dashboard 关键词 — 不应触发
                TextBlock(tag="p", text="After install, visit the dashboard at example.com/dashboard."),
            ],
        )
        cls = PageClassifier()
        result = cls.classify(snap)
        assert "dashboard_content" not in result.signals

    def test_actual_404_page_still_detected(self):
        """真正的 404 页仍应被识别。"""
        snap = self._make_snap(
            url="https://example.com/404",
            title="404 Not Found",
            text_blocks=[
                TextBlock(tag="h1", text="Page not found"),
                TextBlock(tag="p", text="The page you requested does not exist."),
            ],
        )
        cls = PageClassifier()
        result = cls.classify(snap)
        assert result.page_type == "error"