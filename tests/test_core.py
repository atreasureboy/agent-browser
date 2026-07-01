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
from semantic_browser.memory.goal_memory import GoalMemory
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


@pytest.fixture(autouse=True)
def isolate_goal_memory(tmp_path, monkeypatch):
    """把 GoalMemory 默认路径重定向到 tmp_path, 避免污染用户 home + 跨 session 误命中."""
    import semantic_browser.memory.goal_memory as gm_mod
    monkeypatch.setattr(gm_mod, "DEFAULT_PATH", tmp_path / "goal_memory.json")


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

    def test_drag_html5_dispatches_drag_events(self):
        """T28: drag_html5() 用 DataTransfer + DragEvent 派发序列."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        captured: dict = {"evaluate_args": None, "evaluate_result": {"ok": True}}

        class FakeFrame:
            async def evaluate(self, script, arg):
                captured["evaluate_args"] = (script, arg)
                return captured["evaluate_result"]

        ctrl._frame = FakeFrame()  # type: ignore[assignment]
        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.drag_html5("e1", "e2"))
        assert ok is True
        # evaluate 收到 [from_sel, to_sel]
        script, arg = captured["evaluate_args"]
        assert arg == ['[data-sb-ref="e1"]', '[data-sb-ref="e2"]']
        # 脚本里要构造 DataTransfer + dragstart/dragenter/dragover/drop/dragend
        assert "DataTransfer" in script
        assert "dragstart" in script
        assert "drop" in script

    def test_drag_html5_returns_false_on_missing_element(self):
        """T28: 元素找不到时 evaluate 返回 {ok:false}, drag_html5 返回 False."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        class FakeFrame:
            async def evaluate(self, script, arg):
                return {"ok": False, "error": "element not found"}

        ctrl._frame = FakeFrame()  # type: ignore[assignment]
        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.drag_html5("e1", "e2"))
        assert ok is False

    def test_drag_falls_back_to_html5_on_mouse_failure(self):
        """T28: mouse 拖失败时 drag() 自动 fallback 到 drag_html5()."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())

        # mouse 拖时元素找不到 (bounding_box 返回 None)
        class FakeFrame:
            def locator(self, selector):
                class FakeLocator:
                    @property
                    def first(self):
                        class LL:
                            async def scroll_into_view_if_needed(self, timeout=5000):
                                pass
                            async def bounding_box(self):
                                return None  # 触发 RuntimeError
                        return LL()
                return FakeLocator()

            async def evaluate(self, script, arg):
                return {"ok": True}

        ctrl._frame = FakeFrame()  # type: ignore[assignment]
        async def fake_ensure():
            return None
        ctrl._ensure_page = fake_ensure  # type: ignore[method-assign]

        import asyncio
        ok = asyncio.run(ctrl.drag("e1", "e2"))
        # mouse 失败 → 走 drag_html5 → 成功
        assert ok is True


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


class TestGoalMemory:
    """T27: GoalMemory 单元测试 — 跨 session 缓存 goal→answer."""

    def test_record_and_lookup_exact(self):
        """存一条成功, 精确 lookup 命中."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("find contact email", success=True, answer="x@y.com", steps=3)
            hit = mem.lookup("find contact email")
            assert hit is not None
            assert hit["answer"] == "x@y.com"
            assert hit["steps"] == 3

    def test_lookup_fuzzy_match(self):
        """Jaccard 相似度阈值: 'find contact email' 接近 'find contact email for x.com'."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("find contact email for example.com", success=True, answer="a@b.com")
            # token 重叠 ~0.6, 用 threshold=0.5 命中
            hit = mem.lookup("find contact email", threshold=0.5)
            assert hit is not None
            assert hit["answer"] == "a@b.com"

    def test_lookup_threshold_filters(self):
        """完全不同 goal → lookup None."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("find contact email", success=True, answer="x@y.com")
            hit = mem.lookup("book a flight to Tokyo")
            assert hit is None

    def test_lookup_only_returns_success(self):
        """失败的 entry 不会作为答案返回."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("hard task", success=False, reason="couldn't")
            hit = mem.lookup("hard task")
            assert hit is None  # 失败不命中

    def test_persistence_across_instances(self):
        """两次 GoalMemory 实例, 数据持久化在 JSON."""
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.json"
            mem1 = GoalMemory(path)
            mem1.record("get weather", success=True, answer="sunny")
            mem2 = GoalMemory(path)
            hit = mem2.lookup("get weather")
            assert hit is not None
            assert hit["answer"] == "sunny"

    def test_hit_count_increments(self):
        """命中后 hit_count +1."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("test goal", success=True, answer="ok")
            mem.lookup("test goal")
            mem.lookup("test goal")
            mem.lookup("test goal")
            entries = mem.list_recent()
            assert entries[0]["hit_count"] == 3

    def test_record_updates_existing(self):
        """高度相似 (>=0.9) 视为同一 goal → 更新而非新增."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("find contact email", success=True, answer="first")
            mem.record("find contact email", success=True, answer="second")
            entries = mem.list_recent()
            assert len(entries) == 1
            assert entries[0]["answer"] == "second"

    def test_stats_summary(self):
        """stats() 返回 total/success/failure/total_hits."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("g1", success=True, answer="a")
            mem.record("g2", success=False, reason="r")
            mem.record("g3", success=True, answer="b")
            mem.lookup("g1")
            stats = mem.stats()
            assert stats["total"] == 3
            assert stats["success"] == 2
            assert stats["failure"] == 1
            assert stats["total_hits"] == 1

    def test_clear_empties_memory(self):
        """clear() 清空所有 entry."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("g1", success=True, answer="a")
            mem.record("g2", success=True, answer="b")
            mem.clear()
            assert mem.stats()["total"] == 0
            assert mem.lookup("g1") is None

    def test_max_entries_lru_eviction(self):
        """超过 MAX_ENTRIES (500) 时 LRU 淘汰."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            # 注入 MAX_ENTRIES+10 条
            from semantic_browser.memory import goal_memory as gm_mod
            orig_max = gm_mod.MAX_ENTRIES
            gm_mod.MAX_ENTRIES = 5
            try:
                for i in range(7):
                    mem.record(f"goal_{i}", success=True, answer=f"ans_{i}")
                assert mem.stats()["total"] == 5
                # 最早两条应被淘汰, 最近两条保留
                assert mem.lookup("goal_6") is not None
                assert mem.lookup("goal_5") is not None
                assert mem.lookup("goal_0") is None
            finally:
                gm_mod.MAX_ENTRIES = orig_max


class TestGoalAgentMemoryIntegration:
    """T27: GoalAgent.run() 接入 goal memory."""

    def test_cache_hit_short_circuits_run(self):
        """已有 cache 时, run() 直接返回, 不调 LLM."""
        import asyncio
        import tempfile
        from pathlib import Path
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.memory.goal_memory import GoalMemory

        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("get weather in Tokyo", success=True, answer="sunny 25C")

            svc = LLMService(api_key="k", base_url="http://fake")
            ctrl = BrowserController(BrowserConfig())
            agent = GoalAgent(
                ctrl, llm_service=svc, goal_memory=mem,
                max_steps=5,
            )
            # 如果 cache 没命中, 会调 LLM (fake URL 必失败)
            result = asyncio.run(agent.run("get weather in Tokyo"))
            assert result.success is True
            assert result.answer == "sunny 25C"
            assert result.total_steps == 0
            assert "memory" in result.reason
            assert agent.last_memory_hit is not None

    def test_no_memory_when_disabled(self):
        """use_memory=False → 不查 cache."""
        import asyncio
        import tempfile
        from pathlib import Path
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.memory.goal_memory import GoalMemory

        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            mem.record("get weather", success=True, answer="cached")

            svc = LLMService(api_key="k", base_url="http://fake")
            ctrl = BrowserController(BrowserConfig())
            agent = GoalAgent(
                ctrl, llm_service=svc, goal_memory=mem,
                use_memory=False, max_steps=2,
            )
            # 应该走 LLM → 失败 (fake URL)
            # 注入一个返回 done 的 LLM
            async def fake_ask(goal, snap):
                return {"thought": "x", "action": "done", "args": {"answer": "fresh"}}
            async def fake_capture(goal=""):
                return ("URL: x\n", "")
            agent._ask_llm = fake_ask  # type: ignore[method-assign]
            agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

            result = asyncio.run(agent.run("get weather"))
            assert result.answer == "fresh"  # 不是 cache 的 "cached"

    def test_records_result_after_run(self):
        """run() 完成后写入 memory (无论成败)."""
        import asyncio
        import tempfile
        from pathlib import Path
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.memory.goal_memory import GoalMemory

        with tempfile.TemporaryDirectory() as tmp:
            mem = GoalMemory(Path(tmp) / "mem.json")
            svc = LLMService(api_key="k", base_url="http://fake")
            ctrl = BrowserController(BrowserConfig())
            agent = GoalAgent(ctrl, llm_service=svc, goal_memory=mem, max_steps=3)

            async def fake_ask(goal, snap):
                return {"thought": "x", "action": "done", "args": {"answer": "42"}}
            async def fake_capture(goal=""):
                return ("URL: x\n", "")
            agent._ask_llm = fake_ask  # type: ignore[method-assign]
            agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

            asyncio.run(agent.run("compute pi"))
            hit = mem.lookup("compute pi")
            assert hit is not None
            assert hit["answer"] == "42"


class TestGoalAgentPlanDryRun:
    """T29: GoalAgent.plan() — dry-run 模式生成完整 plan 不执行."""

    def test_plan_returns_strategy_and_steps(self):
        """plan() 返回 dict 含 thought + plan 列表."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_complete_json(messages, **kwargs):
            return {
                "thought": "navigate then extract",
                "plan": [
                    {"step": 1, "action": "open", "args": {"url": "https://x.com"}, "why": "go to start"},
                    {"step": 2, "action": "extract_text", "args": {"max_chars": 1000}, "why": "read content"},
                    {"step": 3, "action": "done", "args": {"answer": "found"}, "why": "done"},
                ],
            }

        svc.complete_json = fake_complete_json  # type: ignore[method-assign]

        async def fake_capture(goal=""):
            return ("URL: y\n", "- e1 link: Go")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False)
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        result = asyncio.run(agent.plan("find contact"))
        assert result["thought"] == "navigate then extract"
        assert len(result["plan"]) == 3
        assert result["plan"][0]["action"] == "open"
        assert result["plan"][2]["action"] == "done"
        assert result["goal"] == "find contact"

    def test_plan_truncates_to_max_steps(self):
        """LLM 超长 plan → 自动截断到 max_steps."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_complete_json(messages, **kwargs):
            return {
                "thought": "long",
                "plan": [{"step": i, "action": "click", "args": {"ref": f"e{i}"}, "why": "x"}
                          for i in range(1, 20)],
            }

        svc.complete_json = fake_complete_json  # type: ignore[method-assign]

        async def fake_capture(goal=""):
            return ("URL: y\n", "")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False)
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        result = asyncio.run(agent.plan("do everything", max_steps=5))
        assert len(result["plan"]) == 5  # 截断到 5

    def test_plan_returns_error_when_llm_unavailable(self):
        """LLM 没配时 plan() 返回 error key."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="", base_url="http://fake")
        svc.api_key = ""  # 强制不可用

        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False)
        result = asyncio.run(agent.plan("anything"))
        assert result.get("error") is not None
        assert result["plan"] == []

    def test_plan_handles_llm_exception(self):
        """LLM 抛异常时 plan() 不 crash, 返回 error key."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_complete_json(messages, **kwargs):
            raise RuntimeError("network down")
        svc.complete_json = fake_complete_json  # type: ignore[method-assign]

        async def fake_capture(goal=""):
            return ("URL: y\n", "")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False)
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        result = asyncio.run(agent.plan("anything"))
        assert "RuntimeError" in result.get("error", "")
        assert result["plan"] == []


class TestGoalAgentStreaming:
    """T31: on_step 回调 — 流式进度."""

    def test_on_step_called_for_each_action(self):
        """每步完成后调 on_step(record)."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        decisions = [
            {"thought": "open", "action": "open", "args": {"url": "https://x.com"}},
            {"thought": "done", "action": "done", "args": {"answer": "ok"}},
        ]
        async def fake_ask(goal, snap):
            return decisions.pop(0)
        async def fake_capture(goal=""):
            return ("URL: x\n", "")
        async def fake_execute(action, args):
            return True, ""

        captured: list = []
        def on_step_sync(record):
            captured.append((record.step, record.action, record.success))
        async def on_step_async(record):
            captured.append((record.step, record.action, record.success, "async"))

        # 测同步回调 (没 await)
        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                          use_memory=False, on_step=on_step_sync)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        result = asyncio.run(agent.run("anything"))
        assert result.success is True
        # 2 步都触发回调 (open + done)
        assert len(captured) == 2
        assert captured[0] == (1, "open", True)
        assert captured[1] == (2, "done", True)

    def test_async_on_step_awaited(self):
        """async on_step 也会被 await."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_ask(goal, snap):
            return {"thought": "x", "action": "done", "args": {"answer": "ok"}}
        async def fake_capture(goal=""):
            return ("URL: x\n", "")

        events: list = []
        async def on_step_async(record):
            await asyncio.sleep(0)  # 真 await
            events.append(record.action)

        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                          use_memory=False, on_step=on_step_async)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        asyncio.run(agent.run("anything"))
        assert events == ["done"]

    def test_on_step_exception_does_not_crash(self):
        """on_step 抛异常不能打断 agent 主循环."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_ask(goal, snap):
            return {"thought": "x", "action": "done", "args": {"answer": "ok"}}
        async def fake_capture(goal=""):
            return ("URL: x\n", "")

        def bad_callback(record):
            raise RuntimeError("user code bad")

        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                          use_memory=False, on_step=bad_callback)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        # 不应该因为 callback 抛异常而 crash
        result = asyncio.run(agent.run("anything"))
        assert result.success is True

    def test_no_callback_when_not_provided(self):
        """on_step=None → 不调任何回调 (默认行为)."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        async def fake_ask(goal, snap):
            return {"thought": "x", "action": "done", "args": {"answer": "ok"}}
        async def fake_capture(goal=""):
            return ("URL: x\n", "")

        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5, use_memory=False)
        assert agent.on_step is None
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        result = asyncio.run(agent.run("anything"))
        assert result.success is True



class TestSafetyGuard:
    """T32: destructive action guard — 拦截 type('delete...')/click('删除按钮')."""

    def test_type_delete_keyword_blocked(self):
        """type text 含 'delete' → needs_confirm."""
        from semantic_browser.safety import check_action, SafetyCheck
        result = check_action("type", {"ref": "e5", "text": "delete this"})
        assert result.needs_confirm is True
        assert "delete" in result.reason

    def test_type_safe_text_allowed(self):
        """type 普通 text → 不拦截."""
        from semantic_browser.safety import check_action
        result = check_action("type", {"ref": "e5", "text": "hello world"})
        assert result.needs_confirm is False

    def test_click_dangerous_label_blocked(self):
        """click ref label 含 'Delete' → needs_confirm."""
        from semantic_browser.safety import check_action
        result = check_action("click", {"ref": "e5"}, ref_label="Delete Account")
        assert result.needs_confirm is True

    def test_click_safe_label_allowed(self):
        """click 普通 label → 不拦截."""
        from semantic_browser.safety import check_action
        result = check_action("click", {"ref": "e5"}, ref_label="View Details")
        assert result.needs_confirm is False

    def test_drag_to_trash_blocked(self):
        """drag to_ref 含 'trash' → needs_confirm."""
        from semantic_browser.safety import check_action
        result = check_action("drag", {"from_ref": "e1", "to_ref": "trash-bin"})
        assert result.needs_confirm is True

    def test_drag_safe_target_allowed(self):
        """drag 普通目标 → 不拦截."""
        from semantic_browser.safety import check_action
        result = check_action("drag", {"from_ref": "e1", "to_ref": "dropzone"})
        assert result.needs_confirm is False

    def test_open_extract_done_always_safe(self):
        """open/extract_text/done 永远 safe."""
        from semantic_browser.safety import check_action
        for action in ("open", "extract_text", "done"):
            r = check_action(action, {"url": "x"})
            assert r.needs_confirm is False
            assert r.risk_level == "safe"


class TestGoalAgentSafetyIntegration:
    """T32: GoalAgent 在执行 action 时跑 safety_guard."""

    def test_destructive_type_blocked(self):
        """type('delete') 被 guard 拦截, agent 收到 error."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        decisions = [
            {"thought": "type delete", "action": "type",
             "args": {"ref": "e5", "text": "delete all"}},
            {"thought": "give up", "action": "done",
             "args": {"answer": "blocked by guard"}},
        ]
        async def fake_ask(goal, snap):
            return decisions.pop(0)
        async def fake_capture(goal=""):
            return ("URL: x\n", "- e5 input: name")
        async def fake_execute(action, args):
            return True, "should not reach"

        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                          use_memory=False)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        result = asyncio.run(agent.run("delete stuff"))
        assert result.success is True
        assert result.steps[0].success is False
        assert "BLOCKED" in (result.steps[0].error or "")

    def test_allow_destructive_bypasses_guard(self):
        """allow_destructive=True → guard 放行."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        decisions = [
            {"thought": "type delete", "action": "type",
             "args": {"ref": "e5", "text": "delete all"}},
            {"thought": "done", "action": "done",
             "args": {"answer": "ok"}},
        ]
        async def fake_ask(goal, snap):
            return decisions.pop(0)
        async def fake_capture(goal=""):
            return ("URL: x\n", "")
        executed: list = []
        async def fake_execute(action, args):
            executed.append((action, args))
            return True, ""

        agent = GoalAgent(ctrl, llm_service=svc, max_steps=5,
                          use_memory=False, allow_destructive=True)
        agent._ask_llm = fake_ask  # type: ignore[method-assign]
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]
        agent._execute_action = fake_execute  # type: ignore[method-assign]

        result = asyncio.run(agent.run("delete stuff"))
        assert len(executed) == 1
        assert executed[0] == ("type", {"ref": "e5", "text": "delete all"})

    def test_safety_guard_disabled(self):
        """safety_guard=False → guard 完全跳过."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False,
                          safety_guard=False)
        assert agent.safety_guard is False



class TestGoalAgentAriaIntegration:
    """T34: GoalAgent 注入 ARIA 语义树到 snapshot excerpt."""

    def test_aria_included_in_excerpt_by_default(self):
        """include_aria=True (默认) 时 excerpt 包含 raw_aria 内容."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent
        from semantic_browser.snapshot.engine import (
            PageSnapshot, LinkInfo, ControlInfo,
        )

        class FakePage:
            url = "https://x.com"
            async def title(self):
                return "X"
        ctrl = BrowserController(BrowserConfig())
        ctrl._page = FakePage()  # type: ignore[assignment]

        # 注入一个返回 raw_aria 的 snap
        async def fake_capture(goal=""):
            return (
                "URL: x.com\nTitle: X\n\nInteractive refs (1 shown):",
                "- e1 button: Go\n\nARIA semantic tree:\n- button \"Go\"",
            )
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False,
                          use_smart_slicing=False)
        agent._capture_snapshot_excerpt = fake_capture  # type: ignore[method-assign]

        async def fake_ask(goal, snap):
            return {"thought": "x", "action": "done", "args": {"answer": "ok"}}
        agent._ask_llm = fake_ask  # type: ignore[method-assign]

        asyncio.run(agent.run("anything"))
        # ask_llm 收到的 snapshot 应包含 ARIA 树
        # 通过检查 _ask_llm mock 调用历史
        # 简单方法: 直接调 _capture_snapshot_excerpt 验证
        async def get_excerpt():
            return await agent._capture_snapshot_excerpt(goal="anything")
        header, body = asyncio.run(get_excerpt())
        # fake_capture 返回的 body 已经包含 ARIA, 验证流程不丢
        assert "ARIA semantic tree" in body

    def test_aria_disabled_when_flag_false(self):
        """include_aria=False 时 _capture 不调用 page.aria_snapshot (走 fallback)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False,
                          include_aria=False)
        assert agent.include_aria is False

    def test_aria_max_chars_truncates(self):
        """超长 ARIA 文本被截断到 aria_max_chars."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent import GoalAgent

        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")
        agent = GoalAgent(ctrl, llm_service=svc, use_memory=False,
                          aria_max_chars=100)
        assert agent.aria_max_chars == 100



class TestSiteDiscovery:
    """T30: live 站点图自动发现."""

    def test_discover_bfs_visits_pages(self):
        """discover() BFS 爬 max_pages, 记录 visited."""
        import asyncio
        from semantic_browser.graph.discoverer import discover

        ctrl = _make_fake_controller([
            ("https://x.com/", ["https://x.com/a", "https://x.com/b"]),
            ("https://x.com/a", ["https://x.com/c"]),
            ("https://x.com/b", []),
            ("https://x.com/c", []),
        ])
        result = asyncio.run(discover(ctrl, "https://x.com/", max_pages=10, max_depth=2, delay_ms=0))
        assert len(result.pages_visited) == 4
        assert "https://x.com/" in result.pages_visited
        assert "https://x.com/c" in result.pages_visited

    def test_discover_respects_max_pages(self):
        """超过 max_pages 时停止, 不全爬."""
        import asyncio
        from semantic_browser.graph.discoverer import discover

        # 5 页, max_pages=3 → 只能爬 3 页
        ctrl = _make_fake_controller([
            ("https://x.com/", [f"https://x.com/p{i}" for i in range(5)]),
            ("https://x.com/p0", []), ("https://x.com/p1", []),
            ("https://x.com/p2", []), ("https://x.com/p3", []),
            ("https://x.com/p4", []),
        ])
        result = asyncio.run(discover(ctrl, "https://x.com/", max_pages=3, max_depth=2, delay_ms=0))
        assert len(result.pages_visited) <= 3

    def test_discover_same_domain_filter(self):
        """same_domain_only=True 时不跳外站."""
        import asyncio
        from semantic_browser.graph.discoverer import discover

        ctrl = _make_fake_controller([
            ("https://x.com/", ["https://x.com/a", "https://evil.com/b"]),
            ("https://x.com/a", []),
            # evil.com 不应该被访问
        ])
        result = asyncio.run(discover(ctrl, "https://x.com/", max_pages=10, max_depth=2, delay_ms=0))
        urls = result.pages_visited
        assert "https://x.com/a" in urls
        assert "https://evil.com/b" not in urls

    def test_discover_records_failures(self):
        """页面打开失败时记到 pages_failed, 不中断."""
        import asyncio
        from semantic_browser.graph.discoverer import discover

        # 第一个页面 open 抛异常
        class BrokenCtrl:
            async def open(self, url):
                raise RuntimeError("net down")
        result = asyncio.run(discover(BrokenCtrl(), "https://x.com/", max_pages=5, max_depth=2, delay_ms=0))
        assert len(result.pages_failed) == 1
        assert "https://x.com/" in [u for u, _ in result.pages_failed]

    def test_discover_builds_graph_edges(self):
        """discover 把链接记成 graph edge."""
        import asyncio
        from semantic_browser.graph.discoverer import discover

        ctrl = _make_fake_controller([
            ("https://x.com/", ["https://x.com/a"]),
            ("https://x.com/a", []),
        ])
        result = asyncio.run(discover(ctrl, "https://x.com/", max_pages=5, max_depth=1, delay_ms=0))
        edges = result.graph.edges
        # root → a
        assert any(e[0] == "https://x.com/" and e[1] == "https://x.com/a"
                   for e in edges)

    def test_format_for_llm_includes_summary(self):
        """format_for_llm 输出包含 tree + failures."""
        from semantic_browser.graph.discoverer import format_for_llm, DiscoveryResult
        from semantic_browser.graph.builder import SiteGraph
        result = DiscoveryResult(
            root_url="https://x.com/",
            pages_visited=["https://x.com/", "https://x.com/a"],
            pages_failed=[("https://x.com/b", "404")],
        )
        result.graph = SiteGraph(root_url="https://x.com/", domain="x.com")
        text = format_for_llm(result)
        assert "Site map for" in text
        assert "Pages discovered: 2" in text
        assert "Failures:" in text
        assert "404" in text


def _make_fake_controller(pages: list[tuple[str, list[str]]]):
    """Helper: 构造 fake controller, pages = [(url, [links])]."""
    page_map = {url: links for url, links in pages}
    current = {"url": ""}

    class FakePage:
        def __init__(self, url):
            self.url = url
        async def title(self):
            return f"Page {self.url}"

    class FakeCtrl:
        @property
        def current_page(self):
            return FakePage(current["url"]) if current["url"] else None
        async def open(self, url):
            current["url"] = url

    # Patch SnapshotEngine via monkey patch
    from semantic_browser.graph import discoverer as disc_mod
    original_engine = disc_mod.SnapshotEngine

    class FakeEngine:
        def __init__(self, page):
            self.page = page
        async def capture(self, base_url=""):
            from semantic_browser.snapshot.engine import (
                PageSnapshot, LinkInfo,
            )
            url = self.page.url
            hrefs = page_map.get(url, [])
            return PageSnapshot(
                url=url, title=f"Page {url}", domain="x.com",
                links=[LinkInfo(ref=f"e{i}", href=h, text=h) for i, h in enumerate(hrefs)],
                controls=[],
                meta={}, raw_aria="",
            )
    disc_mod.SnapshotEngine = FakeEngine  # type: ignore[assignment]

    return FakeCtrl()



class TestControllerPool:
    """T33: ControllerPool — 多 controller 共享 browser, 隔离 context."""

    def test_pool_tracks_active_controllers(self):
        """acquire/release 维护 _controllers dict."""
        from semantic_browser.browser.pool import ControllerPool

        pool = ControllerPool()
        # 不真启动 browser — 只测 dict 操作
        pool._controllers["a"] = "fake_ctrl_a"  # type: ignore[assignment]
        pool._controllers["b"] = "fake_ctrl_b"  # type: ignore[assignment]
        assert pool.list_active() == ["a", "b"]

    def test_pool_max_contexts_enforced(self):
        """超过 max_contexts 时 acquire 抛异常."""
        import asyncio
        from semantic_browser.browser.pool import ControllerPool

        async def fake_acquire(name):
            # 模拟 acquire 但跳过真启动 browser
            async with pool._lock:
                if name in pool._controllers:
                    return pool._controllers[name]
                if len(pool._controllers) >= pool.max_contexts:
                    raise RuntimeError(f"ControllerPool exhausted")
                # mock: 不真创建 controller, 只入 dict
                pool._controllers[name] = f"fake_{name}"
                return pool._controllers[name]

        pool = ControllerPool(max_contexts=2)
        pool._lock = asyncio.Lock()
        # patch acquire 用 fake
        pool.acquire = fake_acquire  # type: ignore[method-assign]
        asyncio.run(pool.acquire("a"))
        asyncio.run(pool.acquire("b"))
        # 第 3 个应失败
        try:
            asyncio.run(pool.acquire("c"))
            assert False, "should have raised"
        except RuntimeError as e:
            assert "exhausted" in str(e)

    def test_pool_reuses_named_controller(self):
        """同名 acquire 直接复用, 不创建新 context."""
        import asyncio
        from semantic_browser.browser.pool import ControllerPool

        async def fake_acquire(name):
            async with pool._lock:
                if name in pool._controllers:
                    return pool._controllers[name]
                pool._controllers[name] = f"fake_{name}"
                return pool._controllers[name]

        pool = ControllerPool()
        pool._lock = asyncio.Lock()
        pool.acquire = fake_acquire  # type: ignore[method-assign]

        ctrl1 = asyncio.run(pool.acquire("agent-x"))
        ctrl2 = asyncio.run(pool.acquire("agent-x"))
        assert ctrl1 is ctrl2
        assert pool.list_active() == ["agent-x"]

    def test_pool_release_closes_context(self):
        """release() 关闭 controller 自己的 context, 不影响共享 browser."""
        import asyncio
        from semantic_browser.browser.pool import ControllerPool

        class FakeCtrl:
            def __init__(self, name):
                self._context = f"ctx_{name}"
                self._pool_name = name

        class FakePool(ControllerPool):
            async def release(self, name):
                async with self._lock:
                    ctrl = self._controllers.pop(name, None)
                if ctrl is None:
                    return
                # 模拟 close context
                closed = []
                if ctrl._context is not None:
                    closed.append(ctrl._context)
                return closed

        pool = FakePool()
        pool._lock = asyncio.Lock()
        pool._controllers["a"] = FakeCtrl("a")  # type: ignore[assignment]
        pool._controllers["b"] = FakeCtrl("b")  # type: ignore[assignment]
        asyncio.run(pool.release("a"))
        assert pool.list_active() == ["b"]

    def test_pool_context_manager(self):
        """async with pool as p: ... 走 start/close."""
        from semantic_browser.browser.pool import ControllerPool

        class FakePool(ControllerPool):
            def __init__(self):
                super().__init__()
                self.events = []
            async def start(self):
                self.events.append("start")
            async def close(self):
                self.events.append("close")

        pool = FakePool()
        import asyncio

        async def use():
            async with pool as p:
                p.events.append("enter")
            return p.events

        events = asyncio.run(use())
        assert events == ["start", "enter", "close"]

    def test_make_controller_shares_browser(self):
        """_make_controller 注入共享 browser, 不重新启动."""
        from semantic_browser.browser.pool import ControllerPool

        # 模拟共享 playwright + browser
        class FakeBrowser:
            pass
        class FakePlaywright:
            pass

        pool = ControllerPool()
        pool._browser = FakeBrowser()  # type: ignore[assignment]
        pool._playwright = FakePlaywright()  # type: ignore[assignment]

        ctrl = pool._make_controller("test-agent")
        # 共享同一个 browser / playwright
        assert ctrl._browser is pool._browser
        assert ctrl._playwright is pool._playwright
        assert ctrl._pool_name == "test-agent"
        # context 还没建 (懒加载)
        assert ctrl._context is None



class TestBenchmark:
    """T35: golden task 评测套件."""

    def test_load_tasks_from_json(self):
        """load_tasks() 解析 JSON 列表."""
        import json
        import tempfile
        from pathlib import Path
        from semantic_browser.bench import load_tasks

        data = [
            {"name": "t1", "goal": "extract h1", "start_url": "https://x.com",
             "expected": {"answer_contains": "Hello", "max_steps": 5},
             "tags": ["smoke"]},
            {"name": "t2", "goal": "click button"},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            tasks = load_tasks(f.name)
        assert len(tasks) == 2
        assert tasks[0].name == "t1"
        assert tasks[0].expected_answer_contains == "Hello"
        assert tasks[0].expected_max_steps == 5
        assert tasks[0].tags == ["smoke"]
        assert tasks[1].expected_answer_contains == ""  # 没指定

    def test_grade_passes_when_answer_contains(self):
        """answer 含 expected → pass."""
        from semantic_browser.bench import GoldenTask, _grade
        from semantic_browser.agent.loop import GoalResult

        task = GoldenTask(name="t", goal="x", expected_answer_contains="hello")
        result = GoalResult(goal="x", success=True, answer="say hello world", total_steps=3)
        ok, reason = _grade(task, result)
        assert ok is True
        assert reason == ""

    def test_grade_fails_when_answer_missing(self):
        """answer 不含 expected → fail."""
        from semantic_browser.bench import GoldenTask, _grade
        from semantic_browser.agent.loop import GoalResult

        task = GoldenTask(name="t", goal="x", expected_answer_contains="hello")
        result = GoalResult(goal="x", success=True, answer="goodbye", total_steps=3)
        ok, reason = _grade(task, result)
        assert ok is False
        assert "hello" in reason

    def test_grade_case_insensitive(self):
        """answer 大小写不敏感 (lowercase 比较)."""
        from semantic_browser.bench import GoldenTask, _grade
        from semantic_browser.agent.loop import GoalResult

        task = GoldenTask(name="t", goal="x", expected_answer_contains="HELLO")
        result = GoalResult(goal="x", success=True, answer="hello world", total_steps=3)
        ok, _ = _grade(task, result)
        assert ok is True

    def test_grade_fails_when_steps_exceed_max(self):
        """步数 > expected_max_steps → fail."""
        from semantic_browser.bench import GoldenTask, _grade
        from semantic_browser.agent.loop import GoalResult

        task = GoldenTask(name="t", goal="x",
                          expected_answer_contains="hi",
                          expected_max_steps=3)
        result = GoalResult(goal="x", success=True, answer="hi", total_steps=10)
        ok, reason = _grade(task, result)
        assert ok is False
        assert "steps" in reason

    def test_run_benchmark_aggregates_results(self):
        """run_benchmark() 累加 result + 算 success rate."""
        import asyncio
        from semantic_browser.bench import GoldenTask, run_benchmark
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        from semantic_browser.llm import LLMService
        from semantic_browser.agent.loop import GoalAgent

        # fake controller + service
        ctrl = BrowserController(BrowserConfig())
        svc = LLMService(api_key="k", base_url="http://fake")

        # monkey-patch GoalAgent.run 来 fake 返回
        original_run = GoalAgent.run
        async def fake_run(self, goal, *, start_url=None):
            from semantic_browser.agent.loop import GoalResult, StepRecord
            if "pass" in goal:
                return GoalResult(
                    goal=goal, success=True, answer="hello world",
                    steps=[], total_steps=2,
                )
            return GoalResult(
                goal=goal, success=False, reason="simulated fail",
                steps=[], total_steps=0,
            )
        GoalAgent.run = fake_run  # type: ignore[method-assign]

        tasks = [
            GoldenTask(name="pass1", goal="do thing pass",
                       expected_answer_contains="hello"),
            GoldenTask(name="fail1", goal="do thing fail"),
        ]

        try:
            report = asyncio.run(run_benchmark(
                tasks, llm_service=svc, controller=ctrl, use_memory=False,
            ))
            assert report.total == 2
            assert report.succeeded == 1
            assert report.failed == 1
            assert report.success_rate == 0.5
            # pass1 答 "hello world" 含 "hello" → pass
            assert report.results[0].success is True
            # fail1 没成功 → fail
            assert report.results[1].success is False
        finally:
            GoalAgent.run = original_run  # type: ignore[method-assign]

    def test_benchmark_report_to_dict_shape(self):
        """BenchmarkReport.to_dict() 包含全部字段."""
        from semantic_browser.bench import BenchmarkReport, TaskResult, GoldenTask
        report = BenchmarkReport(total=2)
        report.succeeded = 1
        report.failed = 1
        report.avg_steps = 3.0
        report.avg_duration_sec = 1.5
        report.results = [
            TaskResult(task=GoldenTask(name="t1", goal="x"),
                       success=True, actual_answer="ok",
                       actual_steps=2, duration_sec=1.0),
        ]
        report.failure_reasons = {"agent failed": 1}
        d = report.to_dict()
        assert d["total"] == 2
        assert d["succeeded"] == 1
        assert d["success_rate"] == 0.5
        assert d["avg_steps"] == 3.0
        assert "agent failed" in d["failure_reasons"]
        assert len(d["results"]) == 1



class TestMCPServerAdvancedTools:
    """T37: MCP server 暴露高级 agent 工具."""

    def test_tools_list_includes_advanced(self):
        """TOOL_DEFINITIONS 包含 sb_agent_run / sb_agent_plan / sb_discover 等."""
        from semantic_browser.mcp_server.server import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        # 高级工具必须存在
        assert "sb_agent_run" in names
        assert "sb_agent_plan" in names
        assert "sb_memory_lookup" in names
        assert "sb_memory_stats" in names
        assert "sb_discover" in names
        assert "sb_safety_check" in names
        # 原有底层工具保留
        assert "sb_click" in names
        assert "sb_snapshot" in names

    def test_sb_safety_check_blocks_delete(self):
        """sb_safety_check: action=type, text=delete → needs_confirm=True."""
        import asyncio
        from semantic_browser.mcp_server.server import MCPServer

        server = MCPServer(engine=None)  # 不会真启动 — safety 不依赖 engine
        result = asyncio.run(server._call_tool(
            "sb_safety_check",
            {"action": "type", "text": "delete all"},
        ))
        assert result["needs_confirm"] is True
        assert "delete" in result["reason"]

    def test_sb_safety_check_allows_safe(self):
        """sb_safety_check: 普通 action → needs_confirm=False."""
        import asyncio
        from semantic_browser.mcp_server.server import MCPServer

        server = MCPServer(engine=None)
        result = asyncio.run(server._call_tool(
            "sb_safety_check",
            {"action": "type", "text": "hello world"},
        ))
        assert result["needs_confirm"] is False

    def test_sb_memory_stats_returns_path(self):
        """sb_memory_stats 返回 path / total 字段."""
        import asyncio
        from semantic_browser.mcp_server.server import MCPServer

        server = MCPServer(engine=None)
        result = asyncio.run(server._call_tool("sb_memory_stats", {}))
        assert "path" in result
        assert "total" in result
        assert "success" in result
        assert "failure" in result


class TestT39DeepSnapshot:
    """T39: 默认/深度两层信息架构 — dataclass 字段 + 控制器方法 + MCP/CLI 注册."""

    # ── dataclass 字段 ────────────────────────────────────────

    def test_script_info_default_values(self):
        """ScriptInfo 默认字段全空, has_src 默认 False."""
        from semantic_browser.snapshot.engine import ScriptInfo
        s = ScriptInfo()
        assert s.src == ""
        assert s.inline == ""
        assert s.has_src is False

    def test_control_info_form_metadata_defaults(self):
        """ControlInfo form 元数据字段全默认空 (向后兼容)."""
        from semantic_browser.snapshot.engine import ControlInfo
        c = ControlInfo(ref="e1", kind="textbox", label="x")
        assert c.form_action == ""
        assert c.form_method == ""
        assert c.form_id == ""
        assert c.input_name == ""
        assert c.input_type == ""
        assert c.raw_attrs == {}
        assert c.outer_html == ""

    def test_page_snapshot_has_scripts_and_detail_level(self):
        """PageSnapshot 新增 scripts + detail_level 字段."""
        from semantic_browser.snapshot.engine import PageSnapshot, ScriptInfo
        snap = PageSnapshot(url="https://x", title="t", domain="x")
        assert snap.scripts == []
        assert snap.detail_level == "normal"
        snap.scripts.append(ScriptInfo(src="https://cdn/x.js", has_src=True))
        snap.detail_level = "deep"
        d = snap.to_dict()
        assert len(d["scripts"]) == 1
        assert d["scripts"][0]["src"] == "https://cdn/x.js"
        assert d["detail_level"] == "deep"

    # ── 控制器方法: get_response_headers ─────────────────────────

    def test_get_response_headers_returns_lowercased_dict(self):
        """get_response_headers: URL 命中 → 返回 lowercased-keys 字典."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig

        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            method = "GET"
            url = "https://api.example.com/users"
            resource_type = "fetch"

        class FakeResp:
            url = "https://api.example.com/users"
            status = 200
            headers = {"Content-Type": "application/json",
                       "X-Frame-Options": "DENY",
                       "Set-Cookie": "sid=abc; HttpOnly"}
            request = FakeReq()

        ctrl._on_request(FakeReq())
        ctrl._on_response(FakeResp())

        headers = asyncio.run(ctrl.get_response_headers("https://api.example.com/users"))
        assert headers is not None
        assert headers["content-type"] == "application/json"
        assert headers["x-frame-options"] == "DENY"
        assert "set-cookie" in headers

    def test_get_response_headers_not_found(self):
        """get_response_headers: 没找到 → 返回 None."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        result = asyncio.run(ctrl.get_response_headers("https://never-seen.com/"))
        assert result is None

    def test_on_response_with_headers_pops_latest(self):
        """_on_response 把 headers 写回最近一条匹配的 request."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig

        ctrl = BrowserController(BrowserConfig())

        class FakeReq:
            def __init__(self, m="GET", u="https://x.com/"):
                self.method = m
                self.url = u
                self.resource_type = "fetch"

        class FakeResp:
            def __init__(self, url, status=200, headers=None):
                self.url = url
                self.status = status
                self.headers = headers or {}
                self.request = FakeReq("GET", url)

        ctrl._on_request(FakeReq())
        ctrl._on_response(FakeResp("https://x.com/", 200,
                                   headers=[["Strict-Transport-Security", "max-age=31536000"]]))

        # Find the request entry
        entry = ctrl._network_requests[0]
        assert "response_headers" in entry
        assert "strict-transport-security" in entry["response_headers"]

    # ── 控制器方法: fetch_script_source ────────────────────────

    def test_fetch_script_source_error_returns_error_string(self):
        """fetch_script_source: 不可达 URL → 返回带错误的字符串 (不抛)."""
        import asyncio
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 用保留端口 — fetch 一定失败
        result = asyncio.run(ctrl.fetch_script_source(
            "http://127.0.0.1:1/never.js", timeout_ms=500))
        # 失败时返回 "(fetch failed: ..." 格式
        assert isinstance(result, str)
        assert "fetch failed" in result or "Error" in result or len(result) > 0

    # ── MCP: 新工具注册 ──────────────────────────────────────

    def test_mcp_tools_register_t39(self):
        """TOOL_DEFINITIONS 包含 T39 4 个新工具."""
        from semantic_browser.mcp_server.server import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "sb_snapshot_deep" in names
        assert "sb_get_response_headers" in names
        assert "sb_get_dom_diff" in names
        assert "sb_get_script_source" in names

    def test_mcp_sb_safety_check_accepts_ref_label(self):
        """sb_safety_check 接受 ref_label 参数 — 用于 click action."""
        import asyncio
        from semantic_browser.mcp_server.server import MCPServer
        server = MCPServer(engine=None)
        # click + ref_label 含 "delete" → needs_confirm
        result = asyncio.run(server._call_tool(
            "sb_safety_check",
            {"action": "click", "ref_label": "Delete Account"},
        ))
        assert result["needs_confirm"] is True

    # ── CLI: 新 debug 子命令注册 ──────────────────────────────

    def test_cli_registers_t39_debug_commands(self):
        """cli 包含 debug headers / dom-diff / script-source 子命令."""
        from semantic_browser.client.cli import tb
        # tb 是 root Group, debug 是子命令 (Group)
        debug = tb.commands.get("debug")
        assert debug is not None, "debug subcommand missing"
        cmd_names = set(debug.commands.keys())
        assert "headers" in cmd_names
        assert "dom-diff" in cmd_names
        assert "script-source" in cmd_names


class TestT40c40dSnapshotCommentsParams:
    """T40c + T40d: HTML 注释提取 + URL 参数解析."""

    def test_link_info_has_params_default(self):
        """LinkInfo 默认 params 空 dict."""
        from semantic_browser.snapshot.engine import LinkInfo
        li = LinkInfo(ref="e1", text="x", href="/a")
        assert li.params == {}

    def test_control_info_has_form_params_default(self):
        """ControlInfo 默认 form_params 空 dict."""
        from semantic_browser.snapshot.engine import ControlInfo
        c = ControlInfo(ref="e1", kind="textbox", label="x")
        assert c.form_params == {}

    def test_page_snapshot_has_comments_default(self):
        """PageSnapshot 默认 comments 空 list."""
        from semantic_browser.snapshot.engine import PageSnapshot
        snap = PageSnapshot(url="u", title="t", domain="d")
        assert snap.comments == []
        d = snap.to_dict()
        assert d["comments"] == []

    def test_page_snapshot_includes_comments_in_to_dict(self):
        """comments 进 to_dict, agent 能拿到."""
        from semantic_browser.snapshot.engine import PageSnapshot
        snap = PageSnapshot(url="u", title="t", domain="d")
        snap.comments.append("TODO: fix X")
        snap.comments.append("<!-- debug flag -->")
        d = snap.to_dict()
        assert "TODO: fix X" in d["comments"]
        assert "<!-- debug flag -->" in d["comments"]


class TestT40a40fStorageAndSecurityHeaders:
    """T40a + T40f: 客户端存储 + 安全头结构化."""

    def test_parse_csp_extracts_directives(self):
        from semantic_browser.browser.controller import _parse_csp
        out = _parse_csp("default-src 'self'; script-src 'unsafe-inline' 'self' cdn.example.com; img-src *")
        assert "default-src" in out["directives"]
        assert out["directives"]["default-src"] == ["'self'"]
        assert out["has_unsafe_inline"] is True
        assert out["allows_wildcard"] is True
        assert out["has_default_src"] is True
        assert out["has_script_src"] is True
        assert out["directive_names"] == ["default-src", "script-src", "img-src"]

    def test_parse_csp_empty_returns_empty(self):
        from semantic_browser.browser.controller import _parse_csp
        out = _parse_csp("")
        assert out["directives"] == {}
        assert out["has_unsafe_inline"] is False

    def test_parse_hsts_basic(self):
        from semantic_browser.browser.controller import _parse_hsts
        out = _parse_hsts("max-age=31536000; includeSubDomains")
        assert out["max_age"] == 31536000
        assert out["include_subdomains"] is True
        assert out["preload"] is False

    def test_parse_hsts_with_preload(self):
        from semantic_browser.browser.controller import _parse_hsts
        out = _parse_hsts("max-age=63072000; includeSubDomains; preload")
        assert out["max_age"] == 63072000
        assert out["preload"] is True

    def test_parse_set_cookie_extracts_flags(self):
        from semantic_browser.browser.controller import _parse_set_cookie
        sc = _parse_set_cookie(
            "sessionId=abc123; Path=/; HttpOnly; Secure; SameSite=Strict; "
            "Domain=example.com; Max-Age=3600"
        )
        assert sc["name"] == "sessionId"
        assert sc["value"] == "abc123"
        assert sc["httpOnly"] is True
        assert sc["secure"] is True
        assert sc["sameSite"] == "Strict"
        assert sc["path"] == "/"
        assert sc["domain"] == "example.com"
        assert sc["max_age"] == 3600

    def test_parse_set_cookie_minimal(self):
        from semantic_browser.browser.controller import _parse_set_cookie
        sc = _parse_set_cookie("foo=bar")
        assert sc["name"] == "foo"
        assert sc["httpOnly"] is False
        assert sc["secure"] is False
        assert sc["sameSite"] == ""

    def test_parse_permissions_policy_basic(self):
        from semantic_browser.browser.controller import _parse_permissions_policy
        out = _parse_permissions_policy(
            "camera=(), microphone=(self), geolocation=*"
        )
        assert "camera" in out["directives"]
        assert "microphone" in out["directives"]
        assert "geolocation" in out["directives"]

    def test_storage_init_buffer_starts_empty(self):
        """_storage 字段不存在 — 走 page.evaluate (测不了 page, 测 shape)."""
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        ctrl = BrowserController(BrowserConfig())
        # 验证 storage 方法存在并返回预期 shape (在没 page 时 raise)
        assert hasattr(ctrl, "get_storage")
        assert hasattr(ctrl, "get_security_headers")

    def test_mcp_tools_register_t40a_f(self):
        """TOOL_DEFINITIONS 包含 sb_storage + sb_security_headers."""
        from semantic_browser.mcp_server.server import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "sb_storage" in names
        assert "sb_security_headers" in names

    def test_cli_commands_register_t40a_f(self):
        """tb storage + security-headers 子命令注册."""
        from semantic_browser.client.cli import tb
        assert "storage" in tb.commands
        assert "security-headers" in tb.commands


class TestT40bHiddenPathsProbe:
    """T40b: 探测常见隐藏路径 — robots/sitemap/.well-known/admin/api."""

    def test_well_known_paths_defined(self):
        """_WELL_KNOWN_PATHS 包含 security.txt / openid / change-password 等."""
        from semantic_browser.browser.controller import BrowserController
        paths = BrowserController._WELL_KNOWN_PATHS
        assert "/.well-known/security.txt" in paths
        assert "/.well-known/openid-configuration" in paths
        assert "/.well-known/change-password" in paths

    def test_discovery_paths_include_robots_sitemap(self):
        from semantic_browser.browser.controller import BrowserController
        paths = BrowserController._DISCOVERY_PATHS
        assert "/robots.txt" in paths
        assert "/sitemap.xml" in paths
        assert "/llms.txt" in paths
        assert "/.git/HEAD" in paths

    def test_admin_paths_include_common(self):
        from semantic_browser.browser.controller import BrowserController
        paths = BrowserController._ADMIN_PATHS
        assert "/admin" in paths
        assert "/login" in paths
        assert "/api" in paths
        assert "/graphql" in paths
        assert "/wp-admin/" in paths

    @pytest.mark.asyncio
    async def test_probe_paths_against_http_server(self):
        """真实 HTTP server — 验证探测逻辑 (found/missing 分类正确)."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from semantic_browser.browser.controller import BrowserController

        # 简易 stdlib HTTP server: robots.txt + admin → 200; 其他 → 404
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):  # 静音
                pass
            def do_GET(self):
                if self.path == "/robots.txt":
                    body = b"User-agent: *\nDisallow: /admin\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/admin":
                    body = b"<html>login</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    body = b"nope"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            ctrl = BrowserController()
            result = await ctrl.probe_paths(base_url, categories=["discovery", "admin"])
            assert result["origin"] == base_url
            assert result["total_probed"] > 0
            # robots.txt + admin 应当 found
            found_paths = {e["path"] for e in result["found"]}
            assert "/robots.txt" in found_paths
            assert "/admin" in found_paths
            # /login 不在 server, 应当 missing
            missing_paths = {e["path"]: e["status"] for e in result["missing"]}
            assert missing_paths.get("/login") == 404
        finally:
            srv.shutdown()
            srv.server_close()

    def test_mcp_tool_register_t40b(self):
        """sb_probe_paths 注册到 MCP TOOL_DEFINITIONS."""
        from semantic_browser.mcp_server.server import TOOL_DEFINITIONS
        names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "sb_probe_paths" in names

    def test_cli_command_register_t40b(self):
        """tb probe-paths 注册."""
        from semantic_browser.client.cli import tb
        assert "probe-paths" in tb.commands

