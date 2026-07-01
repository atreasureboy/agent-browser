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
    kind: str  # button, textbox, searchbox, select, checkbox, link, textarea, tab, menuitem, hidden, file_upload
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
    # T42a: hidden 字段的 value (CSRF token / _token / state 等). 默认空 (非 hidden 时无意义).
    value: str = ""
    # T42h: <input type=file> 的 accept / multiple (上传限制, 安全审计关键).
    accept: str = ""
    multiple: bool = False


@dataclass
class FormInfo:
    """T42a: 表单元数据 — 字段汇总 + 分类 + 隐藏字段全集.

    classification 推断:
      - "login"    — 出现 password 字段 + username/email 字段
      - "search"   — 出现 search 类型或 name 含 q/query/search
      - "upload"   — 出现 type=file 字段 (enctype=multipart/form-data)
      - "signup"   — 出现 password + password confirmation
      - "contact"  — 出现 textarea + email 字段
      - "checkout" — 出现信用卡/地址/cvv 字段
      - "unknown"  — 其它
    """
    form_id: str = ""           # <form id="...">
    action: str = ""            # 截断到 200 字符
    method: str = ""            # get/post (默认 get)
    enctype: str = ""           # multipart/form-data / application/x-www-form-urlencoded
    field_count: int = 0
    hidden_fields: list[dict[str, str]] = field(default_factory=list)  # [{name, value, type}]
    input_names: list[str] = field(default_factory=list)              # 所有 input/select/textarea name
    classification: str = "unknown"


@dataclass
class ScriptInfo:
    """T39: <script> 标签信息. normal=只列 src; deep=也抓源码."""
    src: str = ""            # <script src="..."> 绝对 URL
    inline: str = ""         # 内联 JS 内容 (deep 模式, 截断)
    has_src: bool = False
    # T42d: SRI (Subresource Integrity) 检查
    has_integrity: bool = False
    integrity_hash: str = ""  # 截断到 100 字符
    # T42d: mixed content 检查 (HTTPS 页面加载 HTTP subresource)
    is_mixed_content: bool = False


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
    # T42a: form 分类 + 隐藏字段全集 — agent 登录 / 提交表单 / 找 CSRF 必需.
    forms: list[FormInfo] = field(default_factory=list)

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
        # T42d: SRI / mixed content summary
        ext = self.sri_summary()
        if ext:
            lines.append(ext)
        return "\n".join(lines)

    def sri_summary(self) -> str:
        """T42d: SRI coverage + mixed content 摘要.
        返回: "SRI: 2/3 (66.7%) | Mixed: 1 (http://cdn.example.com/script.js)" 或空字符串.
        """
        external = [s for s in self.scripts if s.has_src]
        if not external:
            return ""
        with_sri = sum(1 for s in external if s.has_integrity)
        mixed = [s.src for s in external if s.is_mixed_content]
        total = len(external)
        pct = (with_sri / total * 100) if total else 0
        result = f"SRI: {with_sri}/{total} ({pct:.1f}%)"
        if mixed:
            sample = ", ".join(mixed[:3])
            result += f" | Mixed: {len(mixed)} ({sample})"
        return result


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
        links, controls, forms = await self._extract_interactive(base_url or url, detail_level)
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
            forms=forms,                                 # T42a
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

    async def _extract_interactive(self, base_url: str, detail_level: str = "normal") -> tuple[list[LinkInfo], list[ControlInfo], list[FormInfo]]:
        """提取可操作元素并写入稳定 ref。

        Playwright Python 的 accessibility snapshot 不稳定携带 ref，
        因此这里在 DOM 元素上打 `data-sb-ref=eN`，controller 使用同一属性定位。
        这样 agent 看到的 ref 与 click/type 消费的 ref 是同一套编号。

        T39: 同时收集 form metadata (action/method/input_name) — default 模式.
              detail_level="deep" 时还填 raw_attrs + outerHTML.
        T40h: 穿透 shadow DOM (closed mode 除外) — 用 TreeWalker + 自定义 walk.
        T42a: hidden 字段也抓 value (CSRF token 关键), 分类 form (login/search/upload/...).
        T42h: <input type=file> 标 kind="file_upload" + accept/multiple 属性.
        """
        base_domain = urlparse(base_url).netloc
        raw = await self.page.evaluate(
            """(args) => {
            const [baseDomain, deep] = args;
            const links = [];
            const controls = [];
            const formsMap = new Map();
            const seenLinks = new Set();
            const controlSelector = [
                'button', 'input', 'select', 'textarea',
                '[role="button"]', '[role="tab"]', '[role="menuitem"]',
                '[role="checkbox"]', '[role="switch"]', '[role="combobox"]',
                '[role="searchbox"]', '[contenteditable="true"]',
                'a[role="button"]', 'summary',
            ].join(', ');

            // T40h: 递归 walk — 穿透 shadow DOM (open 模式).
            const visit = (root, fn) => {
                if (!root) return;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null, false);
                let n = walker.currentNode || root;
                while (n) {
                    fn(n);
                    if (n.shadowRoot) {
                        visit(n.shadowRoot, fn);
                    }
                    n = walker.nextNode();
                }
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
            // T42a: 收集 form 数据 (按 form 自身的引用聚合)
            const formKey = (f) => {
                if (!f) return '__no_form__';
                if (f.id) return 'id:' + f.id;
                if (f.getAttribute('name')) return 'name:' + f.getAttribute('name');
                return 'pos:' + Array.from(document.querySelectorAll('form')).indexOf(f);
            };
            const getOrInitForm = (f) => {
                if (!f) return null;
                const k = formKey(f);
                if (!formsMap.has(k)) {
                    formsMap.set(k, {
                        form_id: f.id || '',
                        action: (f.getAttribute('action') || '').substring(0, 200),
                        method: (f.getAttribute('method') || 'get').toLowerCase(),
                        enctype: f.getAttribute('enctype') || '',
                        hidden_fields: [],
                        input_names: [],
                        field_count: 0,
                        _has_password: false,
                        _has_email: false,
                        _has_textarea: false,
                        _has_file: false,
                        _has_credit: false,
                        _has_address: false,
                        _has_search_input: false,
                        _has_password_confirm: false,
                    });
                }
                return formsMap.get(k);
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
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                const role = el.getAttribute('role') || '';
                const name = (el.getAttribute('name') || '').toLowerCase();

                // T42a: hidden 字段不要求 visible — CSRF token 必须抓到, 即使 display:none
                // 其它字段仍然要求 visible.
                if (type !== 'hidden' && !visible(el)) return;

                // T42a: form 元数据 + 字段类型聚合 (login/search/upload 分类用)
                const formEl = el.closest('form');
                const formInfo = getOrInitForm(formEl);
                if (formInfo) {
                    formInfo.field_count++;
                    if (name) formInfo.input_names.push(name);
                    if (tag === 'input' && type === 'password') formInfo._has_password = true;
                    if (tag === 'input' && type === 'email') formInfo._has_email = true;
                    if (tag === 'input' && type === 'file') formInfo._has_file = true;
                    if (tag === 'input' && type === 'search') formInfo._has_search_input = true;
                    if (name && /password.*confirm|confirm.*password|password2/.test(name)) {
                        formInfo._has_password_confirm = true;
                    }
                    if (tag === 'textarea') formInfo._has_textarea = true;
                    if (name && /(credit|card|cvv|ccv|cardnum)/.test(name)) formInfo._has_credit = true;
                    if (name && /(address|zip|postal|street|city|state|country)/.test(name)) formInfo._has_address = true;
                    if (name && /^(q|query|s|search|keyword)$/.test(name)) formInfo._has_search_input = true;
                    // 收集 hidden 字段 — value 是关键 (CSRF token)
                    if (type === 'hidden' && el.getAttribute('name')) {
                        formInfo.hidden_fields.push({
                            name: el.getAttribute('name'),
                            value: (el.getAttribute('value') || '').substring(0, 500),
                            type: 'hidden',
                        });
                    }
                }

                const label = (
                    el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') ||
                    (el.textContent || '').trim() ||
                    el.getAttribute('name') ||
                    ''
                ).substring(0, 100);

                let kind = 'button';
                if (tag === 'input') {
                    if (type === 'hidden') kind = 'hidden';
                    else if (type === 'search' || role === 'searchbox') kind = 'searchbox';
                    else if (type === 'checkbox') kind = 'checkbox';
                    else if (type === 'radio') kind = 'radio';
                    else if (type === 'email') kind = 'email';
                    else if (type === 'password') kind = 'password';
                    else if (type === 'url') kind = 'url';
                    else if (type === 'file') kind = 'file_upload';  // T42h
                    else kind = 'textbox';
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

                let formAction = '', formMethod = '', formId = '', formParams = {};
                if (formEl) {
                    formAction = (formEl.getAttribute('action') || '').substring(0, 200);
                    formMethod = (formEl.getAttribute('method') || 'get').toLowerCase();
                    formId = formEl.getAttribute('id') || '';
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
                    // T42a: hidden value (CSRF token)
                    value: (el.getAttribute('value') || '').substring(0, 500),
                    // T42h: file upload 限制
                    accept: el.getAttribute('accept') || '',
                    multiple: el.hasAttribute('multiple'),
                });
            });
            // T42a: form 分类
            const forms = [];
            for (const f of formsMap.values()) {
                let classification = 'unknown';
                if (f._has_password && f._has_password_confirm) classification = 'signup';
                else if (f._has_password) classification = 'login';
                else if (f._has_file) classification = 'upload';
                else if (f._has_credit || f._has_address) classification = 'checkout';
                else if (f._has_textarea && (f._has_email || /contact|message/.test(f.input_names.join(',')))) classification = 'contact';
                else if (f._has_search_input || f.method === 'get' && f.input_names.some(n => /^(q|query|s|search|keyword)$/.test(n))) classification = 'search';
                const { _has_password, _has_email, _has_textarea, _has_file, _has_credit, _has_address, _has_search_input, _has_password_confirm, ...rest } = f;
                forms.push({...rest, classification});
            }
            return {links, controls, forms};
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
                # T42a: hidden value
                value=c.get("value", ""),
                # T42h: file upload 限制
                accept=c.get("accept", ""),
                multiple=c.get("multiple", False),
            )
            for c in raw.get("controls", [])
        ]
        forms = [
            FormInfo(
                form_id=f.get("form_id", ""),
                action=f.get("action", ""),
                method=f.get("method", ""),
                enctype=f.get("enctype", ""),
                field_count=f.get("field_count", 0),
                hidden_fields=f.get("hidden_fields", []),
                input_names=f.get("input_names", []),
                classification=f.get("classification", "unknown"),
            )
            for f in raw.get("forms", [])
        ]
        return links, controls, forms

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
        T42d: 同时收集 integrity 属性 (SRI) + 检查 mixed content (HTTPS 页面 HTTP 资源).
        """
        try:
            scripts_raw = await self.page.evaluate(
                """(deep) => {
                    const out = [];
                    const pageProto = location.protocol;
                    for (const s of document.querySelectorAll('script')) {
                        const src = s.getAttribute('src') || '';
                        const absSrc = src ? new URL(src, location.href).href : '';
                        let inline = '';
                        if (deep && !src) {
                            inline = (s.textContent || '').substring(0, 2000);
                        }
                        // T42d: SRI
                        const integrity = s.getAttribute('integrity') || '';
                        // T42d: mixed content (HTTPS 页面加载 HTTP 脚本)
                        let isMixed = false;
                        if (pageProto === 'https:' && src) {
                            try {
                                isMixed = new URL(src, location.href).protocol === 'http:';
                            } catch {}
                        }
                        out.push({
                            src: absSrc,
                            inline,
                            has_src: !!src,
                            has_integrity: !!integrity,
                            integrity_hash: integrity.substring(0, 100),
                            is_mixed_content: isMixed,
                        });
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
                    has_integrity=s.get("has_integrity", False),
                    integrity_hash=s.get("integrity_hash", ""),
                    is_mixed_content=s.get("is_mixed_content", False),
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
