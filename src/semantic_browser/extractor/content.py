"""
Content Extractor — 正文/接口提取层。

从页面提取干净的结构化内容。
不依赖 LLM，纯 DOM 分析 + Readability 思路。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from urllib.parse import urljoin

from playwright.async_api import Page

logger = logging.getLogger(__name__)


@dataclass
class ArticleContent:
    """提取的文章内容。"""
    title: str = ""
    author: str = ""
    publish_date: str = ""
    sections: list[dict] = field(default_factory=list)  # [{heading, paragraphs, code_blocks, tables, images}]
    code_blocks: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    word_count: int = 0  # 真实词数 (按空白分词)
    text_length: int = 0  # 字符数
    extraction_confidence: float = 0.0

    def find_sections(
        self,
        keyword: str,
        *,
        case_insensitive: bool = True,
        max_results: int = 10,
    ) -> list[dict]:
        """查找包含 keyword 的 section。

        返回 [{section_index, heading, level, matched_paragraphs, score}], 按 score 降序。
        score = 标题命中 3 + 段落命中 1 + code 命中 1。
        """
        if not keyword:
            return []
        needle = keyword.lower() if case_insensitive else keyword
        results = []
        for i, section in enumerate(self.sections):
            heading = section.get("heading", "") or ""
            paragraphs = section.get("paragraphs", []) or []
            code_blocks = section.get("code_blocks", []) or []

            hay_heading = heading.lower() if case_insensitive else heading
            head_hit = needle in hay_heading

            matched_paras = [
                p for p in paragraphs
                if needle in (p.lower() if case_insensitive else p)
            ]
            matched_code = [
                c for c in code_blocks
                if needle in (c.lower() if case_insensitive else c)
            ]

            score = (3 if head_hit else 0) + len(matched_paras) + len(matched_code)
            if score == 0:
                continue

            results.append({
                "section_index": i,
                "heading": heading,
                "level": section.get("level", 2),
                "matched_paragraphs": matched_paras[:5],  # 防止返回过大
                "matched_code_blocks": matched_code[:3],
                "score": score,
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max_results]

    def extract_topic(
        self,
        keyword: str,
        *,
        max_chars: int = 4000,
        case_insensitive: bool = True,
    ) -> dict:
        """提取与 keyword 相关的紧凑主题摘要。

        返回 {
          keyword, found, total_chars,
          sections: [{heading, level, excerpt}],  # 拼接的命中段落, 截断到 max_chars
        }
        如果 found=False (无任何命中) sections 为空。
        """
        hits = self.find_sections(keyword, case_insensitive=case_insensitive, max_results=20)
        if not hits:
            return {"keyword": keyword, "found": False, "total_chars": 0, "sections": []}

        out_sections = []
        used = 0
        for hit in hits:
            remaining = max_chars - used
            if remaining <= 50:
                break
            # 拼接该 section 的所有命中段落 + 命中 code
            pieces = []
            if hit["heading"]:
                pieces.append(f"## {hit['heading']}")
            for p in hit["matched_paragraphs"]:
                pieces.append(p)
            for c in hit["matched_code_blocks"]:
                pieces.append(f"```\n{c}\n```")
            excerpt = "\n\n".join(pieces)
            if len(excerpt) > remaining:
                excerpt = excerpt[:remaining].rstrip() + "…"
            out_sections.append({
                "heading": hit["heading"],
                "level": hit["level"],
                "section_index": hit["section_index"],
                "excerpt": excerpt,
            })
            used += len(excerpt)

        return {
            "keyword": keyword,
            "found": True,
            "total_chars": used,
            "section_count": len(out_sections),
            "sections": out_sections,
        }

    def to_topic_markdown(self, keyword: str, *, max_chars: int = 4000) -> str:
        """extract_topic 的 Markdown 渲染便捷方法。"""
        t = self.extract_topic(keyword, max_chars=max_chars)
        if not t["found"]:
            return f"(未找到关于 \"{keyword}\" 的内容)"
        lines = [f"# 关于 \"{keyword}\" 的摘要", ""]
        for s in t["sections"]:
            lines.append(f"{'#' * (s['level'] + 1)} {s['heading']}")
            lines.append("")
            lines.append(s["excerpt"])
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        """转为 Markdown 格式。"""
        lines = []
        if self.title:
            lines.append(f"# {self.title}\n")
        if self.author or self.publish_date:
            meta = " | ".join(filter(None, [self.author, self.publish_date]))
            lines.append(f"*{meta}*\n")
        for section in self.sections:
            if section.get("heading"):
                level = section.get("level", 2)
                lines.append(f"{'#' * (level + 1)} {section['heading']}\n")
            for para in section.get("paragraphs", []):
                lines.append(para + "\n")
            for code in section.get("code_blocks", []):
                lines.append(f"```\n{code}\n```\n")
            for table in section.get("tables", []):
                lines.append(table + "\n")
            for image in section.get("images", []):
                alt = image.get("alt") or image.get("caption") or "image"
                src = image.get("src", "")
                if src:
                    lines.append(f"![{alt}]({src})\n")
        return "\n".join(lines)


@dataclass
class InterfaceSummary:
    """页面接口摘要。"""
    search_boxes: list[dict] = field(default_factory=list)
    buttons: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    navigation: list[dict] = field(default_factory=list)
    filters: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        lines = []
        if self.search_boxes:
            lines.append(f"搜索框: {len(self.search_boxes)} 个")
        if self.buttons:
            lines.append(f"按钮: {len(self.buttons)} 个")
        if self.forms:
            lines.append(f"表单: {len(self.forms)} 个")
        if self.navigation:
            lines.append(f"导航: {len(self.navigation)} 项")
        return " | ".join(lines) if lines else "无可操作接口"


class ContentExtractor:
    """
    正文提取器 — Readability 思路 + DOM 分析。
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    async def extract_article(self) -> ArticleContent:
        """提取文章内容。使用轻量 Readability 分数选择主内容容器。"""
        raw = await self.page.evaluate(r"""() => {
            const cleanText = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const isNoise = (el) => !!el.closest('nav, footer, aside, header, [role="navigation"], .sidebar, .menu, .nav, .footer, .advertisement, .ads');
            const textLen = (el) => cleanText(el.textContent).length;
            const linkDensity = (el) => {
                const text = Math.max(textLen(el), 1);
                const linkText = Array.from(el.querySelectorAll('a')).reduce((n, a) => n + textLen(a), 0);
                return linkText / text;
            };
            const tableToMarkdown = (table) => {
                const rows = Array.from(table.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th,td')).map(td => cleanText(td.textContent))
                ).filter(r => r.length);
                if (!rows.length) return '';
                const head = rows[0];
                const sep = head.map(() => '---');
                const body = rows.slice(1);
                return [head, sep, ...body].map(r => '| ' + r.join(' | ') + ' |').join('\n');
            };

            const result = {title: '', author: '', date: '', sections: [], code_blocks: [], tables: [], images: []};
            const titleEl = document.querySelector('article h1, main h1, [role="main"] h1, h1') || document.querySelector('meta[property="og:title"]');
            result.title = titleEl?.content || cleanText(titleEl?.textContent) || document.title || '';

            const author = document.querySelector('[rel="author"], .author, .byline, [itemprop="author"], .post-author, article [class*="author"], main [class*="author"]');
            if (author) result.author = cleanText(author.textContent);
            const date = document.querySelector('time, [datetime], .date, .published, [itemprop="datePublished"], .post-date');
            if (date) result.date = date.getAttribute('datetime') || cleanText(date.textContent);

            const preferred = document.querySelector('article, main, [role="main"], .post-content, .article-content, .entry-content, #content');
            const candidates = [preferred, ...document.querySelectorAll('article, main, [role="main"], section, div')].filter(Boolean);
            let container = preferred || document.body;
            let bestScore = -1;
            for (const el of candidates) {
                if (isNoise(el)) continue;
                const pCount = el.querySelectorAll('p').length;
                const headingCount = el.querySelectorAll('h1,h2,h3').length;
                const codeCount = el.querySelectorAll('pre, code').length;
                const len = textLen(el);
                if (len < 80) continue;
                const score = len + pCount * 120 + headingCount * 80 + codeCount * 60 - linkDensity(el) * len * 1.5;
                if (score > bestScore) { bestScore = score; container = el; }
            }

            let currentSection = {heading: '', level: 2, paragraphs: [], code_blocks: [], tables: [], images: []};
            const pushSection = () => {
                if (currentSection.heading || currentSection.paragraphs.length || currentSection.code_blocks.length || currentSection.tables.length || currentSection.images.length) {
                    result.sections.push(currentSection);
                }
            };
            const nodes = container.querySelectorAll('h2,h3,h4,p,pre,blockquote,ul,ol,table,figure,img');
            const seen = new WeakSet();
            for (const el of nodes) {
                if (isNoise(el) || seen.has(el)) continue;
                const tag = el.tagName.toLowerCase();
                const text = cleanText(el.textContent);
                if (tag.match(/^h[234]$/)) {
                    pushSection();
                    currentSection = {heading: text, level: parseInt(tag[1]), paragraphs: [], code_blocks: [], tables: [], images: []};
                    seen.add(el);
                    continue;
                }
                if (tag === 'pre') {
                    if (text.length >= 2) {
                        result.code_blocks.push(text);
                        currentSection.code_blocks.push(text);
                    }
                    seen.add(el);
                    continue;
                }
                if (tag === 'table') {
                    const md = tableToMarkdown(el);
                    if (md) {
                        result.tables.push(md);
                        currentSection.tables.push(md);
                    }
                    seen.add(el);
                    continue;
                }
                if (tag === 'img') {
                    if (el.closest('figure')) continue;
                    const src = el.currentSrc || el.src || el.getAttribute('src') || '';
                    const alt = el.getAttribute('alt') || '';
                    const caption = cleanText(el.closest('figure')?.querySelector('figcaption')?.textContent || '');
                    if (src && (alt || caption)) {
                        const img = {src, alt, caption};
                        result.images.push(img);
                        currentSection.images.push(img);
                    }
                    seen.add(el);
                    continue;
                }
                if (tag === 'figure') {
                    const imgEl = el.querySelector('img');
                    const src = imgEl?.currentSrc || imgEl?.src || imgEl?.getAttribute('src') || '';
                    const alt = imgEl?.getAttribute('alt') || '';
                    const caption = cleanText(el.querySelector('figcaption')?.textContent || '');
                    if (src && (alt || caption)) {
                        const img = {src, alt, caption};
                        result.images.push(img);
                        currentSection.images.push(img);
                    }
                    seen.add(el);
                    continue;
                }
                if (tag === 'blockquote' && text.length >= 2) {
                    currentSection.paragraphs.push('> ' + text);
                    seen.add(el);
                    continue;
                }
                if ((tag === 'ul' || tag === 'ol') && text.length >= 2) {
                    const items = Array.from(el.querySelectorAll(':scope > li')).map(li => cleanText(li.textContent)).filter(Boolean);
                    if (items.length) currentSection.paragraphs.push(items.map(i => `- ${i}`).join('\n'));
                    seen.add(el);
                    continue;
                }
                if (tag === 'p' && text.length >= 20) {
                    currentSection.paragraphs.push(text);
                    seen.add(el);
                }
            }
            pushSection();
            const allText = result.sections.flatMap(s => [s.heading, ...s.paragraphs, ...s.code_blocks]).join(' ');
            result.text_length = allText.length;
            // 真实词数: 按空白分词; 兼容 CJK (CJK 不分词所以单字计数, 跟英文混排仍可用)
            result.word_count = allText.trim() ? allText.trim().split(/\s+/).filter(Boolean).length : 0;
            result.container_score = bestScore;
            return result;
        }""")

        article = ArticleContent(
            title=raw.get("title", ""),
            author=raw.get("author", ""),
            publish_date=raw.get("date", ""),
            code_blocks=raw.get("code_blocks", []),
            tables=raw.get("tables", []),
            images=raw.get("images", []),
            sections=raw.get("sections", []),
            word_count=int(raw.get("word_count", 0)),
            text_length=int(raw.get("text_length", 0)),
        )

        conf = 0.0
        if article.title:
            conf += 0.25
        total_paras = sum(len(s.get("paragraphs", [])) for s in article.sections)
        if total_paras >= 3:
            conf += 0.35
        elif total_paras >= 1:
            conf += 0.15
        if article.author or article.publish_date:
            conf += 0.15
        if article.code_blocks:
            conf += 0.1
        if article.tables or article.images:
            conf += 0.1
        # 用 word_count (真实词数) 判长文, 避免 CJK 长文本被低估
        if article.word_count >= 500:
            conf += 0.1
        article.extraction_confidence = min(conf, 1.0)

        logger.info(
            "Article extracted: '%s' (%d sections, %d chars, conf=%.0f%%)",
            article.title[:50], len(article.sections),
            article.word_count, article.extraction_confidence * 100,
        )
        return article

    async def extract_interfaces(self) -> InterfaceSummary:
        """提取页面可交互接口。"""
        raw = await self.page.evaluate("""() => {
            const result = {
                search_boxes: [], buttons: [], forms: [],
                navigation: [], filters: [],
            };

            // 搜索框
            document.querySelectorAll(
                'input[type="search"], [role="searchbox"], input[placeholder*="search" i], input[placeholder*="搜索"]'
            ).forEach(el => {
                result.search_boxes.push({
                    placeholder: el.getAttribute('placeholder') || '',
                    label: el.getAttribute('aria-label') || '',
                });
            });

            // 按钮
            document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(el => {
                const text = (el.textContent || el.getAttribute('value') || '').trim();
                if (text && text.length < 50) {
                    result.buttons.push({text});
                }
            });

            // 表单
            document.querySelectorAll('form').forEach(form => {
                const inputs = Array.from(form.querySelectorAll('input, select, textarea'));
                result.forms.push({
                    action: form.getAttribute('action') || '',
                    method: (form.getAttribute('method') || 'GET').toUpperCase(),
                    fields: inputs.map(i => ({
                        type: i.getAttribute('type') || i.tagName.toLowerCase(),
                        name: i.getAttribute('name') || '',
                        placeholder: i.getAttribute('placeholder') || '',
                    })),
                });
            });

            // 导航
            document.querySelectorAll('nav a, [role="navigation"] a').forEach(el => {
                const text = el.textContent.trim();
                const href = el.getAttribute('href') || '';
                if (text && text.length < 50) {
                    result.navigation.push({text, href});
                }
            });

            // 筛选器（select、checkbox 组）
            document.querySelectorAll('select, .filter, [class*="filter"]').forEach(el => {
                if (el.tagName === 'SELECT') {
                    const options = Array.from(el.querySelectorAll('option')).map(o => o.textContent.trim());
                    result.filters.push({
                        label: el.previousElementSibling?.textContent?.trim() || '',
                        options: options.slice(0, 10),
                    });
                }
            });

            return result;
        }""")

        summary = InterfaceSummary(
            search_boxes=raw.get("search_boxes", []),
            buttons=raw.get("buttons", [])[:20],  # 限制数量
            forms=raw.get("forms", []),
            navigation=raw.get("navigation", [])[:20],
            filters=raw.get("filters", []),
        )
        logger.info("Interfaces: %s", summary.summary())
        return summary
