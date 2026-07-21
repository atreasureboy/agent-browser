"""
Semantic Browser Engine — 核心编排引擎。

把 Browser → Snapshot → Classifier → Extractor → Memory → Graph 串起来。
这是整个系统的心脏。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from semantic_browser.browser.controller import BrowserController, BrowserConfig
from semantic_browser.snapshot.engine import SnapshotEngine, PageSnapshot
from semantic_browser.classifier.heuristic import PageClassifier, ClassificationResult
from semantic_browser.classifier.llm_enhanced import LLMEnhancedClassifier
from semantic_browser.extractor.content import ContentExtractor, ArticleContent, InterfaceSummary
from semantic_browser.memory.store import MemoryStore
from semantic_browser.graph.builder import GraphBuilder, SiteGraph

logger = logging.getLogger(__name__)


@dataclass
class BrowseResult:
    """一次浏览的完整结果。"""
    url: str
    snapshot: PageSnapshot
    classification: ClassificationResult
    article: Optional[ArticleContent]
    interfaces: Optional[InterfaceSummary]
    elapsed: float

    def to_dict(self, *, full: bool = False) -> dict[str, Any]:
        """序列化。

        full=False (默认): 摘要视图 — snapshot 只给计数, article 只给统计。
                            适合 CLI 一次性浏览。
        full=True: 完整视图 — snapshot 包含全部 text_blocks/links/controls,
                    article 包含全部 sections/code_blocks/tables/images。
                    适合 agent 一次性拿全数据, 避免多次调用。
        """
        snap_summary = {
            "url": self.snapshot.url,
            "title": self.snapshot.title,
            "page_type": self.snapshot.page_type,
            "domain": self.snapshot.domain,
            "meta": self.snapshot.meta,
            "text_blocks_count": len(self.snapshot.text_blocks),
            "links_count": len(self.snapshot.links),
            "controls_count": len(self.snapshot.controls),
        }
        if full:
            snap_summary["text_blocks"] = [b.to_dict() if hasattr(b, "to_dict") else {
                "tag": b.tag, "text": b.text, "level": b.level,
            } for b in self.snapshot.text_blocks]
            snap_summary["links"] = [{
                "ref": l.ref, "text": l.text, "href": l.href, "internal": l.internal,
            } for l in self.snapshot.links]
            snap_summary["controls"] = [{
                "ref": c.ref, "kind": c.kind, "label": c.label,
                "placeholder": c.placeholder, "role": c.role,
            } for c in self.snapshot.controls]

        d = {
            "url": self.url,
            "final_url": self.snapshot.url,
            "elapsed": round(self.elapsed, 2),
            "classification": self.classification.to_dict(),
            "snapshot": snap_summary,
        }
        if self.article:
            art = {
                "title": self.article.title,
                "author": self.article.author,
                "date": self.article.publish_date,
                "sections_count": len(self.article.sections),
                "word_count": self.article.word_count,
                "text_length": self.article.text_length,
                "confidence": round(self.article.extraction_confidence, 2),
                # 始终带一个 top-level summary — 前 ~1500 字符的扁平文本, 方便快速消费
                "summary": self._build_summary(1500),
            }
            if full:
                art["sections"] = self.article.sections
                art["code_blocks"] = self.article.code_blocks
                art["tables"] = self.article.tables
                art["images"] = self.article.images
            d["article"] = art
        if self.interfaces:
            d["interfaces"] = self.interfaces.summary()
        return d

    def _build_summary(self, max_chars: int = 1500) -> str:
        """把 article 扁平化成一段文本, 用于快速 LLM 消费。"""
        if not self.article:
            return ""
        parts = []
        if self.article.title:
            parts.append(self.article.title)
            parts.append("")
        for section in self.article.sections:
            if section.get("heading"):
                parts.append(section["heading"])
                parts.append("")
            for p in section.get("paragraphs", []):
                parts.append(p)
                parts.append("")
                if sum(len(x) for x in parts) > max_chars:
                    break
            if sum(len(x) for x in parts) > max_chars:
                break
        text = "\n".join(parts).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text


class SemanticBrowser:
    """
    语义浏览器引擎。

    用法:
        sb = SemanticBrowser()
        await sb.start()
        result = await sb.browse("https://blog.example.com/post/123")
        print(result.classification.page_type)  # "article"
        print(result.article.title)              # "Rust 异步运行时"
        graph = sb.get_site_graph("https://blog.example.com")
        print(graph.to_tree_text())
        await sb.close()
    """

    def __init__(
        self,
        db_path: str = "~/.semantic-browser/memory.db",
        headless: bool = True,
        use_llm_classifier: bool = False,
        storage_state_path: str | None = None,
    ) -> None:
        import os
        db_path = os.path.expanduser(db_path)
        self.store = MemoryStore(db_path)
        self.controller = BrowserController(BrowserConfig(
            headless=headless,
            storage_state_path=os.path.expanduser(storage_state_path) if storage_state_path else None,
        ))
        self.classifier = LLMEnhancedClassifier(enable_llm=use_llm_classifier)
        self.session_id = f"sess_{int(time.time())}"
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self.controller.start()
        self.store.start_session(self.session_id)
        self._started = True
        logger.info("SemanticBrowser started (session=%s)", self.session_id)

    async def close(self) -> None:
        if not self._started:
            return
        self.store.end_session(self.session_id)
        await self.controller.close()
        self._started = False
        logger.info("SemanticBrowser closed")

    async def browse(
        self, url: str, extract_content: bool = True,
    ) -> BrowseResult:
        """
        浏览一个 URL 的完整流程：
        打开 → 快照 → 分类 → 提取正文 → 提取接口 → 记忆。

        T104 fix: Playwright 失败时 (Page crashed / net error / anti-bot 阻),
        page 会卡在坏状态. 之前所有后续 query 都返 0. 现在 try/finally
        失败时 reset page (goto about:blank) 解锁下一 query.
        """
        if not self._started:
            await self.start()

        t0 = time.time()
        logger.info("Browsing: %s", url)

        page = await self.controller.open(url)

        try:
            # 2. 语义快照
            snap_engine = SnapshotEngine(page)
            snapshot = await snap_engine.capture(base_url=url)

            # T107: antibot fast-fail — 在分类/提取前先 detect. 阻了就 raise
            # 不让 M3 浪费 token. 注意: page.content() 可能抛 (crashed 等),
            # 那种情况已经被外层 try/except 兜住.
            try:
                html = await page.content()
                from semantic_browser.safety.antibot import detect_antibot
                blocked, reason = detect_antibot(html, status=200)
                if blocked:
                    logger.warning(
                        "Antibot triggered for %s: %s", url, reason
                    )
                    raise RuntimeError(f"antibot block: {reason}")
            except RuntimeError:
                raise
            except Exception:
                # content 取不到不影响主流程
                pass

            # 3. 页面分类（LLM 增强分类器是 async）
            if hasattr(self.classifier, '_llm_classify'):
                classification = await self.classifier.classify(snapshot)
            else:
                classification = self.classifier.classify(snapshot)
            snapshot.page_type = classification.page_type

            # 4. 内容提取（如果是文章/文档类）
            article = None
            interfaces = None
            if extract_content:
                extractor = ContentExtractor(page)
                if classification.page_type in ("article", "docs", "unknown"):
                    article = await extractor.extract_article()
                interfaces = await extractor.extract_interfaces()
        except Exception as e:
            # T104 fix: page 卡坏状态. 失败时 reset page 到 about:blank
            # 否则后续 query 全污染 (实测: 1 个 Amazon 查询后, 后续 5+ 个
            # 站都返 0 sources 因为共享 browser 处于半坏状态)
            logger.warning("browse failed for %s (%s); resetting page", url, e)
            try:
                await page.goto("about:blank", timeout=5)
            except Exception:
                pass  # page 死了, 下一 query 拿新 controller
            raise
        finally:
            elapsed = time.time() - t0

        # 5. 存入记忆
        self._record_to_memory(url, snapshot, classification, page.url)

        result = BrowseResult(
            url=url,
            snapshot=snapshot,
            classification=classification,
            article=article,
            interfaces=interfaces,
            elapsed=elapsed,
        )
        logger.info(
            "Browse complete: %s [%s] in %.2fs",
            url, classification.page_type, elapsed,
        )
        return result

    def _record_to_memory(
        self,
        url: str,
        snapshot: PageSnapshot,
        classification: ClassificationResult,
        final_url: str,
    ) -> None:
        """记录到 MemoryStore。"""
        self.store.record_page(
            url=final_url,
            domain=snapshot.domain,
            title=snapshot.title,
            page_type=classification.page_type,
            confidence=classification.confidence,
            meta=snapshot.meta,
            snapshot_json=snapshot.to_json(),
        )
        self.store.record_links(
            from_url=final_url,
            links=[{"href": l.href, "text": l.text} for l in snapshot.links],
        )
        self.store.record_action(
            session_id=self.session_id,
            action="browse",
            url=final_url,
            detail=f"classified as {classification.page_type}",
        )
        self.store.increment_page_visit(self.session_id)

    # ── 查询能力 ──────────────────────────────────────────────

    def get_site_graph(self, root_url: str) -> SiteGraph:
        """获取站点拓扑图。"""
        builder = GraphBuilder(self.store)
        return builder.build(root_url)

    def get_memory_stats(self) -> dict[str, Any]:
        return self.store.stats()

    def get_visited_pages(self, domain: str = "", limit: int = 100) -> list[dict]:
        if domain:
            return self.store.get_pages_by_domain(domain, limit=limit)
        import sqlite3
        conn = sqlite3.connect(str(self.store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM pages ORDER BY visited_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def save_storage_state(self, path: str | None = None) -> str:
        """保存当前浏览器上下文的登录态。"""
        if not self._started:
            await self.start()
        return await self.controller.save_storage_state(path)

    async def click_and_browse(self, ref: str) -> BrowseResult:
        """点击元素并浏览新页面。"""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self.controller.click(ref)
        await self.controller.wait(1.5)  # 等待页面加载
        new_url = await self.controller.get_url()
        return await self.browse(new_url)

    # ── 主题抽取 ──────────────────────────────────────────────

    async def find(self, url: str, keyword: str, **kwargs) -> dict:
        """浏览 url 并查找包含 keyword 的 sections。

        等价于 browse + article.find_sections, 但少一次浏览器启动 (复用本次 browse 的 article)。
        found=True 仅当 find_sections 返回至少一个 section。

        B24: 空 keyword 抛 ValueError (而非返回 data.error 字符串),
        统一错误路径 — CLI 转 stderr 退出码 1, daemon 转 HTTP 400。
        """
        if not keyword or not keyword.strip():
            raise ValueError("keyword is empty; provide a non-empty keyword to search for")
        result = await self.browse(url)
        if not result.article:
            return {"keyword": keyword, "found": False, "sections": [],
                    "total_sections": 0}
        sections = result.article.find_sections(keyword, **kwargs)
        return {
            "keyword": keyword,
            "found": bool(sections),
            "sections": sections,
            "total_sections": len(result.article.sections),
        }

    async def extract_topic(self, url: str, keyword: str, **kwargs) -> dict:
        """浏览 url 并抽取 keyword 相关的主题摘要。

        B24: 空 keyword 抛 ValueError (而非返回 data.error 字符串)。
        """
        if not keyword or not keyword.strip():
            raise ValueError("keyword is empty; provide a non-empty keyword to extract")
        result = await self.browse(url)
        if not result.article:
            return {"keyword": keyword, "found": False, "sections": [],
                    "total_chars": 0, "section_count": 0}
        return result.article.extract_topic(keyword, **kwargs)
