"""
Page Snapshot — 页面语义快照。

从 Playwright aria tree + DOM 提取结构化页面快照。
这是 Agent 看到的"页面对象"，不是原始 HTML。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Iterator
from urllib.parse import urljoin, urlparse

from playwright.async_api import Page

logger = logging.getLogger(__name__)


@dataclass
class LinkInfo:
    """页面链接。"""
    ref: str
    text: str
    href: str
    internal: bool = True  # 是否站内链接
    # T40d: href 拆出的 query 参数 — 审计/重组 URL 用
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class ControlInfo:
    """可操作控件。"""
    ref: str
    kind: str  # button, textbox, searchbox, select, checkbox, link, textarea, tab, menuitem
    label: str
    placeholder: str = ""
    role: str = ""
    # T39: 所在表单的 metadata (默认 detail_level="normal" 时填充 — 注入测试 / 安全审计关键).
    # 在 detail_level="deep" 时还会填充 raw_attrs (HTML 属性全集) + outerHTML 前 200 字符.
    form_action: str = ""     # 所在 <form action>, 截断到 200 字符
    form_method: str = ""     # 所在 <form method>, 默认 "get"
    input_name: str = ""      # <input name="..."> — 提交时字段名
    input_type: str = ""      # <input type="...">
    form_id: str = ""         # <form id="...">
    # T40d: form_action URL 拆出的 query 参数 (e.g. ?redirect=/admin)
    form_params: dict[str, str] = field(default_factory=dict)
    raw_attrs: dict[str, str] = field(default_factory=dict)  # deep 模式: 所有 HTML 属性
    outer_html: str = ""      # deep 模式: 元素 outerHTML (截断到 500 字符)


@dataclass
class ScriptInfo:
    """T39: <script> 标签信息. normal=只列 src; deep=也抓源码."""
    src: str = ""            # <script src="..."> 绝对 URL
    inline: str = ""         # 内联 JS 内容 (deep 模式, 截断)
    has_src: bool = False


@dataclass
class TextBlock:
    """文本块。"""
    tag: str  # h1, h2, p, li, blockquote, code, pre
    text: str
    level: int = 0  # heading level


@dataclass
class PageSnapshot:
    """页面语义快照 — Agent 看到的核心数据结构。"""
    url: str
    title: str
    domain: str
    page_type: str = "unknown"  # 由 Classifier 填充
    text_blocks: list[TextBlock] = field(default_factory=list)
    links: list[LinkInfo] = field(default_factory=list)
    controls: list[ControlInfo] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    raw_aria: str = ""  # 原始 aria snapshot 文本
    # T39: scripts — normal 模式只列 src; deep 模式也抓内联源码
    scripts: list[ScriptInfo] = field(default_factory=list)
    # T39: detail_level 标记 ("normal" | "deep"), 让 caller 知道信息丰富度
    detail_level: str = "normal"
    # T40c: HTML 注释 — 安全审计 (TODO/FIXME/凭据泄漏常见)
    comments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def summary(self) -> str:
        """人类可读摘要。"""
        lines = [
            f"URL:   {self.url}",
            f"Title: {self.title}",
            f"Type:  {self.page_type}",
            f"Blocks: {len(self.text_blocks)} | Links: {len(self.links)} | Controls: {len(self.controls)}",
        ]
        return "\n".join(lines)


class SnapshotEngine:
    """
    语义快照引擎。

    从 Playwright Page 提取结构化数据，不依赖 LLM。
    快照阶段会给可操作 DOM 元素写入 data-sb-ref=eN，
    BrowserController.click / type_text 使用同一属性定位元素。
    """

    # 可操作元素的角色映射
    INTERACTIVE_ROLES = {
        "button", "link", "textbox", "searchbox", "checkbox", "radio",
        "combobox", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
        "tab", "treeitem", "switch", "slider",
    }

    # 文本类标签
    TEXT_TAGS = {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "li", "blockquote", "code", "pre", "td", "th", "figcaption",
        "dt", "dd", "summary",
    }

    def __init__(self, page: Page) -> None:
        self.page = page

    async def capture(self, base_url: str = "", detail_level: str = "normal") -> PageSnapshot:
        """捕获当前页面的语义快照。

        T39: detail_level="normal" (默认) — 轻量, 省 token.
              detail_level="deep"   — 抓 JS 源码 + 完整 outerHTML + 所有 HTML 属性.
                                   agent 在需要审计 / 调试时显式打开.
        """
        url = self.page.url
        title = await self.page.title()
        domain = urlparse(url).netloc

        meta = await self._extract_meta()
        text_blocks = await self._extract_text_blocks()
        links, controls = await self._extract_interactive(base_url or url, detail_level)
        raw_aria = await self._get_raw_aria()
        scripts = await self._extract_scripts(detail_level)
        comments = await self._extract_comments()       # T40c

        snapshot = PageSnapshot(
            url=url,
            title=title,
            domain=domain,
            text_blocks=text_blocks,
            links=links,
            controls=controls,
            scripts=scripts,
            comments=comments,
            detail_level=detail_level,
            meta=meta,
            raw_aria=raw_aria,
        )
        logger.info("Snapshot captured: %s (%d blocks, %d links, %d controls, %d comments)",
                     url, len(text_blocks), len(links), len(controls), len(comments))
        return snapshot

    async def _extract_meta(self) -> dict[str, str]:
        """提取 meta 标签。"""
        return await self.page.evaluate("""() => {
            const meta = {};
            for (const el of document.querySelectorAll('meta[name], meta[property]')) {
                const key = el.getAttribute('name') || el.getAttribute('property');
                const val = el.getAttribute('content');
                if (key && val) meta[key] = val;
            }
            const lang = document.documentElement.getAttribute('lang');
            if (lang) meta['lang'] = lang;
            meta['charset'] = document.characterSet || 'UTF-8';
            return meta;
        }""")

    async def _extract_text_blocks(self) -> list[TextBlock]:
        """提取页面中的文本块。"""
        raw = await self.page.evaluate("""() => {
            const blocks = [];
            const tags = ['h1','h2','h3','h4','h5','h6','p','li','blockquote','pre','code','dt','dd','summary','figcaption'];
            const seen = new WeakSet();

            for (const tag of tags) {
                for (const el of document.querySelectorAll(tag)) {
                    if (el.closest('nav, footer, header[role="banner"], aside, [role="navigation"]')) continue;
                    if (seen.has(el)) continue;
                    let parent = el.parentElement;
                    while (parent) {
                        if (seen.has(parent)) break;
                        parent = parent.parentElement;
                    }
                    const text = el.textContent.trim();
                    if (!text) continue;
                    const level = tag.startsWith('h') ? parseInt(tag[1]) : 0;
                    blocks.push({tag, text: text.substring(0, 2000), level, index: blocks.length});
                    seen.add(el);
                }
            }
            blocks.sort((a, b) => a.index - b.index);
            return blocks;
        }""")

        return [
            TextBlock(tag=b["tag"], text=b["text"], level=b.get("level", 0))
            for b in raw
        ]

    async def _extract_interactive(self, base_url: str, detail_level: str = "normal") -> tuple[list[LinkInfo], list[ControlInfo]]:
        """提取可操作元素并写入稳定 ref。

        Playwright Python 的 accessibility snapshot 不稳定携带 ref，
        因此这里在 DOM 元素上打 `data-sb-ref=eN`，controller 使用同一属性定位。
        这样 agent 看到的 ref 与 click/type 消费的 ref 是同一套编号。

        T39: 同时收集 form metadata (action/method/input_name) — default 模式.
              detail_level="deep" 时还填 raw_attrs + outerHTML.
        T40h: 穿透 shadow DOM (closed mode 除外) — 用 TreeWalker + 自定义 walk.
        """
        base_domain = urlparse(base_url).netloc
        raw = await self.page.evaluate(
            """(args) => {
            const [baseDomain, deep] = args;
            const links = [];
            const controls = [];
            const seenLinks = new Set();
            const controlSelector = [
                'button', 'input', 'select', 'textarea',
                '[role="button"]', '[role="tab"]', '[role="menuitem"]',
                '[role="checkbox"]', '[role="switch"]', '[role="combobox"]',
                '[role="searchbox"]', '[contenteditable="true"]',
                'a[role="button"]', 'summary',
            ].join(', ');

            // T40h: 递归 walk — 穿透 shadow DOM (open 模式).
            // closed shadow root 无法访问, 这是浏览器安全限制.
            const visit = (root, fn) => {
                if (!root) return;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null, false);
                let n = walker.currentNode || root;
                while (n) {
                    fn(n);
                    // 先遍历 light DOM, 再进入 shadow root (避免重复)
                    if (n.shadowRoot) {
                        visit(n.shadowRoot, fn);
                    }
                    n = walker.nextNode();
                }
                // 也遍历 root 自身 (walker.currentNode 默认不会触发 fn)
            };

            let idx = 0;
            const assignRef = (el) => {
                let ref = el.getAttribute('data-sb-ref');
                if (!ref) {
                    ref = `e${++idx}`;
                    el.setAttribute('data-sb-ref', ref);
                }
                return ref;
            };
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                return style.visibility !== 'hidden' && style.display !== 'none';
            };

            // T40h: 改 querySelectorAll 为递归 walk — shadow DOM 内 a[href] 也能拿到.
            visit(document, (el) => {
                if (el.tagName && el.tagName.toLowerCase() === 'a' && el.hasAttribute('href')) {
                    if (!visible(el)) return;
                    const href = el.href;
                    if (!href || href.startsWith('javascript:') || seenLinks.has(href)) return;
                    seenLinks.add(href);
                    let internal = true;
                    try { internal = new URL(href).hostname === baseDomain.split(':')[0]; } catch { internal = false; }
                    let params = {};
                    try {
                        new URL(href, location.href).searchParams.forEach((v, k) => {
                            params[k] = (v || '').substring(0, 200);
                        });
                    } catch {}
                    links.push({
                        ref: assignRef(el),
                        href,
                        text: (el.textContent || '').trim().substring(0, 200),
                        internal,
                        params,
                    });
                }
            });

            visit(document, (el) => {
                if (!el.matches || !el.matches(controlSelector)) return;
                if (!visible(el)) return;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const role = el.getAttribute('role') || '';
                const label = (
                    el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') ||
                    (el.textContent || '').trim() ||
                    el.getAttribute('name') ||
                    ''
                ).substring(0, 100);

                let kind = 'button';
                if (tag === 'input') {
                    kind = type === 'search' || role === 'searchbox' ? 'searchbox'
                         : type === 'checkbox' ? 'checkbox'
                         : type === 'radio' ? 'radio'
                         : type === 'email' ? 'email'
                         : type === 'password' ? 'password'
                         : type === 'url' ? 'url'
                         : 'textbox';
                } else if (tag === 'select' || role === 'combobox' || role === 'listbox') {
                    kind = 'select';
                } else if (tag === 'textarea') {
                    kind = 'textarea';
                } else if (tag === 'a') {
                    kind = 'link';
                } else if (role === 'tab') {
                    kind = 'tab';
                } else if (role === 'menuitem') {
                    kind = 'menuitem';
                } else if (role === 'switch') {
                    kind = 'switch';
                }

                let form = el.closest('form');
                let formAction = '', formMethod = '', formId = '', formParams = {};
                if (form) {
                    formAction = (form.getAttribute('action') || '').substring(0, 200);
                    formMethod = (form.getAttribute('method') || 'get').toLowerCase();
                    formId = form.getAttribute('id') || '';
                    if (formAction) {
                        try {
                            const formUrl = new URL(formAction, location.href);
                            formUrl.searchParams.forEach((v, k) => {
                                formParams[k] = (v || '').substring(0, 200);
                            });
                        } catch {}
                    }
                }

                let rawAttrs = {}, outerHtml = '';
                if (deep) {
                    const attrs = el.attributes;
                    for (let i = 0; i < attrs.length; i++) {
                        rawAttrs[attrs[i].name] = (attrs[i].value || '').substring(0, 200);
                    }
                    outerHtml = (el.outerHTML || '').substring(0, 500);
                }

                controls.push({
                    ref: assignRef(el),
                    kind, label, role,
                    placeholder: el.getAttribute('placeholder') || '',
                    form_action: formAction,
                    form_method: formMethod,
                    form_id: formId,
                    form_params: formParams,
                    input_name: el.getAttribute('name') || '',
                    input_type: el.getAttribute('type') || '',
                    raw_attrs: rawAttrs,
                    outer_html: outerHtml,
                });
            });
            return {links, controls};
        }""", [base_domain, detail_level == "deep"])

        links = []
        for link in raw.get("links", []):
            if not link["href"] or link["href"].startswith("javascript:"):
                continue
            links.append(LinkInfo(
                ref=link["ref"],
                text=link["text"][:200],
                href=link["href"],
                internal=link.get("internal", True),
                params=link.get("params", {}),
            ))
        controls = [
            ControlInfo(
                ref=c["ref"],
                kind=c["kind"],
                label=c["label"],
                placeholder=c.get("placeholder", ""),
                role=c.get("role", ""),
                # T39: form metadata — 默认 detail_level="normal" 时也填 (这是常态需求)
                form_action=c.get("form_action", ""),
                form_method=c.get("form_method", ""),
                form_id=c.get("form_id", ""),
                form_params=c.get("form_params", {}),
                input_name=c.get("input_name", ""),
                input_type=c.get("input_type", ""),
                raw_attrs=c.get("raw_attrs", {}),
                outer_html=c.get("outer_html", ""),
            )
            for c in raw.get("controls", [])
        ]
        return links, controls

    async def _get_raw_aria(self) -> str:
        """获取 Playwright aria snapshot 文本。"""
        try:
            return await self.page.aria_snapshot()
        except Exception:
            return ""

    async def _extract_scripts(self, detail_level: str = "normal") -> list[ScriptInfo]:
        """T39: 抓 <script> 标签.

        normal: 只列 src (让 agent 知道页面加载了哪些 JS — fingerprint 用).
        deep:   也抓 inline 源码 (deep 模式, agent 审计时开).
                src 不抓内容 (太大), 让 agent 用 sb_get_script_source 单独按 URL 抓.
        """
        try:
            scripts_raw = await self.page.evaluate(
                """(deep) => {
                    const out = [];
                    for (const s of document.querySelectorAll('script')) {
                        const src = s.getAttribute('src') || '';
                        const absSrc = src ? new URL(src, location.href).href : '';
                        let inline = '';
                        if (deep && !src) {
                            inline = (s.textContent || '').substring(0, 2000);
                        }
                        out.push({src: absSrc, inline, has_src: !!src});
                    }
                    return out;
                }""",
                detail_level == "deep",
            )
            return [
                ScriptInfo(
                    src=s.get("src", ""),
                    inline=s.get("inline", ""),
                    has_src=s.get("has_src", False),
                )
                for s in scripts_raw
            ]
        except Exception as e:
            logger.warning("_extract_scripts failed: %s", e)
            return []

    async def _extract_comments(self) -> list[str]:
        """T40c: 抓 HTML 注释 — 安全审计常发现 TODO/FIXME/凭据泄漏.

        normal 模式也开 (廉价元数据).
        """
        try:
            return await self.page.evaluate("""() => {
                const out = [];
                const seen = new Set();
                const walk = (root) => {
                    if (!root) return;
                    const it = document.createTreeWalker(
                        root, NodeFilter.SHOW_COMMENT, null, false
                    );
                    let n;
                    while ((n = it.nextNode())) {
                        const t = (n.nodeValue || '').trim();
                        if (t && !seen.has(t)) {
                            seen.add(t);
                            out.push(t.substring(0, 500));
                            if (out.length >= 200) return;
                        }
                    }
                    // T40h: 也进 shadow root
                    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (const el of all) {
                        if (el.shadowRoot) walk(el.shadowRoot);
                    }
                };
                walk(document);
                return out;
            }""")
        except Exception as e:
            logger.warning("_extract_comments failed: %s", e)
            return []
