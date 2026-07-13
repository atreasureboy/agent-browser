"""
LLM Enhanced Classifier — LLM 增强页面分类。

启发式分类置信度低时，调 LLM 二次判断。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

from semantic_browser.classifier.heuristic import (
    PageClassifier,
    ClassificationResult,
)
from semantic_browser.snapshot.engine import PageSnapshot

logger = logging.getLogger(__name__)

# LLM 判断时的页面类型枚举
VALID_TYPES = {
    "article", "list", "search", "login", "docs",
    "forum", "dashboard", "error", "video", "unknown",
}

_SYSTEM_PROMPT = """You are a web page classifier. Given page metadata and content, classify the page type.

Return a JSON object with:
  "page_type": one of article, list, search, login, docs, forum, dashboard, error, video, unknown
  "confidence": float 0.0-1.0
  "reason": brief explanation

Rules:
- article: blog post, news article, story with heading + multiple paragraphs
- docs: technical documentation, API reference, tutorial with code
- search: search results page with search box
- login: authentication page with password field
- list: directory, category listing, tag page with many links
- forum: forum thread, discussion page
- dashboard: admin panel, user dashboard, settings
- error: 404, 500, or other error page
- video: video player page (YouTube, Vimeo, etc.)
- unknown: cannot determine

Output ONLY the JSON, no other text."""


class LLMEnhancedClassifier:
    """
    LLM 增强分类器。

    流程: 启发式分类 → 置信度低 → LLM 二次判断 → 降级保护。

    用法:
        classifier = LLMEnhancedClassifier(threshold=0.5)
        result = await classifier.classify(snapshot)
    """

    def __init__(
        self,
        threshold: float = 0.5,
        enable_llm: bool = True,
        model: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self.heuristic = PageClassifier()
        self.threshold = threshold
        self.enable_llm = enable_llm
        self.timeout = timeout
        # model 优先用显式传入，其次读环境变量，都没有则标记不可用
        self.model = model or os.getenv("OPENAI_MODEL")
        # 兜底用官方 endpoint；私有 endpoint 通过 OPENAI_BASE_URL 注入
        # (兼容旧名 OPENAI_API_BASE)
        self._base_url = (
            os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        )
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        # 也认 Claude Code 风格的 ANTHROPIC_* (让 anthropic provider 也走 LLMService)
        self._anthropic_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN", "")
        self._anthropic_base = os.getenv("ANTHROPIC_BASE_URL", "")
        self._llm_available = (
            (bool(self._api_key) and bool(self._base_url) and bool(self.model))
            or (bool(self._anthropic_key) and bool(self._anthropic_base))
        )

    async def classify(self, snapshot: PageSnapshot, *,
                       re_raise_on_failure: bool = False) -> ClassificationResult:
        """
        分类页面。先启发式，低置信度时 LLM 增强。

        T65.2: re_raise_on_failure=True 时, LLM 调用失败 (连接错 / 401 / 5xx /
        解析错) 直接向上抛, 不再 silent fallback. 调用方自己决定怎么处理.
        默认 False 维持 backward-compat (启发式 silent fallback).
        """
        # Step 1: 启发式分类
        heuristic_result = self.heuristic.classify(snapshot)
        logger.info(
            "Heuristic: %s (%.0f%%) for %s",
            heuristic_result.page_type,
            heuristic_result.confidence * 100,
            snapshot.url,
        )

        # Step 2: 置信度够 → 直接返回
        if not self.enable_llm or heuristic_result.confidence >= self.threshold:
            return heuristic_result

        # Step 3: 置信度低 → LLM 增强
        if not self._llm_available:
            logger.warning("LLM not configured, using heuristic result")
            return heuristic_result

        logger.info(
            "Low confidence (%.0f%% < %.0f%%), trying LLM...",
            heuristic_result.confidence * 100,
            self.threshold * 100,
        )

        try:
            llm_result = await self._llm_classify(snapshot)
            if llm_result:
                logger.info(
                    "LLM: %s (%.0f%%) — %s",
                    llm_result.page_type,
                    llm_result.confidence * 100,
                    llm_result.reason,
                )
                # 合并启发式命中的信号 + llm_enhanced 标记，便于追溯
                merged_signals = list(dict.fromkeys(
                    heuristic_result.signals + ["llm_enhanced"]
                ))
                llm_result.signals = merged_signals
                return llm_result
        except Exception as e:
            logger.warning("LLM classify failed: %s, falling back to heuristic", e)
            # T65.2: strict 模式 → 直接抛, 让调用方决定 (返 LLM_UNAVAILABLE /
            # 重试 / 切到启发式)
            if re_raise_on_failure:
                raise

        return heuristic_result

    async def _llm_classify(self, snapshot: PageSnapshot) -> Optional[ClassificationResult]:
        """调用 LLM 进行分类。

        优先用 LLMService (auto-detect provider, 支持 anthropic / openai / gemini / ollama);
        失败回落到直连 OpenAI-compat endpoint 保持向后兼容 (老的 OPENAI_* 单独设置仍能工作).
        """
        from semantic_browser.llm import LLMService  # 避免循环 import

        user_prompt = self._build_prompt(snapshot)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # Path A: LLMService (走 anthropic/openai/gemini/ollama 全 provider 路由)
        try:
            svc = LLMService(timeout=self.timeout)
            if svc.is_available():
                # 显式 model 优先, 否则用 service 解析出来的 cheap tier
                if self.model and not os.getenv("LLM_MODEL_CHEAP"):
                    svc.model_cheap = self.model
                result = await svc.complete_json(
                    messages, tier="cheap",
                    temperature=0.1, max_tokens=500,
                )
                return self._parse_result(result)
        except Exception as e:
            logger.warning("LLMService path failed: %s; trying direct OpenAI-compat", e)

        # Path B: 直连 OpenAI-compat (legacy 路径, 老的 OPENAI_* 单独配置还能用)
        if not (self._api_key and self._base_url and self.model):
            return None

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 300,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"].strip()
        return self._parse_content(content)

    def _parse_result(self, result: dict) -> "ClassificationResult":
        """从已 parse 的 JSON dict 构造 ClassificationResult."""
        page_type = result.get("page_type", "unknown").lower().strip()
        if page_type not in VALID_TYPES:
            page_type = "unknown"
        confidence = float(result.get("confidence", 0.5))
        confidence = max(0.0, min(1.0, confidence))
        return ClassificationResult(
            page_type=page_type,
            confidence=confidence,
            reason=result.get("reason", "LLM classified"),
            signals=["llm_enhanced"],
        )

    def _parse_content(self, content: str) -> "ClassificationResult":
        """从 LLM 原始文本里剥 markdown 包裹 + parse JSON + 构造结果."""
        if "```" in content:
            match = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if match:
                content = match.group(1).strip()
        return self._parse_result(json.loads(content))

    def _build_prompt(self, snapshot: PageSnapshot) -> str:
        """构建给 LLM 的 prompt。"""
        # 提取前 2000 字文本
        text_parts = []
        total_chars = 0
        for block in snapshot.text_blocks:
            if total_chars + len(block.text) > 2000:
                remaining = 2000 - total_chars
                if remaining > 0:
                    text_parts.append(f"[{block.tag}] {block.text[:remaining]}...")
                break
            text_parts.append(f"[{block.tag}] {block.text}")
            total_chars += len(block.text)

        text_combined = "\n".join(text_parts)

        # 控件列表
        controls_str = ""
        if snapshot.controls:
            ctrl_list = [
                f"  - {c.kind}: {c.label}" for c in snapshot.controls[:15]
            ]
            controls_str = "\n".join(ctrl_list)
        else:
            controls_str = "  (none)"

        # URL 分析
        url_hint = ""
        path = snapshot.url.split("//")[-1].split("/", 1)[-1] if "//" in snapshot.url else ""
        if path:
            url_hint = f"URL path: /{path}"

        return f"""Classify this page:

URL: {snapshot.url}
{url_hint}
Title: {snapshot.title}
Domain: {snapshot.domain}

Content:
{text_combined}

Interactive controls:
{controls_str}

Links count: {len(snapshot.links)}

Return JSON:"""
