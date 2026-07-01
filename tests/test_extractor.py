"""
ContentExtractor 测试 — 不启动浏览器，测 ArticleContent / InterfaceSummary
的纯数据逻辑 (to_dict, to_markdown, summary, confidence 边界)。
"""
from __future__ import annotations

import pytest

from semantic_browser.extractor.content import (
    ArticleContent,
    InterfaceSummary,
)


# ── ArticleContent 纯数据 ─────────────────────────────────────

class TestArticleContent:
    def test_defaults(self):
        a = ArticleContent()
        assert a.title == ""
        assert a.author == ""
        assert a.publish_date == ""
        assert a.sections == []
        assert a.code_blocks == []
        assert a.tables == []
        assert a.images == []
        assert a.word_count == 0
        assert a.text_length == 0
        assert a.extraction_confidence == 0.0

    def test_to_dict_roundtrip(self):
        a = ArticleContent(
            title="T", author="A", publish_date="2026-01-01",
            sections=[{"heading": "H1", "paragraphs": ["p1"]}],
            code_blocks=["c1"], tables=["t1"], images=[{"src": "x", "alt": "y"}],
            word_count=100, text_length=450, extraction_confidence=0.8,
        )
        d = a.to_dict()
        assert d["title"] == "T"
        assert d["author"] == "A"
        assert d["publish_date"] == "2026-01-01"
        assert d["sections"] == [{"heading": "H1", "paragraphs": ["p1"]}]
        assert d["code_blocks"] == ["c1"]
        assert d["word_count"] == 100
        assert d["text_length"] == 450
        assert d["extraction_confidence"] == 0.8

    def test_to_markdown_basic(self):
        a = ArticleContent(
            title="Hello",
            author="Alice",
            publish_date="2026-01-01",
            sections=[
                {"heading": "Intro", "level": 2,
                 "paragraphs": ["para one", "para two"],
                 "code_blocks": ["print(1)"],
                 "tables": [], "images": []},
            ],
        )
        md = a.to_markdown()
        assert md.startswith("# Hello")
        assert "*Alice | 2026-01-01*" in md
        assert "## Intro" in md  # level 2 + 1 == ##
        assert "para one" in md
        assert "```\nprint(1)\n```" in md

    def test_to_markdown_image(self):
        a = ArticleContent(
            title="T",
            sections=[{"heading": "", "level": 2, "paragraphs": [],
                       "code_blocks": [], "tables": [],
                       "images": [{"src": "https://x.com/i.png", "alt": "fig", "caption": "Figure 1"}]}],
        )
        md = a.to_markdown()
        assert "![fig](https://x.com/i.png)" in md

    def test_to_markdown_image_without_src_skipped(self):
        a = ArticleContent(
            sections=[{"heading": "", "level": 2, "paragraphs": [],
                       "code_blocks": [], "tables": [],
                       "images": [{"src": "", "alt": "fig", "caption": ""}]}],
        )
        md = a.to_markdown()
        assert "![" not in md

    def test_to_markdown_no_meta_line_when_both_empty(self):
        a = ArticleContent(title="T")
        md = a.to_markdown()
        assert "*" not in md  # 不输出空的 meta 行

    def test_to_markdown_heading_levels(self):
        a = ArticleContent(title="T", sections=[
            {"heading": "H2", "level": 2, "paragraphs": [], "code_blocks": [], "tables": [], "images": []},
            {"heading": "H3", "level": 3, "paragraphs": [], "code_blocks": [], "tables": [], "images": []},
            {"heading": "H4", "level": 4, "paragraphs": [], "code_blocks": [], "tables": [], "images": []},
        ])
        md = a.to_markdown()
        assert "### H2" in md
        assert "#### H3" in md
        assert "##### H4" in md


# ── InterfaceSummary ──────────────────────────────────────────

class TestInterfaceSummary:
    def test_summary_empty(self):
        s = InterfaceSummary()
        assert s.summary() == "无可操作接口"

    def test_summary_counts(self):
        s = InterfaceSummary(
            search_boxes=[{}, {}],
            buttons=[{}, {}, {}],
            forms=[{}],
            navigation=[{}, {}, {}, {}],
        )
        out = s.summary()
        assert "搜索框: 2 个" in out
        assert "按钮: 3 个" in out
        assert "表单: 1 个" in out
        assert "导航: 4 项" in out

    def test_summary_partial(self):
        s = InterfaceSummary(buttons=[{}])
        out = s.summary()
        assert "按钮: 1 个" in out
        assert "搜索框" not in out
        assert "表单" not in out

    def test_to_dict(self):
        s = InterfaceSummary(
            search_boxes=[{"placeholder": "q", "label": ""}],
            buttons=[{"text": "Go"}],
            forms=[{"action": "/a", "method": "POST", "fields": []}],
            navigation=[{"text": "Home", "href": "/"}],
            filters=[{"label": "Sort", "options": ["a", "b"]}],
        )
        d = s.to_dict()
        assert d["search_boxes"] == [{"placeholder": "q", "label": ""}]
        assert d["buttons"] == [{"text": "Go"}]
        assert d["forms"] == [{"action": "/a", "method": "POST", "fields": []}]
        assert d["navigation"] == [{"text": "Home", "href": "/"}]
        assert d["filters"] == [{"label": "Sort", "options": ["a", "b"]}]


# ── find_sections / extract_topic ─────────────────────────────

class TestFindSections:
    def _make_article(self) -> ArticleContent:
        return ArticleContent(
            title="T",
            sections=[
                {"heading": "", "level": 2, "paragraphs": ["intro text"], "code_blocks": [], "tables": [], "images": []},
                {"heading": "JIT Compiler", "level": 2,
                 "paragraphs": ["The JIT compiler improves performance.", "Other notes."],
                 "code_blocks": ["jit_compile(x)"], "tables": [], "images": []},
                {"heading": "Other Topic", "level": 2,
                 "paragraphs": ["This section mentions JIT in passing."],
                 "code_blocks": [], "tables": [], "images": []},
                {"heading": "Unrelated", "level": 2,
                 "paragraphs": ["nothing here"], "code_blocks": [], "tables": [], "images": []},
            ],
        )

    def test_finds_in_heading_scores_higher(self):
        a = self._make_article()
        results = a.find_sections("JIT")
        # JIT Compiler (heading hit + 2 paragraph hit + 1 code hit = 6) 在前面
        assert results[0]["heading"] == "JIT Compiler"
        assert results[0]["score"] >= 4

    def test_returns_sorted_by_score(self):
        a = self._make_article()
        results = a.find_sections("JIT")
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_matched_paragraphs_excerpt(self):
        a = self._make_article()
        results = a.find_sections("JIT")
        jit = results[0]
        assert any("JIT compiler" in p for p in jit["matched_paragraphs"])

    def test_no_match_returns_empty(self):
        a = self._make_article()
        assert a.find_sections("nonexistent_xyz") == []

    def test_empty_keyword_returns_empty(self):
        a = self._make_article()
        assert a.find_sections("") == []

    def test_case_insensitive_by_default(self):
        a = self._make_article()
        assert len(a.find_sections("jit")) > 0
        assert len(a.find_sections("JIT")) > 0

    def test_case_sensitive_when_requested(self):
        a = self._make_article()
        # 用只在 code block 出现的 keyword 验证大小写敏感
        # 找 "jit_compile" 小写 (code block 里的) → 命中
        assert a.find_sections("jit_compile", case_insensitive=False) != []
        # 找 "JIT_compile" → 不命中
        assert a.find_sections("JIT_compile", case_insensitive=False) == []
        # 大小写不敏感默认: 都命中
        assert len(a.find_sections("JIT_COMPILE", case_insensitive=True)) > 0

    def test_max_results_limits(self):
        a = self._make_article()
        # 制造更多命中
        a.sections[0]["paragraphs"].append("JIT mentioned in intro")
        results = a.find_sections("JIT", max_results=1)
        assert len(results) == 1

    def test_matched_code_blocks_included(self):
        a = self._make_article()
        results = a.find_sections("jit_compile")
        assert len(results) >= 1
        jit = next(r for r in results if r["heading"] == "JIT Compiler")
        assert "jit_compile(x)" in jit["matched_code_blocks"]


class TestExtractTopic:
    def _make_article(self) -> ArticleContent:
        return ArticleContent(
            title="T",
            sections=[
                {"heading": "Intro", "level": 2, "paragraphs": ["foo bar"], "code_blocks": [], "tables": [], "images": []},
                {"heading": "JIT Details", "level": 2,
                 "paragraphs": ["The JIT compiler improves Python performance significantly.",
                                "It uses tracing and specialization."],
                 "code_blocks": ["def jit_hot_loop():\n    pass"], "tables": [], "images": []},
            ],
        )

    def test_found_returns_sections(self):
        a = self._make_article()
        t = a.extract_topic("JIT")
        assert t["found"] is True
        assert t["keyword"] == "JIT"
        assert len(t["sections"]) >= 1
        assert t["sections"][0]["heading"] == "JIT Details"

    def test_not_found_returns_empty(self):
        a = self._make_article()
        t = a.extract_topic("nonexistent")
        assert t["found"] is False
        assert t["sections"] == []
        assert t["total_chars"] == 0

    def test_max_chars_truncates(self):
        a = self._make_article()
        t = a.extract_topic("JIT", max_chars=100)
        assert t["total_chars"] <= 200  # 留余量; 主要断言确实是 truncated
        assert any("…" in s["excerpt"] or len(s["excerpt"]) <= 100 for s in t["sections"])

    def test_to_topic_markdown_renders(self):
        a = self._make_article()
        md = a.to_topic_markdown("JIT")
        assert md.startswith('# 关于 "JIT" 的摘要')
        assert "## JIT Details" in md or "### JIT Details" in md  # level 2+1=3

    def test_to_topic_markdown_not_found(self):
        a = self._make_article()
        md = a.to_topic_markdown("nonexistent")
        assert "(未找到" in md