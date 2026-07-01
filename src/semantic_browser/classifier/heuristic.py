"""
Page Classifier — 页面类型自动识别。

基于启发式规则，不依赖 LLM。
识别: article, list, search, login, docs, forum, dashboard, error, video, unknown。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from semantic_browser.snapshot.engine import PageSnapshot

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    """页面分类结果。"""
    page_type: str
    confidence: float
    reason: str
    signals: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_type": self.page_type,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "signals": self.signals,
        }


class PageClassifier:
    """
    启发式页面分类器。

    评分机制：每个页面类型有一组信号检测器，命中信号得分，
    取最高分的类型。阈值以下判为 unknown。
    """

    def classify(self, snapshot: PageSnapshot) -> ClassificationResult:
        signals = self._collect_signals(snapshot)
        scores = self._score_by_type(signals)

        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        total = sum(scores.values()) or 1
        confidence = min(best_score / max(total, 1), 1.0)

        reason = self._build_reason(best_type, signals)
        hit_signals = [s for s, hit in signals.items() if hit]

        result = ClassificationResult(
            page_type=best_type if confidence >= 0.25 else "unknown",
            confidence=confidence,
            reason=reason,
            signals=hit_signals,
        )
        logger.info("Classified %s as %s (%.0f%%)",
                     snapshot.url, result.page_type, confidence * 100)
        return result

    def _collect_signals(self, s: PageSnapshot) -> dict[str, bool]:
        """收集所有信号。返回 signal_name -> bool。"""
        url_lower = s.url.lower()
        title_lower = s.title.lower()
        meta_desc = s.meta.get("description", "").lower()
        meta_og_type = s.meta.get("og:type", "").lower()

        # URL 信号
        has_article_url = bool(re.search(
            r"/(article|post|blog|news)/\d+|/\d{4}/\d{2}/|/p/\d+", url_lower
        ))
        has_search_url = "search" in url_lower or "query=" in url_lower or "q=" in url_lower
        has_login_url = bool(re.search(
            r"/(login|signin|auth|account/login)", url_lower
        ))
        has_docs_url = bool(re.search(r"/(docs?|api|guide|tutorial|reference)", url_lower))
        has_list_url = bool(re.search(r"/(list|category|tag|archive|page/\d+)", url_lower))
        has_error_url = bool(re.search(r"/(404|500|error|not-found)", url_lower))
        has_dashboard_url = bool(re.search(r"/(dashboard|admin|panel|console|settings)", url_lower))
        has_video_url = "watch" in url_lower or "/video/" in url_lower or "youtube" in s.domain

        # 内容信号
        text_combined = " ".join(b.text.lower() for b in s.text_blocks[:50])

        # 仅取前 5 个文本块做 "主要信号" 判断, 避免埋在代码示例 / 长正文里的词污染分类
        headline_text = " ".join(b.text.lower() for b in s.text_blocks[:5])
        has_article_content = any(
            b.tag in ("h1", "h2") for b in s.text_blocks
        ) and len([b for b in s.text_blocks if b.tag == "p"]) >= 3

        has_login_form = any(c.kind == "password" for c in s.controls) or (
            any(c.kind == "textbox" and "user" in c.label.lower() for c in s.controls)
            and any(c.kind == "password" for c in s.controls)
        )

        has_search_box = any(c.kind == "searchbox" for c in s.controls)

        has_code_blocks = len([b for b in s.text_blocks if b.tag in ("code", "pre")]) >= 2

        has_many_links = len(s.links) > 30
        has_list_structure = len([b for b in s.text_blocks if b.tag == "li"]) > 10

        has_video_element = bool(re.search(r"video|player|youtube|vimeo", headline_text))

        # 仅在 title / 头条 h1 / 前 5 个块里出现 "404/not found/error" 才算 error 页;
        # 这样代码块里的 "Traceback ...Error" 不会触发误报。
        has_error_content = any(
            kw in title_lower or kw in headline_text
            for kw in ["not found", "404", "page not found", "does not exist"]
        )

        # dashboard / overview 等关键词容易在 footer/nav 里出现; 限制到 headline
        has_dashboard_content = any(
            kw in title_lower or kw in headline_text
            for kw in ["dashboard", "admin panel", "control panel"]
        )

        # meta 信号
        is_og_article = meta_og_type == "article"

        return {
            "article_url": has_article_url,
            "search_url": has_search_url,
            "login_url": has_login_url,
            "docs_url": has_docs_url,
            "list_url": has_list_url,
            "error_url": has_error_url,
            "dashboard_url": has_dashboard_url,
            "video_url": has_video_url,
            "article_content": has_article_content,
            "login_form": has_login_form,
            "search_box": has_search_box,
            "code_blocks": has_code_blocks,
            "many_links": has_many_links,
            "list_structure": has_list_structure,
            "video_element": has_video_element,
            "error_content": has_error_content,
            "dashboard_content": has_dashboard_content,
            "og_article": is_og_article,
        }

    def _score_by_type(self, signals: dict[str, bool]) -> dict[str, float]:
        """根据信号给每个类型打分。"""
        s = signals
        return {
            "article": (
                (2.0 if s["article_content"] else 0) +
                (1.0 if s["article_url"] else 0) +
                (1.0 if s["og_article"] else 0) +
                (0.5 if s["code_blocks"] else 0)
            ),
            "search": (
                (2.0 if s["search_box"] else 0) +
                (1.5 if s["search_url"] else 0) +
                (0.5 if s["many_links"] else 0)
            ),
            "login": (
                (3.0 if s["login_form"] else 0) +
                (1.5 if s["login_url"] else 0)
            ),
            "docs": (
                (2.0 if s["docs_url"] else 0) +
                (1.5 if s["code_blocks"] else 0) +
                (1.0 if s["article_content"] else 0)
            ),
            "list": (
                (2.0 if s["list_structure"] else 0) +
                (1.0 if s["many_links"] else 0) +
                (0.5 if s["list_url"] else 0)
            ),
            "error": (
                (2.5 if s["error_content"] else 0) +
                (2.0 if s["error_url"] else 0)
            ),
            "dashboard": (
                (2.0 if s["dashboard_content"] else 0) +
                (1.5 if s["dashboard_url"] else 0)
            ),
            "video": (
                (2.5 if s["video_element"] else 0) +
                (1.0 if s["video_url"] else 0)
            ),
        }

    def _build_reason(self, page_type: str, signals: dict[str, bool]) -> str:
        """生成人类可读的分类理由。"""
        type_reasons = {
            "article": "页面包含标题、多段正文和可能的代码块",
            "search": "页面包含搜索框和搜索相关 URL 参数",
            "login": "页面包含密码输入框和登录表单",
            "docs": "页面 URL 路径含 docs/api/guide 且有代码块",
            "list": "页面有大量列表项和链接，呈现列表结构",
            "error": "页面显示错误信息或错误状态码",
            "dashboard": "页面包含 dashboard/overview/settings 等内容",
            "video": "页面包含视频播放器或视频平台标识",
            "unknown": "未命中足够强的类型信号",
        }
        return type_reasons.get(page_type, "无法确定页面类型")
