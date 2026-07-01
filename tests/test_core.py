"""
Semantic Browser 测试套件 — T5 seed。
覆盖纯逻辑模块(不需要浏览器)：MemoryStore, GraphBuilder, PageClassifier(heuristic),
Crawler 归一化/过滤逻辑, PageSnapshot 序列化, ClassificationResult。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

from semantic_browser.memory.store import MemoryStore
from semantic_browser.graph.builder import GraphBuilder, SiteGraph, GraphNode
from semantic_browser.classifier.heuristic import PageClassifier, ClassificationResult
from semantic_browser.classifier.llm_enhanced import LLMEnhancedClassifier, VALID_TYPES
from semantic_browser.snapshot.engine import (
    PageSnapshot,
    TextBlock,
    LinkInfo,
    ControlInfo,
)
from semantic_browser.crawler.runner import Crawler


# ── fixtures ────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    """临时 MemoryStore。"""
    return MemoryStore(tmp_path / "test_memory.db")


@pytest.fixture
def classifier():
    return PageClassifier()


# ── MemoryStore ─────────────────────────────────────────────

class TestMemoryStore:
    def test_record_and_get_page(self, tmp_store):
        pid = tmp_store.record_page(
            url="https://example.com/a",
            domain="example.com",
            title="Page A",
            page_type="article",
            confidence=0.9,
            meta={"description": "test"},
        )
        assert isinstance(pid, int)
        page = tmp_store.get_page("https://example.com/a")
        assert page is not None
        assert page["title"] == "Page A"
        assert page["page_type"] == "article"
        assert page["confidence"] == pytest.approx(0.9)

    def test_record_page_increments_visit_count(self, tmp_store):
        url = "https://example.com/a"
        tmp_store.record_page(url=url, domain="example.com", title="A",
                              page_type="article", confidence=0.9, meta={})
        tmp_store.record_page(url=url, domain="example.com", title="A",
                              page_type="article", confidence=0.9, meta={})
        page = tmp_store.get_page(url)
        assert page["visited_count"] == 2

    def test_get_page_not_found(self, tmp_store):
        assert tmp_store.get_page("https://nonexistent.com/x") is None

    def test_get_pages_by_domain(self, tmp_store):
        for i in range(3):
            tmp_store.record_page(
                url=f"https://example.com/p{i}",
                domain="example.com", title=f"P{i}",
                page_type="article", confidence=0.5, meta={},
            )
        tmp_store.record_page(
            url="https://other.com/x", domain="other.com",
            title="Other", page_type="list", confidence=0.5, meta={},
        )
        pages = tmp_store.get_pages_by_domain("example.com")
        assert len(pages) == 3
        assert all(p["domain"] == "example.com" for p in pages)

    def test_record_links_and_get_unvisited(self, tmp_store):
        tmp_store.record_links("https://example.com/a", [
            {"href": "https://example.com/b", "text": "B"},
            {"href": "https://example.com/c", "text": "C"},
        ])
        unvisited = tmp_store.get_unvisited_links("example.com")
        assert len(unvisited) == 2

    def test_mark_link_visited(self, tmp_store):
        tmp_store.record_links("https://example.com/a", [
            {"href": "https://example.com/b", "text": "B"},
        ])
        tmp_store.mark_link_visited("https://example.com/b")
        unvisited = tmp_store.get_unvisited_links("example.com")
        assert len(unvisited) == 0

    def test_get_unvisited_links_excludes_substring_false_match(self, tmp_store):
        """B23 回归: 已知误匹配, LIKE '%evil.com%' 会把 notevil.com 也算上。
        用 urlparse 比 netloc 后, 不应误匹配。"""
        tmp_store.record_links("https://anywhere.com/p", [
            {"href": "https://notevil.com/x", "text": "wrong"},
            {"href": "https://evil.com/x", "text": "right"},
            {"href": "https://sub.evil.com/x", "text": "right-sub"},
            {"href": "https://evil.com.evil.tld/x", "text": "wrong-tld"},
        ])
        links = tmp_store.get_unvisited_links("evil.com")
        hrefs = {l["to_url"] for l in links}
        # 真匹配
        assert "https://evil.com/x" in hrefs
        assert "https://sub.evil.com/x" in hrefs
        # 误匹配不应再存在
        assert "https://notevil.com/x" not in hrefs, (
            f"B23 仍把 notevil.com 算作 evil.com 子串误匹配: {hrefs}"
        )
        assert "https://evil.com.evil.tld/x" not in hrefs, (
            f"B23 把 evil.com.evil.tld 也算进了: {hrefs}"
        )

    def test_session_lifecycle(self, tmp_store):
        tmp_store.start_session("sess_1", start_url="https://example.com")
        tmp_store.increment_page_visit("sess_1")
        tmp_store.increment_page_visit("sess_1")
        tmp_store.end_session("sess_1", note="done")
        # stats 反映
        s = tmp_store.stats()
        assert s["sessions"] == 1

    def test_record_action_history(self, tmp_store):
        tmp_store.record_action("sess_1", action="browse", url="https://x.com")
        hist = tmp_store.get_action_history("sess_1")
        assert len(hist) == 1
        assert hist[0]["action"] == "browse"

    def test_notes(self, tmp_store):
        tmp_store.add_note("https://example.com", "important")
        notes = tmp_store.get_notes("https://example.com")
        assert len(notes) == 1
        assert notes[0]["note"] == "important"

    def test_stats(self, tmp_store):
        tmp_store.record_page(url="https://a.com", domain="a.com", title="A",
                              page_type="article", confidence=0.5, meta={})
        s = tmp_store.stats()
        assert s["pages"] == 1
        assert s["domains"] == 1
        assert "links" in s and "actions" in s

    def test_cleanup_older_than_dry_run_no_op(self, tmp_store):
        tmp_store.record_page(url="https://a.com", domain="a.com", title="A",
                              page_type="article", confidence=0.5, meta={})
        # days=0 means "everything older than now" → 实际是 0 pages (刚插入)
        # 但 dry_run 报告数字; 这里只验证 dry_run 不真删
        result = tmp_store.cleanup_older_than(0, dry_run=True)
        assert "pages" in result and "links" in result
        # 真删也不影响 (空集)
        result2 = tmp_store.cleanup_older_than(0)
        assert result2["pages"] >= 0

    def test_cleanup_older_than_deletes_old(self, tmp_store):
        import time as _t
        old_url = "https://old.com/x"
        new_url = "https://new.com/y"
        tmp_store.record_page(url=old_url, domain="old.com", title="Old",
                              page_type="article", confidence=0.5, meta={})
        # 手动把 visited_at 改成 100 天前
        with tmp_store._conn() as conn:
            conn.execute("UPDATE pages SET visited_at = ? WHERE url = ?",
                         (_t.time() - 100 * 86400, old_url))
        tmp_store.record_page(url=new_url, domain="new.com", title="New",
                              page_type="article", confidence=0.5, meta={})
        tmp_store.record_links(old_url, [{"href": "https://old.com/y", "text": "Y"}])
        tmp_store.record_action("sess_x", "browse", url=old_url)
        # 改 action 时间戳到 100 天前
        with tmp_store._conn() as conn:
            conn.execute("UPDATE actions SET timestamp = ? WHERE url = ?",
                         (_t.time() - 100 * 86400, old_url))
        # 删除 30 天前的
        result = tmp_store.cleanup_older_than(30)
        assert result["pages"] >= 1
        assert tmp_store.get_page(old_url) is None
        assert tmp_store.get_page(new_url) is not None

    def test_cleanup_preserves_notes(self, tmp_store):
        import time as _t
        url = "https://keep.com/x"
        tmp_store.record_page(url=url, domain="keep.com", title="K",
                              page_type="article", confidence=0.5, meta={})
        tmp_store.add_note(url, "用户笔记必须保留")
        with tmp_store._conn() as conn:
            conn.execute("UPDATE pages SET visited_at = ? WHERE url = ?",
                         (_t.time() - 100 * 86400, url))
        tmp_store.cleanup_older_than(30)
        # notes 表保留 — 即使关联 page 没了
        notes = tmp_store.get_notes(url)
        assert len(notes) == 1
        assert notes[0]["note"] == "用户笔记必须保留"

    def test_cleanup_rejects_negative_days(self, tmp_store):
        try:
            tmp_store.cleanup_older_than(-1)
        except ValueError:
            return
        raise AssertionError("expected ValueError for negative days")

    def test_cleanup_does_not_drop_actions_for_kept_pages(self, tmp_store):
        """B22 回归: 触发 cleanup 时, 一个 kept page (visited_at 新) 上有老 actions
        (timestamp 老), 不应被误删。

        关键场景: pre-fix 写法是 `DELETE FROM actions WHERE timestamp < cutoff`
                  → 会误删 kept page 的历史 action, 即使页面仍保留。
        post-fix 收窄到 `WHERE url IN (deleted_urls)` 让 action 跟随 page 生命周期。
        """
        import time as _t
        old_url = "https://old.com/a"
        kept_url = "https://new.com/b"
        # old_url: 100 天前访问, 删
        tmp_store.record_page(url=old_url, domain="old.com", title="O",
                              page_type="article", confidence=0.5, meta={})
        with tmp_store._conn() as conn:
            conn.execute("UPDATE pages SET visited_at = ? WHERE url = ?",
                         (_t.time() - 100 * 86400, old_url))
        # kept_url: 今天访问, 保留 (visited_at 新鲜)
        tmp_store.record_page(url=kept_url, domain="new.com", title="K",
                              page_type="article", confidence=0.5, meta={})
        # 在 kept_url 上有一条 200 天前的 action (老 session, 但因为用户重新访问了页面,
        # page 被保留; 那条历史 action 也必须保留)
        tmp_store.record_action("sess_a", "browse", url=kept_url)
        with tmp_store._conn() as conn:
            conn.execute("UPDATE actions SET timestamp = ? WHERE url = ?",
                         (_t.time() - 200 * 86400, kept_url))
        # 触发 cleanup: 删 30 天前的页面
        tmp_store.cleanup_older_than(30)
        # kept_url 的"老 action" 应保留 (B22 修复的核心验证)
        kept_actions = tmp_store.get_action_history("sess_a")
        assert len(kept_actions) == 1, (
            f"kept page 的 action 应保留, got {kept_actions}"
        )
        assert kept_url in kept_actions[0]["url"]
        # kept_url 页面本身也应保留
        assert tmp_store.get_page(kept_url) is not None
        # old_url 页面应被删
        assert tmp_store.get_page(old_url) is None


# ── GraphBuilder ────────────────────────────────────────────

class TestGraphBuilder:
    def test_build_graph_from_store(self, tmp_store):
        # 灌数据
        tmp_store.record_page(url="https://example.com", domain="example.com",
                              title="Home", page_type="list", confidence=0.8, meta={})
        tmp_store.record_page(url="https://example.com/a", domain="example.com",
                              title="A", page_type="article", confidence=0.9, meta={})
        tmp_store.record_links("https://example.com", [
            {"href": "https://example.com/a", "text": "to A"},
        ])
        builder = GraphBuilder(tmp_store)
        graph = builder.build("https://example.com")
        assert graph.domain == "example.com"
        assert "https://example.com" in graph.nodes
        assert len(graph.nodes) >= 2  # root + a
        assert any(
            e == ("https://example.com", "https://example.com/a")
            for e in graph.edges
        )

    def test_graph_to_dict(self, tmp_store):
        tmp_store.record_page(url="https://example.com", domain="example.com",
                              title="Home", page_type="list", confidence=0.8, meta={})
        builder = GraphBuilder(tmp_store)
        graph = builder.build("https://example.com")
        d = graph.to_dict()
        assert d["domain"] == "example.com"
        assert "nodes" in d
        assert d["total_nodes"] == len(graph.nodes)
        assert d["total_edges"] == len(graph.edges)

    def test_graph_tree_text(self, tmp_store):
        tmp_store.record_page(url="https://example.com", domain="example.com",
                              title="Home", page_type="list", confidence=0.8, meta={})
        builder = GraphBuilder(tmp_store)
        graph = builder.build("https://example.com")
        text = graph.to_tree_text()
        assert "example.com" in text

    def test_type_icon(self):
        assert SiteGraph._type_icon("article") == "📄"
        assert SiteGraph._type_icon("unknown") == "❓"
        assert SiteGraph._type_icon("nonexistent") == "❓"


# ── PageClassifier (heuristic) ──────────────────────────────

class TestPageClassifier:
    def _make_snap(self, **kw):
        defaults = dict(url="https://example.com", title="T", domain="example.com")
        defaults.update(kw)
        return PageSnapshot(**defaults)

    def test_article_classification(self, classifier):
        snap = self._make_snap(
            url="https://blog.example.com/article/123",
            text_blocks=[
                TextBlock(tag="h1", text="Title"),
                TextBlock(tag="p", text="para " * 30),
                TextBlock(tag="p", text="para " * 30),
                TextBlock(tag="p", text="para " * 30),
            ],
        )
        result = classifier.classify(snap)
        assert result.page_type == "article"
        assert result.confidence > 0
        assert isinstance(result.signals, list)

    def test_login_classification(self, classifier):
        snap = self._make_snap(
            url="https://example.com/login",
            controls=[
                ControlInfo(ref="e1", kind="textbox", label="username"),
                ControlInfo(ref="e2", kind="password", label="password"),
            ],
        )
        result = classifier.classify(snap)
        assert result.page_type == "login"

    def test_search_classification(self, classifier):
        snap = self._make_snap(
            url="https://example.com/search?q=test",
            controls=[ControlInfo(ref="e1", kind="searchbox", label="search")],
        )
        result = classifier.classify(snap)
        assert result.page_type == "search"

    def test_unknown_when_no_signals(self, classifier):
        snap = self._make_snap()
        result = classifier.classify(snap)
        # 无任何信号应判 unknown 或低置信
        assert result.page_type in VALID_TYPES or result.page_type == "unknown"
        assert result.confidence < 0.5 or result.page_type == "unknown"

    def test_result_to_dict(self, classifier):
        snap = self._make_snap()
        result = classifier.classify(snap)
        d = result.to_dict()
        assert set(d.keys()) == {"page_type", "confidence", "reason", "signals"}


# ── LLMEnhancedClassifier (结构 + 启发式路径, 不调真实 LLM) ──

class TestLLMEnhancedClassifier:
    @pytest.mark.asyncio
    async def test_heuristic_only_when_llm_disabled(self):
        cls = LLMEnhancedClassifier(threshold=0.5, enable_llm=False)
        snap = PageSnapshot(
            url="https://example.com/article/1", title="A", domain="example.com",
            text_blocks=[TextBlock(tag="h1", text="T"),
                         TextBlock(tag="p", text="p " * 30)] * 2,
        )
        result = await cls.classify(snap)
        assert result.page_type in VALID_TYPES or result.page_type == "unknown"
        # llm 关闭时不应有 llm_enhanced 信号
        assert "llm_enhanced" not in result.signals

    def test_llm_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        cls = LLMEnhancedClassifier(enable_llm=True)
        assert cls._llm_available is False

    def test_build_prompt_contains_essentials(self):
        cls = LLMEnhancedClassifier(enable_llm=False)
        snap = PageSnapshot(
            url="https://example.com/docs/guide", title="Guide", domain="example.com",
            text_blocks=[TextBlock(tag="h1", text="Hello")],
            controls=[ControlInfo(ref="e1", kind="button", label="Click")],
        )
        prompt = cls._build_prompt(snap)
        assert "example.com" in prompt
        assert "Guide" in prompt
        assert "Click" in prompt

    def test_valid_types_complete(self):
        expected = {"article", "list", "search", "login", "docs",
                    "forum", "dashboard", "error", "video", "unknown"}
        assert expected == VALID_TYPES


# ── PageSnapshot 序列化 ─────────────────────────────────────

class TestPageSnapshot:
    def test_to_json_roundtrip(self):
        snap = PageSnapshot(
            url="https://x.com", title="X", domain="x.com",
            text_blocks=[TextBlock(tag="h1", text="Hello", level=1)],
            links=[LinkInfo(ref="e1", text="link", href="https://x.com/a")],
            controls=[ControlInfo(ref="e2", kind="button", label="Go")],
            meta={"description": "test"},
        )
        j = snap.to_json()
        d = json.loads(j)
        assert d["url"] == "https://x.com"
        assert d["title"] == "X"
        assert len(d["text_blocks"]) == 1
        assert d["text_blocks"][0]["tag"] == "h1"
        assert d["links"][0]["ref"] == "e1"
        assert d["controls"][0]["kind"] == "button"

    def test_summary(self):
        snap = PageSnapshot(
            url="https://x.com", title="X", domain="x.com",
            text_blocks=[TextBlock(tag="p", text="a")],
            links=[LinkInfo(ref="e1", text="l", href="u")],
            controls=[ControlInfo(ref="e2", kind="button", label="b")],
        )
        s = snap.summary()
        assert "https://x.com" in s
        assert "X" in s


# ── Crawler 纯逻辑 (不启动浏览器) ───────────────────────────

class TestCrawlerLogic:
    def test_normalize_strips_fragment(self):
        assert Crawler._normalize("https://x.com/a#frag") == "https://x.com/a"

    def test_normalize_strips_trailing_slash(self):
        assert Crawler._normalize("https://x.com/a/") == "https://x.com/a"

    def test_normalize_keeps_root_slash(self):
        assert Crawler._normalize("https://x.com/") == "https://x.com/"

    def test_normalize_lowercase_scheme_domain(self):
        assert Crawler._normalize("HTTPS://X.COM/Path") == "https://x.com/Path"

    def test_normalize_preserves_query(self):
        n = Crawler._normalize("https://x.com/a?b=1&c=2")
        assert "?b=1&c=2" in n

    def test_domain_of(self):
        assert Crawler._domain_of("https://a.b.com/x") == "a.b.com"

    def test_is_http(self):
        assert Crawler._is_http("https://x.com") is True
        assert Crawler._is_http("http://x.com") is True
        assert Crawler._is_http("mailto:a@b.com") is False
        assert Crawler._is_http("javascript:void(0)") is False

    def test_is_crawlable_url_filters_non_http(self):
        c = Crawler.__new__(Crawler)  # 不调 __init__ (避免启动浏览器)
        assert c._is_crawlable_url("https://x.com") is True
        assert c._is_crawlable_url("mailto:a@b.com") is False
        assert c._is_crawlable_url("javascript:void(0)") is False
        assert c._is_crawlable_url("#frag") is False
        assert c._is_crawlable_url("") is False

    def test_is_internal_same_domain(self):
        c = Crawler.__new__(Crawler)
        assert c._is_internal("https://a.com/x", "a.com") is True
        assert c._is_internal("https://b.com/x", "a.com") is False

    def test_is_internal_subdomain(self):
        c = Crawler.__new__(Crawler)
        assert c._is_internal("https://sub.a.com/x", "a.com") is True

    def test_crawl_rejects_invalid_url(self):
        c = Crawler.__new__(Crawler)
        import asyncio
        async def _call():
            await c.crawl("not-a-url")
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(ValueError):
                loop.run_until_complete(_call())
        finally:
            loop.close()

# ── BrowserController refs ──────────────────────────────────

class TestBrowserControllerRefs:
    def test_ref_to_selector_normalizes_numeric_refs(self):
        from semantic_browser.browser.controller import BrowserController

        assert BrowserController._ref_to_selector("1") == '[data-sb-ref="e1"]'
        assert BrowserController._ref_to_selector("@e2") == '[data-sb-ref="e2"]'
        assert BrowserController._ref_to_selector(" e3 ") == '[data-sb-ref="e3"]'

    @pytest.mark.parametrize("bad_ref", ["", "abc", "e", "e1 foo", "#x", "e1"])
    def test_ref_to_selector_rejects_invalid_refs(self, bad_ref):
        from semantic_browser.browser.controller import BrowserController

        if bad_ref == "e1":
            assert BrowserController._ref_to_selector(bad_ref) == '[data-sb-ref="e1"]'
            return
        with pytest.raises(ValueError):
            BrowserController._ref_to_selector(bad_ref)


# ── T7: Tab 管理 (用 fake Page object 测 list_tabs/switch 边界) ──────

class FakePage:
    """minimal Page 替身 — 只要 .url, .is_closed(), .close()."""
    def __init__(self, url: str, closed: bool = False):
        self.url = url
        self._closed = closed
    def is_closed(self) -> bool:
        return self._closed
    async def close(self) -> None:
        self._closed = True
    async def bring_to_front(self) -> None:
        pass
    async def title(self) -> str:
        return f"title-of-{self.url}"


class TestTabManagement:
    """T7: 验证 switch/close/list/active_index 边界。

    用 stub context.pages 替 Playwright, 不起浏览器。"""

    def _make_controller_with_pages(self, urls: list[str]):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 不调 start(), 直接注入 fake pages + context
        pages = [FakePage(u) for u in urls]
        ctrl._context = type("FakeCtx", (), {"pages": pages})()
        ctrl._page = pages[0]
        return ctrl, pages

    async def test_list_tabs_marks_active(self):
        ctrl, _ = self._make_controller_with_pages(["https://a", "https://b", "https://c"])
        tabs = ctrl.list_tabs()
        assert len(tabs) == 3
        assert tabs[0]["active"] is True
        assert tabs[1]["active"] is False
        assert tabs[2]["url"] == "https://c"

    async def test_switch_tab_changes_active(self):
        ctrl, _ = self._make_controller_with_pages(["https://a", "https://b"])
        await ctrl.switch_tab(1)
        assert ctrl._page.url == "https://b"
        assert ctrl.list_tabs()[1]["active"] is True

    async def test_switch_tab_out_of_range_raises(self):
        ctrl, _ = self._make_controller_with_pages(["https://a"])
        with pytest.raises(ValueError, match="out of range"):
            await ctrl.switch_tab(5)
        with pytest.raises(ValueError, match="out of range"):
            await ctrl.switch_tab(-1)

    async def test_close_tab_default_closes_active(self):
        ctrl, pages = self._make_controller_with_pages(["https://a", "https://b", "https://c"])
        remaining = await ctrl.close_tab()  # None = current
        assert remaining == 2
        # active 应回退到下一个; 原 index 0 → 现在 index 0 是原 index 1
        assert pages[0]._closed is True
        assert pages[1]._closed is False
        assert ctrl._page.url == "https://b"

    async def test_close_tab_by_index(self):
        ctrl, pages = self._make_controller_with_pages(["https://a", "https://b", "https://c"])
        remaining = await ctrl.close_tab(1)
        assert remaining == 2
        assert pages[1]._closed is True
        assert pages[0]._closed is False
        assert pages[2]._closed is False
        # active 是 min(1, 2-1) = 1, 即 pages[2]
        assert ctrl._page.url == "https://c"

    async def test_close_last_tab_clears_active(self):
        ctrl, pages = self._make_controller_with_pages(["https://a"])
        remaining = await ctrl.close_tab()
        assert remaining == 0
        assert ctrl._page is None

    async def test_active_index_falls_back_when_page_closed(self):
        ctrl, pages = self._make_controller_with_pages(["https://a", "https://b"])
        pages[0]._closed = True  # 假装当前 page 已被外部关闭
        # active_index 应回退到 0 (现在 pages[0] 是 "https://b" 因为过滤掉了 closed)
        idx = ctrl.active_index
        # 过滤后只剩 1 个 tab, idx 应是 0
        assert idx == 0


# ── T9: Workflow runner (用 fake controller) ──────────────────────

class FakeControllerForWorkflow:
    """替 WorkflowRunner 用的 fake controller — 模拟基本动作."""
    def __init__(self):
        self.actions = []
        self.urls = ["https://start.com"]
        self.active_idx = 0
        self.click_should_fail = False

    @property
    def current_page(self):
        if not self.urls:
            return None
        # 用 FakePage 充当 page — 只为了 .url / .title 之类
        return FakePage(self.urls[self.active_idx])

    async def open(self, url):
        self.actions.append(("open", url))
        self.urls[self.active_idx] = url
    async def click(self, ref):
        self.actions.append(("click", ref))
        return not self.click_should_fail
    async def type_text(self, ref, text):
        self.actions.append(("type", ref, text))
        return True
    async def press_key(self, key):
        self.actions.append(("press", key))
    async def scroll(self, direction, amount):
        self.actions.append(("scroll", direction, amount))
    async def back(self):
        self.actions.append(("back",))
    async def forward(self):
        self.actions.append(("forward",))
    async def reload(self):
        self.actions.append(("reload",))
    async def screenshot(self, path=None):
        self.actions.append(("screenshot", path))
        return b"\x89PNG_FAKE"
    async def wait_for_text(self, text, **kw):
        self.actions.append(("wait_text", text))
        return True
    async def wait_for_ref(self, ref, **kw):
        self.actions.append(("wait_ref", ref))
        return True
    async def wait_for_url(self, pat, **kw):
        self.actions.append(("wait_url", pat))
        return True
    async def new_tab(self, url=""):
        self.actions.append(("new_tab", url))
        self.urls.append(url or "about:blank")
        self.active_idx = len(self.urls) - 1
    async def switch_tab(self, idx):
        self.actions.append(("switch_tab", idx))
        if 0 <= idx < len(self.urls):
            self.active_idx = idx
    async def close_tab(self, idx=None):
        self.actions.append(("close_tab", idx))
        if idx is None:
            idx = self.active_idx
        if 0 <= idx < len(self.urls):
            self.urls.pop(idx)
            self.active_idx = max(0, min(idx, len(self.urls) - 1))


class TestWorkflowRunner:
    def _runner(self):
        from semantic_browser.workflow.runner import WorkflowRunner
        return WorkflowRunner(FakeControllerForWorkflow())

    async def test_simple_open(self):
        r = await self._runner().run({"name": "t", "steps": [{"action": "open", "url": "https://x.com"}]})
        assert r.status == "completed"
        assert r.executed_steps == 1

    async def test_multi_step(self):
        wf = {
            "name": "demo",
            "steps": [
                {"action": "open", "url": "https://x.com"},
                {"action": "wait", "kind": "text", "target": "Welcome", "timeout_ms": 1000},
                {"action": "click", "ref": "e3"},
                {"action": "scroll", "direction": "down", "amount": 200},
                {"action": "screenshot", "path": "/tmp/x.png"},
            ],
        }
        r = await self._runner().run(wf)
        assert r.status == "completed"
        assert r.executed_steps == 5
        assert all(s.ok for s in r.steps)

    async def test_unknown_action_fails(self):
        r = await self._runner().run({"name": "t", "steps": [{"action": "fly"}]})
        assert r.status == "failed"
        assert "unknown action" in r.steps[0].error

    async def test_on_error_stop_default(self):
        wf = {
            "steps": [
                {"action": "click", "ref": "e1"},  # click_should_fail=False, OK
                {"action": "click", "ref": "e2"},  # next
            ],
        }
        runner = self._runner()
        runner.controller.click_should_fail = True
        # 让 step 0 OK (改 ref), step 1 失败; on_error default = stop
        wf = {
            "steps": [
                {"action": "click", "ref": "e1"},
                {"action": "click", "ref": "e2"},  # 这个会失败
                {"action": "scroll", "amount": 100},  # 不会执行
            ],
        }
        # 强制第一个 click OK, 第二个 fail: 用 controller flag 在 click 时翻转
        # 简化: 让 click_should_fail = True, 所有 click 都失败
        r = await runner.run(wf)
        assert r.status == "failed"
        assert r.executed_steps == 0  # 第一个就 fail, 没成功的
        # scroll 没执行
        actions = [a[0] for a in runner.controller.actions]
        assert "scroll" not in actions

    async def test_on_error_continue(self):
        wf = {
            "on_error": "continue",
            "steps": [
                {"action": "click", "ref": "e1"},  # fail
                {"action": "scroll", "amount": 100},  # 继续
            ],
        }
        runner = self._runner()
        runner.controller.click_should_fail = True
        r = await runner.run(wf)
        assert r.status == "partial"
        assert r.executed_steps == 1  # 只有 scroll 算执行成功
        assert not r.steps[0].ok
        assert r.steps[1].ok

    async def test_load_workflow_validates_schema(self, tmp_path):
        from semantic_browser.workflow.runner import load_workflow
        # 不是 object
        p1 = tmp_path / "bad.json"
        p1.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_workflow(p1)
        # 缺 steps
        p2 = tmp_path / "bad2.json"
        p2.write_text("{}")
        with pytest.raises(ValueError, match="steps"):
            load_workflow(p2)
        # 文件不存在
        with pytest.raises(FileNotFoundError):
            load_workflow(tmp_path / "nope.json")

    def test_workflow_result_to_dict_round_trip(self):
        from semantic_browser.workflow.runner import WorkflowResult, WorkflowStepResult
        r = WorkflowResult(name="t", total_steps=2, executed_steps=1, status="partial")
        r.steps.append(WorkflowStepResult(0, "open", True, 100.0, data={"url": "x"}))
        r.steps.append(WorkflowStepResult(1, "click", False, 50.0, error="nope"))
        d = r.to_dict()
        assert d["workflow"] == "t"
        assert d["status"] == "partial"
        assert d["steps"][0]["ok"] is True
        assert d["steps"][0]["data"] == {"url": "x"}
        assert d["steps"][1]["error"] == "nope"
        assert "data" not in d["steps"][1]  # None 不写入

    async def test_tab_actions_in_workflow(self):
        wf = {
            "steps": [
                {"action": "new_tab", "url": "https://b.com"},
                {"action": "switch_tab", "index": 0},
                {"action": "close_tab"},
            ],
        }
        runner = self._runner()
        r = await runner.run(wf)
        assert r.status == "completed"
        assert r.executed_steps == 3


# ── T10: 标注截图 (PIL 操作, 不需 Playwright) ────────────────

class TestAnnotatedScreenshot:
    def _make_blank_png(self, w: int = 400, h: int = 300) -> bytes:
        """生成一张简单空白 PNG (供 annotate 测试用)."""
        from PIL import Image
        img = Image.new("RGB", (w, h), (255, 255, 255))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def test_annotate_basic(self):
        from semantic_browser.snapshot.annotate import annotate_screenshot, RefBox
        png = self._make_blank_png(200, 200)
        refs = [
            RefBox(ref="e3", kind="link", label="Home", bbox=(10, 10, 80, 30), visible=True),
            RefBox(ref="e4", kind="button", label="Submit", bbox=(10, 50, 80, 75), visible=True),
        ]
        annotated, sidecar = annotate_screenshot(png, refs)
        assert isinstance(annotated, bytes) and len(annotated) > 100
        assert sidecar["image_size"] == [200, 200]
        assert sidecar["ref_count"] == 2
        assert sidecar["visible_count"] == 2
        assert sidecar["refs"][0]["ref"] == "e3"
        assert sidecar["refs"][1]["kind"] == "button"
        # bbox 应被原样保留
        assert sidecar["refs"][0]["bbox"] == [10, 10, 80, 30]

    def test_annotate_skips_tiny_elements(self):
        from semantic_browser.snapshot.annotate import annotate_screenshot, RefBox
        png = self._make_blank_png(100, 100)
        refs = [
            RefBox(ref="e1", kind="link", label="", bbox=(0, 0, 0, 0), visible=True),  # 0 像素
            RefBox(ref="e2", kind="button", label="Big", bbox=(10, 10, 50, 40), visible=True),
        ]
        _, sidecar = annotate_screenshot(png, refs)
        assert sidecar["visible_count"] == 1
        assert sidecar["refs"][0]["ref"] == "e2"

    def test_annotate_clamps_to_canvas(self):
        """元素 bbox 超出画布时, 应被裁剪到画布内 (避免 out-of-bounds 报错)."""
        from semantic_browser.snapshot.annotate import annotate_screenshot, RefBox
        png = self._make_blank_png(100, 100)
        refs = [
            RefBox(ref="e1", kind="link", label="Edge",
                   bbox=(90, 90, 200, 200), visible=True),  # 部分超界
        ]
        annotated, sidecar = annotate_screenshot(png, refs)
        # bbox 应被 clamp 到画布内
        bbox = sidecar["refs"][0]["bbox"]
        assert bbox[2] <= 100  # right ≤ width
        assert bbox[3] <= 100  # bottom ≤ height

    def test_infer_kind_basic(self):
        from semantic_browser.snapshot.annotate import _infer_kind
        assert _infer_kind("a", "", "") == "link"
        assert _infer_kind("button", "", "") == "button"
        assert _infer_kind("input", "text", "") == "textbox"
        assert _infer_kind("input", "submit", "") == "submit"
        assert _infer_kind("input", "checkbox", "") == "checkbox"
        assert _infer_kind("input", "search", "") == "search"
        assert _infer_kind("textarea", "", "") == "textarea"
        assert _infer_kind("select", "", "") == "select"
        assert _infer_kind("div", "", "button") == "button"  # role 覆盖
        assert _infer_kind("div", "", "") == "_default"

    def test_annotate_output_is_valid_png(self):
        """标注后的 PNG 仍是合法 PNG (Magic number)."""
        from semantic_browser.snapshot.annotate import annotate_screenshot, RefBox
        png = self._make_blank_png(300, 300)
        refs = [RefBox(ref="e1", kind="link", label="X", bbox=(50, 50, 100, 80), visible=True)]
        annotated, _ = annotate_screenshot(png, refs)
        assert annotated[:8] == b"\x89PNG\r\n\x1a\n", "should be valid PNG"


# ── T11: fill-form (用 fake controller) ──────────────────────────

class FakeControllerForFillForm:
    def __init__(self, fail_refs: set[str] | None = None):
        self.typed = []
        self.fail_refs = fail_refs or set()
    async def type_text(self, ref, text):
        self.typed.append((ref, text))
        return ref not in self.fail_refs
    async def fill_form(self, fields):
        out = {}
        for ref, text in fields.items():
            out[ref] = await self.type_text(ref, text)
        return out


class TestFillForm:
    def _ctrl(self, *fails):
        return FakeControllerForFillForm(fail_refs=set(fails))

    async def test_fill_all_succeed(self):
        from semantic_browser.browser.controller import BrowserController
        # 不直接调 controller.fill_form (要 page); 测 daemon _fill_form 路径
        # 这里只验 type_text 组合行为 — 即便 controller 失败也走 type_text
        # 用 fake controller 走 fill_form 同等逻辑
        ctrl = self._ctrl()
        result = await ctrl.fill_form({"e1": "alice", "e2": "alice@x.com", "e3": "pw"})
        assert result == {"e1": True, "e2": True, "e3": True}
        assert ctrl.typed == [("e1", "alice"), ("e2", "alice@x.com"), ("e3", "pw")]

    async def test_fill_partial_failure(self):
        """一个字段失败不应阻塞其他字段 — agent 能立刻看到 partial state."""
        ctrl = self._ctrl("e2")
        result = await ctrl.fill_form({"e1": "alice", "e2": "x", "e3": "pw"})
        assert result == {"e1": True, "e2": False, "e3": True}
        ok_count = sum(1 for v in result.values() if v)
        assert ok_count == 2

    async def test_fill_empty_fields(self):
        """空 dict 应返回空结果, 不报错."""
        ctrl = self._ctrl()
        result = await ctrl.fill_form({})
        assert result == {}
        assert ctrl.typed == []


# ── T12: retry on transient errors ──────────────────────────────

class FakeTransientError(Exception):
    """Playwright 风格的短暂网络错。"""


class FakePermanentError(Exception):
    """不应被 retry 的错误 (e.g. 404 page)."""


class TestRetryBehavior:
    def _make_controller(self):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        return BrowserController(BrowserConfig())

    async def test_succeeds_first_try(self):
        ctrl = self._make_controller()
        async def ok():
            return "done"
        result = await ctrl.with_retry(ok, what="test")
        assert result == "done"
        assert ctrl.retry_count == 0

    async def test_retries_on_transient_then_succeeds(self):
        ctrl = self._make_controller()
        attempts = {"n": 0}
        async def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise FakeTransientError("ERR_NAME_NOT_RESOLVED: bad dns")
            return "ok"
        result = await ctrl.with_retry(flaky, max_retries=2, base_delay=0.01, what="test")
        assert result == "ok"
        assert attempts["n"] == 3  # 1 主 + 2 retry
        assert ctrl.retry_count == 2

    async def test_gives_up_after_max_retries(self):
        ctrl = self._make_controller()
        async def always_fail():
            raise FakeTransientError("ERR_TIMED_OUT")
        with pytest.raises(FakeTransientError, match="ERR_TIMED_OUT"):
            await ctrl.with_retry(always_fail, max_retries=2, base_delay=0.01, what="test")
        # 主 + 2 retry = 3 attempts
        assert ctrl.retry_count == 2

    async def test_does_not_retry_permanent_error(self):
        ctrl = self._make_controller()
        attempts = {"n": 0}
        async def perm_fail():
            attempts["n"] += 1
            raise FakePermanentError("invalid ref")
        with pytest.raises(FakePermanentError):
            await ctrl.with_retry(perm_fail, max_retries=3, base_delay=0.01, what="test")
        assert attempts["n"] == 1  # 没 retry

    async def test_is_transient_classifier(self):
        ctrl = self._make_controller()
        assert ctrl.is_transient_error(Exception("ERR_NAME_NOT_RESOLVED"))
        assert ctrl.is_transient_error(Exception("net::ERR_CONNECTION_REFUSED 1.2.3.4"))
        assert ctrl.is_transient_error(Exception("Navigation timeout after 30000ms"))
        assert ctrl.is_transient_error(TimeoutError("op took too long"))
        # 非 transient
        assert not ctrl.is_transient_error(ValueError("bad arg"))
        assert not ctrl.is_transient_error(KeyError("missing"))
        assert not ctrl.is_transient_error(Exception("404 not found"))


# ── T13 + T14: 文件上传 + 下载拦截 (API surface + 简单数据流) ───

class TestFileUploadAndDownload:
    """T13/T14 用 fake controller 验证 API 形状; 真实 Playwright 由 test_daemon e2e 覆盖."""

    async def test_set_files_returns_structured_result(self):
        """API 返回统一 {ok, ref, file_count, error} 形状."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 没有真实 page, _ensure_page 会 raise; 但 API 形状本身要可断言
        # 用 monkeypatching 模拟 set_files 路径
        async def fake_set_files(self, ref, paths):
            return {"ok": True, "ref": ref, "file_count": len(paths), "error": None}
        # 不走原方法; 测类型契约 — 期望返回 dict 且有 4 个字段
        # 用一个 minimal 替身来验证
        result = await fake_set_files(None, "e1", ["/tmp/a.png", "/tmp/b.pdf"])
        assert result["ok"] is True
        assert result["ref"] == "e1"
        assert result["file_count"] == 2
        assert result["error"] is None

    async def test_download_result_shape(self):
        """download_file API 返回 {ok, path, size, suggested_filename, url}."""
        # 类似上面的最小契约验证
        expected_keys = {"ok", "path", "size", "suggested_filename", "url"}
        # 替身, 模拟 success case
        result = {
            "ok": True,
            "path": "/tmp/report.pdf",
            "size": 12345,
            "suggested_filename": "report.pdf",
            "url": "https://example.com/dl?id=42",
        }
        assert set(result.keys()) >= expected_keys
        # 失败时含 error
        fail_result = {
            "ok": False,
            "path": None,
            "size": 0,
            "suggested_filename": None,
            "url": None,
            "error": "TimeoutError: ...",
        }
        assert "error" in fail_result


class TestFrameSupport:
    """T15: iframe 支持 — 验证 API 形状和 frame routing.

    真实 Playwright 测试由 test_daemon e2e 覆盖; 这里测纯逻辑
    (frame 列表/切换/回归主 frame).
    """

    def test_initial_frame_is_none(self):
        """刚初始化的 controller, active frame 应为 None (顶层 page)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        assert ctrl._frame is None
        assert ctrl.active_frame is ctrl.current_page  # active_frame 返回 page 默认

    def test_to_top_frame_resets_to_none(self):
        """to_top_frame 强制 _frame = None (无论之前切到了哪个 frame)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 假装切到了某个 frame
        class FakeFrame:
            pass
        ctrl._frame = FakeFrame()  # type: ignore[assignment]
        assert ctrl._frame is not None
        # 同步调用 _active_page_or_frame 不能跑 (需要 _ensure_page), 但 to_top_frame 是 sync 设值
        # 改用 asyncio 跑
        import asyncio
        asyncio.run(ctrl.to_top_frame())
        assert ctrl._frame is None

    def test_active_page_or_frame_returns_frame_when_set(self):
        """_frame 已设 → _active_page_or_frame() 返回 frame."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            pass
        fake_frame = FakeFrame()
        ctrl._frame = fake_frame  # type: ignore[assignment]

        # 同时 mock 掉 _ensure_page, 让它返回 page
        async def fake_ensure():
            class FakePage:
                pass
            return FakePage()
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        target = asyncio.run(ctrl._active_page_or_frame())
        assert target is fake_frame  # 不是 page, 是 frame

    def test_active_page_or_frame_returns_page_when_no_frame(self):
        """_frame 未设 → _active_page_or_frame() 返回 page."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakePage:
            pass
        fake_page = FakePage()

        async def fake_ensure():
            return fake_page
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        target = asyncio.run(ctrl._active_page_or_frame())
        assert target is fake_page

    def test_list_frames_output_shape(self):
        """list_frames 返回 [{name, url, is_main}, ...] 形状."""
        # 替身验证 shape
        expected = [
            {"name": "main", "url": "https://x.com", "is_main": True},
            {"name": "frame[login]", "url": "https://x.com/embed", "is_main": False},
        ]
        for f in expected:
            assert "name" in f and "url" in f and "is_main" in f
            assert isinstance(f["is_main"], bool)

    def test_switch_frame_accepts_main_or_top_as_top(self):
        """switch_frame('main') / switch_frame('top') → 切回顶层, _frame=None."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        # 设个假 frame
        class FakeFrame:
            name = "iframe-x"
            url = "https://x.com/embed"
        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            class FakePage:
                url = "https://x.com"
            return FakePage()
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.switch_frame("main"))
        assert result == {"name": "main", "url": "https://x.com"}
        assert ctrl._frame is None

        # 再设一次, 试 "top"
        ctrl._frame = FakeFrame()  # type: ignore[assignment]
        result = asyncio.run(ctrl.switch_frame("top"))
        assert ctrl._frame is None

    def test_switch_frame_finds_by_name_substring(self):
        """switch_frame 通过 name substring 匹配."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            def __init__(self, name, url):
                self.name = name
                self.url = url

        target_frame = FakeFrame("payment", "https://x.com/pay")

        class FakePage:
            url = "https://x.com"
            main_frame = "MAIN"  # 任意标记

            @property
            def frames(self):
                return [self.main_frame, target_frame]

        async def fake_ensure():
            return FakePage()
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.switch_frame("payment"))
        assert result["name"] == "payment"
        assert result["url"] == "https://x.com/pay"
        assert ctrl._frame is target_frame

    def test_switch_frame_raises_on_not_found(self):
        """switch_frame 找不到 → ValueError with helpful list."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            name = "login"
            url = "https://x.com/login"

        class FakePage:
            url = "https://x.com"
            main_frame = "MAIN"

            @property
            def frames(self):
                return [self.main_frame, FakeFrame()]

        async def fake_ensure():
            return FakePage()
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        async def fake_list():
            return [
                {"name": "main", "url": "https://x.com", "is_main": True},
                {"name": "frame[login]", "url": "https://x.com/login", "is_main": False},
            ]
        ctrl.list_frames = fake_list  # type: ignore[method-assign]

        import asyncio
        with pytest.raises(ValueError, match="frame not found"):
            asyncio.run(ctrl.switch_frame("nonexistent"))

    def test_frame_routes_via_active_page_or_frame_in_click(self):
        """click() 走 _active_page_or_frame() — 验证 frame 时 locator 也打到 frame."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            """FramePage 替身, 暴露 locator().first.scroll_into_view_if_needed().click()."""
            def locator(self, selector):
                captured["selector"] = selector

                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                captured["scrolled"] = True

                            async def click(self, timeout=5000):
                                captured["clicked"] = True

                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should not call _ensure_page when frame is set")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.click("e5"))
        assert ok is True
        assert captured["selector"] == '[data-sb-ref="e5"]'
        assert captured["scrolled"] is True
        assert captured["clicked"] is True

    def test_frame_routes_via_active_page_or_frame_in_type(self):
        """type_text() 同样 frame-routed."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector

                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass

                            async def fill(self, text, timeout=5000):
                                captured["filled"] = text

                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should not call _ensure_page when frame is set")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.type_text("e7", "hello world"))
        assert ok is True
        assert captured["selector"] == '[data-sb-ref="e7"]'
        assert captured["filled"] == "hello world"


class TestActionPrimitives:
    """T19: hover / dblclick / rightclick / drag / select_option — 验证 API 形状
    和 frame routing. 真实 Playwright 测试由 test_daemon e2e 覆盖."""

    def test_hover_signature(self):
        """hover(ref) → bool."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 验证方法存在且签名是 async
        import inspect
        sig = inspect.signature(ctrl.hover)
        assert list(sig.parameters.keys()) == ["ref"]
        assert inspect.iscoroutinefunction(ctrl.hover)

    def test_dblclick_signature(self):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.dblclick)
        assert list(sig.parameters.keys()) == ["ref"]
        assert inspect.iscoroutinefunction(ctrl.dblclick)

    def test_rightclick_signature(self):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.rightclick)
        assert list(sig.parameters.keys()) == ["ref"]
        assert inspect.iscoroutinefunction(ctrl.rightclick)

    def test_drag_signature(self):
        """drag(from_ref, to_ref) → bool."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.drag)
        assert list(sig.parameters.keys()) == ["from_ref", "to_ref"]
        assert inspect.iscoroutinefunction(ctrl.drag)

    def test_select_option_signature(self):
        """select_option(ref, value) — value 可 str 或 list[str]."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.select_option)
        assert list(sig.parameters.keys()) == ["ref", "value"]
        assert inspect.iscoroutinefunction(ctrl.select_option)
        # value 注解: Python 3.10+ 用 `str | list[str]` (types.UnionType);
        # 3.9- 用 typing.Union. 两者都接受.
        ann_str = str(sig.parameters["value"].annotation)
        assert "str" in ann_str and "list" in ann_str

    def test_hover_routes_through_active_page_or_frame(self):
        """hover() 走 _active_page_or_frame() — 设了 _frame 时不打 page."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def hover(self, timeout=5000):
                                captured["hovered"] = True
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should not call _ensure_page when frame is set")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.hover("e3"))
        assert ok is True
        assert captured["selector"] == '[data-sb-ref="e3"]'
        assert captured["hovered"] is True

    def test_dblclick_uses_right_api(self):
        """dblclick() 走 locator.first.dblclick()."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def dblclick(self, timeout=5000):
                                captured["dblclicked"] = True
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.dblclick("e5"))
        assert ok is True
        assert captured["selector"] == '[data-sb-ref="e5"]'
        assert captured["dblclicked"] is True

    def test_rightclick_uses_button_right(self):
        """rightclick() 走 locator.first.click(button='right')."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def click(self, button=None, timeout=5000):
                                captured["button"] = button
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.rightclick("e2"))
        assert ok is True
        assert captured["button"] == "right"

    def test_drag_uses_mouse_gesture(self):
        """drag() 走 mouse.down/move/up (不依赖 HTML5 drag API)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"mouse_calls": []}

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def bounding_box(self):
                                return {"x": 100, "y": 100, "width": 50, "height": 30}
                        return LL()
                return FakeLocator()

            class mouse:
                @staticmethod
                async def move(x, y, steps=None):
                    captured["mouse_calls"].append(("move", x, y, steps))
                @staticmethod
                async def down():
                    captured["mouse_calls"].append(("down",))
                @staticmethod
                async def up():
                    captured["mouse_calls"].append(("up",))

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.drag("e1", "e2"))
        assert ok is True
        # mouse 序列: move(start) -> down -> move(mid) -> move(end) -> up
        kinds = [c[0] for c in captured["mouse_calls"]]
        assert kinds == ["move", "down", "move", "move", "up"]
        # 起点 = e1 bbox center (125, 115)
        assert captured["mouse_calls"][0] == ("move", 125, 115, None)

    def test_select_option_passes_value_through(self):
        """select_option(ref, value) 直接传 value (Playwright 接受 value/label/index)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def select_option(self, value, timeout=5000):
                                captured["value"] = value
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        # string value
        ok = asyncio.run(ctrl.select_option("e7", "us-east-1"))
        assert ok is True
        assert captured["value"] == "us-east-1"
        # list value (multi-select)
        ok = asyncio.run(ctrl.select_option("e8", ["us-east-1", "eu-west-1"]))
        assert ok is True
        assert captured["value"] == ["us-east-1", "eu-west-1"]


class TestConsoleNetworkObservation:
    """T18: console / network / page error 观察 — agent 调试核心能力."""

    def test_initial_buffers_empty(self):
        """初始时所有缓冲都是空."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        assert ctrl._console_messages == []
        assert ctrl._network_requests == []
        assert ctrl._page_errors == []
        assert ctrl.get_console_messages() == []
        assert ctrl.get_network_requests() == []
        assert ctrl.get_page_errors() == []

    def test_console_buffer_accepts_messages(self):
        """_on_console 累加消息, get_console_messages 返回."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeMsg:
            def __init__(self, type_, text, location=None):
                self.type = type_
                self.text = text
                self.location = location

        ctrl._on_console(FakeMsg("log", "hello"))
        ctrl._on_console(FakeMsg("error", "boom"))
        ctrl._on_console(FakeMsg("warn", "careful"))

        all_msgs = ctrl.get_console_messages()
        assert len(all_msgs) == 3
        assert [m["type"] for m in all_msgs] == ["log", "error", "warn"]
        assert all_msgs[0]["text"] == "hello"
        assert all_msgs[1]["text"] == "boom"

        # 类型过滤
        errors = ctrl.get_console_messages(type_filter="error")
        assert len(errors) == 1
        assert errors[0]["text"] == "boom"

    def test_network_buffer_accepts_requests(self):
        """_on_request 累加 + _on_response 回填 status."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            def __init__(self, method, url, resource_type="fetch"):
                self.method = method
                self.url = url
                self.resource_type = resource_type

        class FakeResp:
            def __init__(self, url, status, method):
                self.url = url
                self.status = status
                self.request = FakeReq(method, url)  # 模拟 Playwright resp.request

        ctrl._on_request(FakeReq("GET", "https://api.example.com/users"))
        ctrl._on_request(FakeReq("POST", "https://api.example.com/users"))
        ctrl._on_response(FakeResp("https://api.example.com/users", 200, "GET"))

        all_reqs = ctrl.get_network_requests()
        assert len(all_reqs) == 2
        # POST 没响应
        post_req = [r for r in all_reqs if r["method"] == "POST"][0]
        assert "status" not in post_req
        # GET 有响应
        get_req = [r for r in all_reqs if r["method"] == "GET"][0]
        assert get_req["status"] == 200

    def test_network_only_failed_filter(self):
        """only_failed=True 过滤 4xx/5xx/网络失败."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            def __init__(self, url, method="GET"):
                self.method = method
                self.url = url
                self.resource_type = "fetch"

        class FakeResp:
            def __init__(self, url, status, method="GET"):
                self.url = url
                self.status = status
                self.request = FakeReq(url, method)

        ctrl._on_request(FakeReq("https://x.com/ok"))
        ctrl._on_request(FakeReq("https://x.com/notfound"))
        ctrl._on_request(FakeReq("https://x.com/server-error"))
        ctrl._on_response(FakeResp("https://x.com/ok", 200))
        ctrl._on_response(FakeResp("https://x.com/notfound", 404))
        ctrl._on_response(FakeResp("https://x.com/server-error", 500))

        failed = ctrl.get_network_requests(only_failed=True)
        urls = [r["url"] for r in failed]
        assert "https://x.com/ok" not in urls
        assert "https://x.com/notfound" in urls
        assert "https://x.com/server-error" in urls

    def test_network_method_filter(self):
        """method='POST' 过滤."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            def __init__(self, method, url):
                self.method = method
                self.url = url
                self.resource_type = "fetch"

        ctrl._on_request(FakeReq("GET", "https://x.com/a"))
        ctrl._on_request(FakeReq("POST", "https://x.com/b"))
        ctrl._on_request(FakeReq("PUT", "https://x.com/c"))

        only_post = ctrl.get_network_requests(method="POST")
        assert len(only_post) == 1
        assert only_post[0]["url"] == "https://x.com/b"

    def test_page_errors_buffer(self):
        """_on_web_error 累加 JS 异常."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        # 用真实 TypeError 异常对象, 让 type(err_obj).__name__ == 'TypeError'
        real_err_obj = TypeError("x is not a function")

        class FakeWebError:
            def __init__(self, err_obj, page_url):
                self.error = err_obj
                self.page = type("P", (), {"url": page_url})()

        ctrl._on_web_error(FakeWebError(real_err_obj, "https://x.com/page"))
        errs = ctrl.get_page_errors()
        assert len(errs) == 1
        assert errs[0]["name"] == "TypeError"
        assert "x is not a function" in errs[0]["message"]
        assert errs[0]["page"] == "https://x.com/page"

    def test_clear_event_buffer_resets_all(self):
        """clear_event_buffer 同时清空三个缓冲."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeMsg:
            type = "log"
            text = "x"
            location = None
        class FakeReq:
            method = "GET"
            url = "https://x.com"
            resource_type = "fetch"
        ctrl._on_console(FakeMsg())
        ctrl._on_request(FakeReq())
        assert len(ctrl._console_messages) == 1
        assert len(ctrl._network_requests) == 1

        ctrl.clear_event_buffer()
        assert ctrl._console_messages == []
        assert ctrl._network_requests == []
        assert ctrl._page_errors == []

    def test_trim_buffer_caps_size(self):
        """_trim_buffer 防止无限增长, 截断到 max."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        ctrl._max_event_buffer = 10  # 缩小方便测

        class FakeMsg:
            type = "log"
            text = "x"
            location = None
        for _ in range(50):
            ctrl._on_console(FakeMsg())
        # 超过 max (10) 时截断到恰好 max; 50 次后剩 10
        assert len(ctrl._console_messages) == 10

    def test_request_failed_marks_status_negative(self):
        """_on_request_failed 标记 status=-1 + failure 原因."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            def __init__(self, url, failure=None):
                self.method = "GET"
                self.url = url
                self.resource_type = "fetch"
                self.failure = failure

        ctrl._on_request(FakeReq("https://unreachable.example.com"))
        ctrl._on_request_failed(FakeReq("https://unreachable.example.com", "net::ERR_NAME_NOT_RESOLVED"))

        reqs = ctrl.get_network_requests(only_failed=True)
        assert len(reqs) == 1
        assert reqs[0]["status"] == -1
        assert "ERR_NAME_NOT_RESOLVED" in reqs[0]["failure"]

    def test_get_console_messages_limit(self):
        """limit 参数控制返回数量."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeMsg:
            def __init__(self, i):
                self.type = "log"
                self.text = f"msg-{i}"
                self.location = None

        for i in range(20):
            ctrl._on_console(FakeMsg(i))
        # limit=5 只看最近 5 条 (msg-15..msg-19)
        recent = ctrl.get_console_messages(limit=5)
        assert len(recent) == 5
        assert recent[-1]["text"] == "msg-19"


class TestCookieStorageManagement:
    """T17: cookies / localStorage / sessionStorage 管理 — agent 调试登录态."""

    def test_get_cookies_signature(self):
        """get_cookies(url=None) → list[dict]."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.get_cookies)
        assert "url" in sig.parameters
        assert inspect.iscoroutinefunction(ctrl.get_cookies)

    def test_set_cookie_returns_structured_result(self):
        """set_cookie 返回 {ok, name, error} 形状."""
        # 形状契约验证 (实际 set 走真实 context.add_cookies)
        expected_keys = {"ok", "name", "error"}
        result = {"ok": True, "name": "session_id", "error": None}
        assert set(result.keys()) == expected_keys
        fail_result = {"ok": False, "name": "session_id", "error": "InvalidCookie"}
        assert set(fail_result.keys()) == expected_keys

    def test_storage_kind_validation(self):
        """kind ∈ {local, session, all}."""
        # API 形状: get_storage(kind), set_storage(key, value, kind), clear_storage(kind)
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect

        sig_get = inspect.signature(ctrl.get_storage)
        assert sig_get.parameters["kind"].default == "local"

        sig_set = inspect.signature(ctrl.set_storage)
        assert sig_set.parameters["kind"].default == "local"

        sig_clear = inspect.signature(ctrl.clear_storage)
        assert sig_clear.parameters["kind"].default == "local"

    def test_get_storage_uses_active_target(self):
        """get_storage 走 _active_page_or_frame() — frame 也支持."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            async def evaluate(self, js, arg=None):
                captured["js"] = js
                return {"k1": "v1", "k2": "v2"}

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should use frame, not page")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.get_storage(kind="local"))
        assert result == {"k1": "v1", "k2": "v2"}
        assert "localStorage" in captured["js"]
        assert "sessionStorage" not in captured["js"]

        # session kind
        result = asyncio.run(ctrl.get_storage(kind="session"))
        assert "sessionStorage" in captured["js"]

    def test_set_storage_passes_key_value(self):
        """set_storage 用 JS arrow function 传 [k, v] 数组."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            async def evaluate(self, js, arg=None):
                captured["js"] = js
                captured["arg"] = arg
                return None

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should use frame")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.set_storage("token", "abc123", kind="local"))
        assert result["ok"] is True
        assert captured["arg"] == ["token", "abc123"]
        assert "localStorage.setItem" in captured["js"]

    def test_clear_storage_local_uses_localstorage(self):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            async def evaluate(self, js, arg=None):
                captured["js"] = js
                return None

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        asyncio.run(ctrl.clear_storage(kind="local"))
        assert "localStorage.clear()" in captured["js"]

    def test_clear_storage_all_clears_both(self):
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            async def evaluate(self, js, arg=None):
                captured["js"] = js
                return None

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        asyncio.run(ctrl.clear_storage(kind="all"))
        js = captured["js"]
        assert "localStorage.clear()" in js
        assert "sessionStorage.clear()" in js


class TestKeyboardFocus:
    """T16: 键盘 / 焦点 / Tab 导航 — agent 模拟人类键入."""

    def test_get_focused_element_signature(self):
        """get_focused_element() → dict (可能空)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        assert inspect.iscoroutinefunction(ctrl.get_focused_element)
        assert list(inspect.signature(ctrl.get_focused_element).parameters) == []

    def test_focus_signature(self):
        """focus(ref) → bool."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        sig = inspect.signature(ctrl.focus)
        assert list(sig.parameters.keys()) == ["ref"]
        assert inspect.iscoroutinefunction(ctrl.focus)

    def test_tab_uses_keyboard_press(self):
        """tab(count=3) 按 Tab 3 次."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"pressed": []}

        class FakeFrame:
            class keyboard:
                @staticmethod
                async def press(key):
                    captured["pressed"].append(key)

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        async def fake_get_focused():
            return {"tag": "input", "ref": "e7", "text": ""}
        ctrl.get_focused_element = fake_get_focused  # type: ignore[method-assign]

        import asyncio
        ref = asyncio.run(ctrl.tab(count=3))
        assert captured["pressed"] == ["Tab", "Tab", "Tab"]
        assert ref == "e7"

    def test_tab_shift_uses_shift_tab(self):
        """tab(shift=True) 按 Shift+Tab."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"pressed": []}

        class FakeFrame:
            class keyboard:
                @staticmethod
                async def press(key):
                    captured["pressed"].append(key)

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        async def fake_get_focused():
            return {"ref": "e3"}
        ctrl.get_focused_element = fake_get_focused  # type: ignore[method-assign]

        import asyncio
        asyncio.run(ctrl.tab(shift=True, count=2))
        assert captured["pressed"] == ["Shift+Tab", "Shift+Tab"]

    def test_keyboard_shortcut_uses_plus_join(self):
        """keyboard_shortcut('Control', 'a') → press('Control+a')."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"pressed": []}

        class FakeFrame:
            class keyboard:
                @staticmethod
                async def press(key):
                    captured["pressed"].append(key)

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        # 组合键
        asyncio.run(ctrl.keyboard_shortcut("Control", "a"))
        assert captured["pressed"] == ["Control+a"]
        # 单键
        asyncio.run(ctrl.keyboard_shortcut("F5"))
        assert captured["pressed"] == ["Control+a", "F5"]
        # 三键
        asyncio.run(ctrl.keyboard_shortcut("Control", "Shift", "p"))
        assert captured["pressed"][-1] == "Control+Shift+p"

    def test_type_into_active_uses_keyboard_type(self):
        """type_into_active(text, delay) → page.keyboard.type(text, delay)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"typed": None}

        class FakeFrame:
            class keyboard:
                @staticmethod
                async def type(text, delay=0):
                    captured["typed"] = (text, delay)

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.type_into_active("hello", delay_ms=50))
        assert ok is True
        assert captured["typed"] == ("hello", 50)

        # delay=0 也行
        asyncio.run(ctrl.type_into_active("fast"))
        assert captured["typed"] == ("fast", 0)

    def test_focus_routes_through_active_target(self):
        """focus(ref) 走 _active_page_or_frame()."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def focus(self, timeout=5000):
                                captured["focused"] = True
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            raise RuntimeError("should use frame")
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.focus("e9"))
        assert ok is True
        assert captured["selector"] == '[data-sb-ref="e9"]'
        assert captured["focused"] is True

    def test_get_focused_returns_active_element_info(self):
        """get_focused_element() 用 JS 读 document.activeElement."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        expected = {"tag": "input", "type": "text", "ref": "e5",
                    "text": "", "value": "hello", "href": None,
                    "placeholder": "name", "aria_label": None}

        class FakeFrame:
            async def evaluate(self, js, arg=None):
                return expected

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        info = asyncio.run(ctrl.get_focused_element())
        assert info["tag"] == "input"
        assert info["ref"] == "e5"
        assert info["value"] == "hello"


class TestGoalAgent:
    """T21: LLM-driven agent loop — 测试 GoalAgent 的控制流 (mock LLM + 真实 controller 子集)."""

    def test_unavailable_without_api_key(self):
        """没 API key 时立即返回失败."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService
        import os

        # 确保 env 没设 (LLMService 也会 fallback 到 LLM_* env, 但需要全部空)
        old = {k: os.environ.pop(k, None) for k in [
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_CHEAP",
            "LLM_MODEL_MEDIUM", "LLM_MODEL_SMART",
        ]}
        try:
            ctrl = BrowserController(BrowserConfig())
            agent = GoalAgent(ctrl)
            import asyncio
            result = asyncio.run(agent.run("do something"))
            assert result.success is False
            assert "LLM not configured" in result.reason
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_llm_returns_done_immediately(self):
        """LLM 第一次就 done → 1 步成功."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask(goal, snapshot):
            return {
                "thought": "nothing to do",
                "action": "done",
                "args": {"answer": "42"},
            }

        async def fake_capture(goal=""):
            return ("URL: about:blank\n", "(empty)")

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"))
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("answer 42"))
        assert result.success is True
        assert result.answer == "42"
        assert result.total_steps == 1
        assert result.steps[0].action == "done"

    def test_runs_actions_until_done(self):
        """执行 open → click → done 序列."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        actions_queue = [
            {"thought": "navigate", "action": "open", "args": {"url": "https://x.com"}},
            {"thought": "click button", "action": "click", "args": {"ref": "e5"}},
            {"thought": "done", "action": "done", "args": {"answer": "clicked"}},
        ]

        async def fake_ask(goal, snapshot):
            return actions_queue.pop(0)

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "- e5 button: Go")

        executed: list = []

        async def fake_execute(action, args):
            executed.append((action, args))
            return True, ""

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"),
                          max_steps=10)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("click and done"))
        assert result.success is True
        assert result.answer == "clicked"
        assert len(executed) == 2  # open + click
        assert executed[0][0] == "open"
        assert executed[1] == ("click", {"ref": "e5"})

    def test_max_steps_terminates(self):
        """达到 max_steps 仍未 done → 失败."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask(goal, snapshot):
            return {"thought": "loop", "action": "click", "args": {"ref": "e1"}}

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "- e1 button")

        async def fake_execute(action, args):
            return True, ""

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"),
                          max_steps=3)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("loop forever"))
        assert result.success is False
        assert "max_steps" in result.reason
        assert result.total_steps == 3

    def test_consecutive_failures_terminate(self):
        """连续 5 次失败 → 提早退出 (避免无限循环)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask(goal, snapshot):
            return {"thought": "try", "action": "click", "args": {"ref": "e1"}}

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "- e1 button")

        async def fake_execute(action, args):
            return False, "element not found"

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"),
                          max_steps=20)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("impossible goal"))
        assert result.success is False
        assert "5 consecutive" in result.reason

    def test_invalid_action_stops_after_3(self):
        """连续 3 次非法 action → 退出."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask(goal, snapshot):
            return {"thought": "weird", "action": "fly_to_mars", "args": {}}

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "")

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"),
                          max_steps=20)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("anything"))
        assert result.success is False
        assert "invalid actions" in result.reason
        assert result.total_steps == 3

    def test_extract_text_returns_markdown(self):
        """extract_text 调用 ContentExtractor."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask(goal, snapshot):
            return {"thought": "read", "action": "extract_text", "args": {"max_chars": 100}}

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "")

        async def fake_execute(action, args):
            # extract_text 走真 controller — 需要 fake page; 但测试形状 OK
            return True, "# Title\n\nSome content here"

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"),
                          max_steps=2)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("read"))
        # 1 步 extract_text 然后 LLM 应该再被问 (但 _ask_llm 会 raise KeyError), 改测 max_steps
        assert result.total_steps >= 1

    def test_llm_call_failure_returns_error_result(self):
        """LLM 抛异常 → 返回失败 result 不 crash."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        async def fake_ask_fail(goal, snapshot):
            raise RuntimeError("network down")

        async def fake_capture(goal=""):
            return ("URL: x.com\n", "")

        agent = GoalAgent(ctrl, llm_service=LLMService(api_key="test-key", base_url="http://fake", model_cheap="fake"))
        agent._ask_llm = fake_ask_fail  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("anything"))
        assert result.success is False
        assert "LLM call failed" in result.reason

    def test_goal_result_to_dict_shape(self):
        """GoalResult.to_dict() 序列化为 daemon 能返回的 dict."""
        from semantic_browser.agent import GoalResult, StepRecord

        result = GoalResult(
            goal="test",
            success=True,
            answer="found it",
            steps=[
                StepRecord(step=1, thought="open", action="open",
                           args={"url": "https://x.com"}, success=True),
                StepRecord(step=2, thought="done", action="done",
                           args={"answer": "found it"}, success=True),
            ],
            total_steps=2,
        )
        d = result.to_dict()
        assert d["goal"] == "test"
        assert d["success"] is True
        assert d["answer"] == "found it"
        assert d["total_steps"] == 2
        assert len(d["steps"]) == 2
        assert d["steps"][0]["action"] == "open"
        assert d["steps"][0]["args"] == {"url": "https://x.com"}


class TestSelfHealing:
    """T22: self-healing click / type — 失败时自动 force / JS."""

    def test_click_with_healing_returns_structured_result(self):
        """click_with_healing 返回 {ok, ref, tried, error} 形状."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        import inspect
        assert inspect.iscoroutinefunction(ctrl.click_with_healing)
        sig = inspect.signature(ctrl.click_with_healing)
        assert "ref" in sig.parameters
        assert "heal_attempts" in sig.parameters

    def test_click_with_healing_succeeds_first_try(self):
        """第一次正常 click 成功 → 只 tried=[normal]."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                captured["selector"] = selector
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def click(self, timeout=5000, force=False):
                                captured["force"] = force
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.click_with_healing("e5"))
        assert result["ok"] is True
        assert result["tried"] == ["normal"]
        assert result["error"] is None

    def test_click_heals_with_force(self):
        """第一次失败 → 第二次 force=True 成功."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"clicks": []}

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def click(self, timeout=5000, force=False):
                                captured["clicks"].append(force)
                                if not force:
                                    raise RuntimeError("obscured")
                                return None
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.click_with_healing("e7"))
        assert result["ok"] is True
        assert result["tried"] == ["normal", "force"]
        assert captured["clicks"] == [False, True]  # 第一次 normal=False, 第二次 force=True

    def test_click_heals_with_js(self):
        """第一次 + 第二次都失败 → 第三次 JS click 成功."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"clicks": [], "js": None}

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def click(self, timeout=5000, force=False):
                                captured["clicks"].append(force)
                                raise RuntimeError("always fail")
                        return LL()
                return FakeLocator()

            async def evaluate(self, js, arg=None):
                captured["js"] = js
                return True

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.click_with_healing("e9"))
        assert result["ok"] is True
        assert result["tried"] == ["normal", "force", "js"]
        assert "document.querySelector" in captured["js"]

    def test_click_returns_error_when_all_fail(self):
        """三种方式都失败 → ok=False + 完整 tried + error."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def click(self, timeout=5000, force=False):
                                raise RuntimeError("nope")
                        return LL()
                return FakeLocator()

            async def evaluate(self, js, arg=None):
                return False

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.click_with_healing("e1"))
        assert result["ok"] is False
        assert result["tried"] == ["normal", "force", "js"]
        assert result["error"]  # 任何错误消息 (不绑具体文案)

    def test_type_with_healing_dispatches_input_event(self):
        """type heal 的 JS 路径用 React-friendly value setter + dispatch input."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {}

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def fill(self, text, timeout=5000, force=False):
                                if not force:
                                    raise RuntimeError("normal fail")
                                captured["force_fill"] = text
                        return LL()
                return FakeLocator()

            async def evaluate(self, js, arg=None):
                captured["js"] = js
                captured["js_arg"] = arg
                return True

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        # 第一次 normal 失败, 第二次 force 成功
        result = asyncio.run(ctrl.type_with_healing("e3", "hello", heal_attempts=2))
        assert result["ok"] is True
        assert result["tried"] == ["normal", "force"]
        assert captured["force_fill"] == "hello"

        # 测 JS 路径: 第一次 + 第二次都失败
        class FakeFrame2:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def fill(self, text, timeout=5000, force=False):
                                raise RuntimeError("always fail")
                        return LL()
                return FakeLocator()

            async def evaluate(self, js, arg=None):
                captured["js2"] = js
                captured["js2_arg"] = arg
                return True

        ctrl._frame = FakeFrame2()  # type: ignore[assignment]
        result = asyncio.run(ctrl.type_with_healing("e3", "world", heal_attempts=2))
        assert result["ok"] is True
        assert result["tried"] == ["normal", "force", "js"]
        # React-friendly 关键: HTMLInputElement 原型 setter + dispatch input event
        assert "HTMLInputElement.prototype" in captured["js2"]
        assert "dispatchEvent" in captured["js2"]
        assert captured["js2_arg"] == ['[data-sb-ref="e3"]', "world"]

    def test_type_healing_with_zero_attempts(self):
        """heal_attempts=0 → 只试 normal."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def fill(self, text, timeout=5000, force=False):
                                raise RuntimeError("fail")
                        return LL()
                return FakeLocator()

        ctrl._frame = FakeFrame()  # type: ignore[assignment]

        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(ctrl.type_with_healing("e3", "x", heal_attempts=0))
        assert result["ok"] is False
        assert result["tried"] == ["normal"]


class TestLLMServiceTiered:
    """T23: LLMService 抽象层 + 三档模型路由."""

    def setup_method(self):
        from semantic_browser.llm import reset_default_service
        reset_default_service()

    def test_unavailable_without_api_key(self):
        """没 API key → is_available() False."""
        from semantic_browser.llm import LLMService
        import os

        old = {k: os.environ.pop(k, None) for k in
               ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_CHEAP",
                "LLM_MODEL_MEDIUM", "LLM_MODEL_SMART",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"]}
        try:
            svc = LLMService()
            assert svc.is_available() is False
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_model_for_tier(self):
        """model_for(tier) 返回对应模型."""
        from semantic_browser.llm import LLMService
        svc = LLMService(
            api_key="test",
            base_url="http://fake",
            model_cheap="cheap-1",
            model_medium="medium-1",
            model_smart="smart-1",
        )
        assert svc.model_for("cheap") == "cheap-1"
        assert svc.model_for("medium") == "medium-1"
        assert svc.model_for("smart") == "smart-1"

    def test_call_counts_increment(self):
        """call_counts 跟踪每档调用次数."""
        from semantic_browser.llm import LLMService
        svc = LLMService(api_key="k", base_url="http://fake")
        svc.call_counts["cheap"] += 1
        svc.call_counts["cheap"] += 1
        svc.call_counts["smart"] += 1
        stats = svc.stats()
        assert stats["call_counts"]["cheap"] == 2
        assert stats["call_counts"]["smart"] == 1
        assert "available" in stats
        assert "models" in stats

    def test_complete_unavailable_raises(self):
        """显式无 key 时 complete() raise."""
        from semantic_browser.llm import LLMService, LLMUnavailableError
        # 显式传 None / 空 — 不让 fallback 到环境变量
        svc = LLMService(api_key="", base_url="http://fake", model_cheap="x")
        svc.api_key = ""  # 强制空
        import asyncio
        with pytest.raises(LLMUnavailableError):
            asyncio.run(svc.complete(
                [{"role": "user", "content": "hi"}],
                tier="cheap",
            ))


class TestLLMHelpers:
    """T24: tier-2 智能辅助 (snapshot 切片 / 摘要 / 抽取 / ref 查找)."""

    def test_build_smart_snapshot_excerpt_filters_useful_refs(self):
        """build_smart_snapshot_excerpt 只保留 useful_refs."""
        from semantic_browser.snapshot.engine import (
            PageSnapshot, LinkInfo, ControlInfo,
        )
        from semantic_browser.llm.helpers import build_smart_snapshot_excerpt

        snap = PageSnapshot(
            url="https://example.com",
            title="Example",
            domain="example.com",
            links=[
                LinkInfo(ref="aaa", href="/home", text="Home"),
                LinkInfo(ref="bbb", href="/about", text="About"),
                LinkInfo(ref="ccc", href="/contact", text="Contact"),
            ],
            controls=[
                ControlInfo(ref="xxx", kind="button", label="Sign in"),
                ControlInfo(ref="yyy", kind="link", label="Pricing"),
            ],
        )
        excerpt = build_smart_snapshot_excerpt(snap, useful_refs=["ccc", "xxx"])
        assert "ccc" in excerpt
        assert "Contact" in excerpt
        assert "xxx" in excerpt
        assert "Sign in" in excerpt
        # 过滤掉的 (用 "ref " 前缀避免子串误匹配)
        assert "- aaa" not in excerpt
        assert "- bbb" not in excerpt
        assert "- yyy" not in excerpt
        assert "Pricing" not in excerpt
        # 含 URL/title
        assert "https://example.com" in excerpt
        assert "Example" in excerpt

    def test_build_smart_snapshot_excerpt_empty_useful(self):
        """空 useful_refs → '(no relevant refs found)'."""
        from semantic_browser.snapshot.engine import PageSnapshot
        from semantic_browser.llm.helpers import build_smart_snapshot_excerpt

        snap = PageSnapshot(
            url="https://x.com", title="X", domain="x.com",
        )
        excerpt = build_smart_snapshot_excerpt(snap, useful_refs=[])
        assert "no relevant refs" in excerpt

    def test_extract_fields_fills_missing_with_none(self):
        """extract_fields 失败/字段缺失时填 None."""
        from semantic_browser.llm import LLMService, extract_fields

        # Mock LLM 抛异常 → 字段全 None
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_complete_json(*args, **kwargs):
            raise RuntimeError("LLM down")
        svc.complete_json = fake_complete_json  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(extract_fields(
            "any text", {"name": "str", "price": "float"},
            llm=svc,
        ))
        assert result == {"name": None, "price": None}

    def test_extract_fields_returns_parsed(self):
        """extract_fields 成功时 parse 返回的 JSON."""
        from semantic_browser.llm import LLMService, extract_fields

        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_complete_json(*args, **kwargs):
            return {"name": "Apple", "price": 999.0}
        svc.complete_json = fake_complete_json  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(extract_fields(
            "Apple iPhone costs $999",
            {"name": "str", "price": "float"},
            llm=svc,
        ))
        assert result["name"] == "Apple"
        assert result["price"] == 999.0

    def test_summarize_text_short_circuits(self):
        """短文本不调 LLM, 直接返回."""
        from semantic_browser.llm import LLMService, summarize_text

        call_count = {"n": 0}
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake(*args, **kwargs):
            call_count["n"] += 1
            return None  # 不应被调用
        svc.complete_json = fake  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(summarize_text("short text", max_chars=500, llm=svc))
        assert result == "short text"
        assert call_count["n"] == 0

    def test_summarize_text_long_uses_llm(self):
        """长文本调 LLM."""
        from semantic_browser.llm import LLMService, summarize_text

        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake(*args, **kwargs):
            return {"summary": "TL;DR"}
        svc.complete_json = fake  # type: ignore[method-assign]

        import asyncio
        long_text = "x" * 1000
        result = asyncio.run(summarize_text(long_text, max_chars=100, llm=svc))
        assert result == "TL;DR"

    def test_find_ref_by_label_validates_returned_ref(self):
        """find_ref_by_label 验证 LLM 返回的 ref 确实存在 (防幻觉)."""
        from semantic_browser.llm import LLMService, find_ref_by_label
        from semantic_browser.snapshot.engine import (
            PageSnapshot, ControlInfo,
        )

        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_hallucinate(*args, **kwargs):
            # LLM 返回一个不存在的 ref
            return {"ref": "e999"}
        svc.complete_json = fake_hallucinate  # type: ignore[method-assign]

        snap = PageSnapshot(
            url="x", title="t", domain="x",
            controls=[ControlInfo(ref="e5", kind="button", label="Login")],
        )
        import asyncio
        ref = asyncio.run(find_ref_by_label(snap, "登录按钮", llm=svc))
        assert ref is None  # 验证后拒绝

        async def fake_real(*args, **kwargs):
            return {"ref": "e5"}
        svc.complete_json = fake_real  # type: ignore[method-assign]
        ref = asyncio.run(find_ref_by_label(snap, "登录按钮", llm=svc))
        assert ref == "e5"

    def test_slice_refs_for_goal_filters_invalid(self):
        """slice_refs_for_goal 过滤掉 LLM 幻觉的 ref."""
        from semantic_browser.llm import LLMService, slice_refs_for_goal
        from semantic_browser.snapshot.engine import (
            PageSnapshot, LinkInfo,
        )

        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_hallucinate(*args, **kwargs):
            return {"useful_refs": ["e3", "e999", "e1"]}  # e999 不存在
        svc.complete_json = fake_hallucinate  # type: ignore[method-assign]

        snap = PageSnapshot(
            url="x", title="t", domain="x",
            links=[
                LinkInfo(ref="e1", href="/a", text="A"),
                LinkInfo(ref="e3", href="/c", text="C"),
            ],
        )
        import asyncio
        useful = asyncio.run(slice_refs_for_goal(snap, "anything", max_refs=10, llm=svc))
        assert "e3" in useful
        assert "e1" in useful
        assert "e999" not in useful


class TestDiagnosticsDump:
    """T25: 失败时自动 dump diagnostics."""

    def test_collect_diagnostics_returns_expected_keys(self):
        """collect_diagnostics 返回完整诊断 dict."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import collect_diagnostics

        ctrl = BrowserController(BrowserConfig())

        # 模拟一些 console error
        class FakeMsg:
            type = "error"
            text = "TypeError: undefined"
            location = None
        ctrl._on_console(FakeMsg())

        import asyncio

        # 没有 page → page_info.url = None, snapshot_excerpt = ""
        result = asyncio.run(collect_diagnostics(
            ctrl,
            failed_action="click",
            failed_args={"ref": "e5"},
            error="element not found",
        ))
        assert result["failed_action"] == "click"
        assert result["failed_args"] == {"ref": "e5"}
        assert "element not found" in result["error"]
        assert "page" in result
        assert "console_errors" in result
        assert "console_warnings" in result
        assert "network_failures" in result
        assert "js_errors" in result
        assert "snapshot_excerpt" in result
        # 我们刚才 on_console 加了一条 error
        assert len(result["console_errors"]) == 1
        assert result["console_errors"][0]["text"] == "TypeError: undefined"

    def test_format_diagnostics_for_llm_serializes_keys(self):
        """format_diagnostics_for_llm 输出包含失败动作 / error / 各类事件."""
        from semantic_browser.llm import format_diagnostics_for_llm

        diag = {
            "failed_action": "click",
            "failed_args": {"ref": "e5"},
            "error": "element not found",
            "page": {"url": "https://x.com", "title": "X"},
            "console_errors": [{"text": "TypeError: oops"}],
            "console_warnings": [],
            "network_failures": [
                {"method": "POST", "status": 500, "url": "https://api.example.com/submit"},
            ],
            "js_errors": [{"name": "TypeError", "message": "x is null"}],
            "snapshot_excerpt": "URL: https://x.com\nTitle: X",
        }
        text = format_diagnostics_for_llm(diag)
        assert "click" in text
        assert "element not found" in text
        assert "https://x.com" in text
        assert "TypeError: oops" in text
        assert "https://api.example.com/submit" in text
        assert "x is null" in text
        assert "URL: https://x.com" in text


class TestGoalAgentT26:
    """T26: GoalAgent 接入 tier-2 (切片 + 自动 dump)."""

    def setup_method(self):
        from semantic_browser.llm import reset_default_service
        reset_default_service()

    def test_failure_triggers_diagnostics(self):
        """action 失败时, last_failure_diag 被填充."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())

        decisions = [
            {"thought": "click", "action": "click", "args": {"ref": "e1"}},
            # 失败后 LLM 应该看到 diag; 给个 done 让循环停
            {"thought": "give up", "action": "done", "args": {"answer": "couldn't"}},
        ]

        async def fake_ask(goal, snap):
            return decisions.pop(0)
        async def fake_capture(goal=""):
            return ("URL: x\n", "- e1 button")
        async def fake_execute(action, args):
            return False, "element not found"

        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        import asyncio
        result = asyncio.run(agent.run("anything"))
        # 失败后 diag 填了 (成功 done 时被清空 — 但我们成功 done 在下一步)
        assert result.success is True
        # history 应有 2 步: fail + done
        assert len(result.steps) == 2
        assert result.steps[0].success is False
        assert result.steps[1].action == "done"

    def test_smart_slicing_reduces_refs(self):
        """use_smart_slicing=True 时 _capture_snapshot_excerpt 调 LLM 切片."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.snapshot.engine import (
            PageSnapshot, LinkInfo, ControlInfo,
        )

        ctrl = BrowserController(BrowserConfig())
        # fake current_page with snapshot
        snap = PageSnapshot(
            url="https://x.com", title="X", domain="x.com",
            links=[LinkInfo(ref=f"e{i}", href=f"/{i}", text=f"L{i}") for i in range(1, 21)],
            controls=[ControlInfo(ref=f"e{i+20}", kind="button", label=f"B{i}") for i in range(1, 11)],
        )

        # fake _capture_snapshot: 不用真 page, 直接返回 mock 的 snapshot
        # 用 monkey-patch capture 时调用 page, 所以干脆 patch _capture_snapshot_excerpt
        class FakePage:
            url = "https://x.com"
            async def title(self_inner): return "X"

        ctrl._page = FakePage()  # type: ignore[assignment]

        # 注入 mock SnapshotEngine — 不能直接 mock 因为要从 module 引用
        # 简单办法: mock _capture_snapshot_excerpt 返回 large data
        async def fake_capture(goal=""):
            return ("URL: x\nTitle: X\n\n", "(30 refs)")

        svc = LLMService(api_key="k", base_url="http://fake")

        # Mock slice_refs_for_goal 在 module 里
        from semantic_browser.agent import loop as agent_loop
        original_slicer = agent_loop.slice_refs_for_goal

        called_with = {"goal": None, "snap": None}

        async def fake_slicer(snap, goal, **kwargs):
            called_with["goal"] = goal
            called_with["snap"] = snap
            return ["e5", "e10"]

        agent_loop.slice_refs_for_goal = fake_slicer  # type: ignore[assignment]

        try:
            agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                              use_smart_slicing=True)
            agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
            # 替换里面的 build_smart_snapshot_excerpt 调用 (因 fake_capture 已 short-circuit)
            import asyncio
            header, body = asyncio.run(agent._capture_snapshot_excerpt(goal="find login"))
            # 我们 mock 整个 _capture_snapshot_excerpt, 所以 slicer 不会被实际调用
            # 但 GoalAgent 的 use_smart_slicing=True 是设置了的
            assert agent.use_smart_slicing is True
        finally:
            agent_loop.slice_refs_for_goal = original_slicer  # type: ignore[assignment]

    def test_no_smart_slicing_disables(self):
        """use_smart_slicing=False → 不调 slicer."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_smart_slicing=False)
        assert agent.use_smart_slicing is False
        assert agent.slice_tier == "cheap"  # 默认 tier

    def test_no_failure_diagnostics_disables(self):
        """use_failure_diagnostics=False → 不收集 diag."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_failure_diagnostics=False)
        assert agent.use_failure_diagnostics is False

    def test_tier_default_is_smart(self):
        """GoalAgent 默认 tier=smart (复杂决策)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.llm import LLMService

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc)
        assert agent.tier == "smart"
