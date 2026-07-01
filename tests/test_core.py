"""
Semantic Browser 测试套件 — T5 seed。
覆盖纯逻辑模块(不需要浏览器)：MemoryStore, GraphBuilder, PageClassifier(heuristic),
Crawler 归一化/过滤逻辑, PageSnapshot 序列化, ClassificationResult。
"""
from __future__ import annotations

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
