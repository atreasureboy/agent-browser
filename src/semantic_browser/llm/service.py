"""
T23+ T36: LLMService — 统一的 LLM 调用抽象层, 支持模型分层 (cheap/medium/smart)
+ 多 provider 路由 (openai-compat / Anthropic / Gemini / Ollama).

设计动机:
  - 不是每个决策都需要最聪明的模型 (T23)
  - 不同用户用不同 provider (T36) — 不能 lock 到 DeepSeek

模型分层 (Anthropic 自己的 Haiku/Sonnet/Opus 思路):
  - cheap:   默认用于高频低风险任务 (snapshot 切片, 摘要, 抽取, 校验)
  - medium:  默认用于中等复杂度 (单步决策, 短 prompt 推理)
  - smart:   默认用于复杂决策 (GoalAgent 多步规划, 长 horizon)

Provider:
  - openai (默认 — DeepSeek/OpenAI/Groq/Together 全走这条, OpenAI 兼容)
  - anthropic (Claude 原生 API — /v1/messages)
  - gemini (Google Gemini — :generateContent)
  - ollama (本地 OpenAI 兼容)

环境变量:
  LLM_PROVIDER     openai | anthropic | gemini | ollama (auto-detect)
  LLM_API_KEY      主 API key (provider-aware: 也读 ANTHROPIC_API_KEY / GEMINI_API_KEY)
  LLM_BASE_URL     API base URL (auto 当 provider 不同)
  LLM_MODEL_CHEAP  便宜模型 (默认按 provider 推断)
  LLM_MODEL_MEDIUM 中等模型
  LLM_MODEL_SMART  强模型

用法:
  svc = LLMService()  # auto-detect
  result = await svc.complete(messages=[...], tier="cheap", max_tokens=300)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal, Optional

from semantic_browser.llm.types import LLMResponse, LLMUnavailableError
from semantic_browser.llm.providers import (
    build_provider,
    detect_provider,
    guess_provider_from_model,
    default_model_for,
)

logger = logging.getLogger(__name__)


Tier = Literal["cheap", "medium", "smart"]


# 类型别名方便旧 import:  e.g. `from semantic_browser.llm.service import LLMResponse`
__all__ = ["LLMService", "LLMResponse", "LLMUnavailableError", "Tier",
           "get_default_service", "reset_default_service"]


# 保留旧字段定义引用 — 让 re-export 不丢.


class LLMService:
    """统一的 LLM 调用层, 支持 cheap/medium/smart 三档 + 多 provider."""

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_cheap: Optional[str] = None,
        model_medium: Optional[str] = None,
        model_smart: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        # T36: 显式 provider 优先, 否则 auto-detect from env
        provider_name = (provider or detect_provider()).lower()
        self._provider = build_provider(
            provider_name,
            api_key=api_key, base_url=base_url, timeout=timeout,
        )
        self.provider_name = provider_name

        # 三档 model id — 不显式给就按 provider 默认
        # Claude Code env 里 ANTHROPIC_MODEL 是单一全局值; 用它做兜底让 cheap/medium/smart
        # 三个 tier 都能复用同一模型 (代理通常只服务一个 model id, 比如 MiniMax-M3).
        anthropic_default = os.getenv("ANTHROPIC_MODEL") if provider_name == "anthropic" else None
        self.model_cheap = (
            model_cheap
            or os.getenv("LLM_MODEL_CHEAP")
            or anthropic_default
            or default_model_for(provider_name, "cheap")
        )
        self.model_medium = (
            model_medium
            or os.getenv("LLM_MODEL_MEDIUM")
            or anthropic_default
            or default_model_for(provider_name, "medium")
        )
        self.model_smart = (
            model_smart
            or os.getenv("LLM_MODEL_SMART")
            or anthropic_default
            or default_model_for(provider_name, "smart")
        )
        # 兼容旧字段 — 部分用户代码可能引用 self.base_url / self.api_key
        self.base_url = getattr(self._provider, "base_url", "")
        self.api_key = getattr(self._provider, "api_key", "")
        # 统计
        self.call_counts: dict[str, int] = {"cheap": 0, "medium": 0, "smart": 0}

    # ── 兼容旧 API ────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(self._provider.is_available())

    @property
    def provider(self):
        return self._provider

    def model_for(self, tier: Tier) -> str:
        if tier == "cheap":
            return self.model_cheap
        if tier == "medium":
            return self.model_medium
        return self.model_smart

    # ── 主入口 ─────────────────────────────────────────────

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "cheap",
        temperature: float = 0.2,
        max_tokens: int = 500,
        json_mode: bool = False,
    ) -> LLMResponse:
        """调用 LLM. tier 默认 cheap (高频低风险任务).

        json_mode=True 时让模型返回 JSON (provider-aware 实现, 不支持则降级).
        """
        if not self.is_available():
            raise LLMUnavailableError(
                f"LLM ({self.provider_name}) not configured: "
                f"set LLM_API_KEY or provider-specific key"
            )
        model = self.model_for(tier)
        self.call_counts[tier] += 1
        resp = await self._provider.call(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        # 上层关心 tier, 强行设值
        return LLMResponse(
            content=resp.content,
            model=resp.model,
            tier=tier,
            usage=dict(resp.usage),
            raw=resp.raw,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "cheap",
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> dict[str, Any]:
        # 调 complete 拿 content
        resp = await self.complete(
            messages, tier=tier, temperature=temperature, max_tokens=max_tokens,
            json_mode=True,
        )
        # T112 audit fix: dedup — 之前 inline compile r"```(?:json)?\s*(.*?)```"
        # 跟下面 complete_json_with_fallback + snapshot/vision.py 等 4 处同 regex.
        from semantic_browser.llm.json_utils import loads_json_strip_fence
        return loads_json_strip_fence(resp.content)

    # ── T72: fallback chain ───────────────────────────────────

    async def complete_with_fallback(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "cheap",
        temperature: float = 0.2,
        max_tokens: int = 500,
        json_mode: bool = False,
        fallback_chain: tuple[Tier, ...] | None = None,
    ) -> "LLMResponse":
        """T72: 失败自动升级 tier 重试.

        默认链: cheap → medium → smart. 5xx/超时/network error 升级 tier 重试.
        LLMUnavailableError (key 缺失) 不会因升级而修好, 直接抛.

        Returns:
            LLMResponse (含实际用的 tier).
        """
        import httpx

        chain = fallback_chain or ("cheap", "medium", "smart")
        last_exc: Exception | None = None
        for current_tier in chain:
            try:
                resp = await self.complete(
                    messages,
                    tier=current_tier,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                )
                if current_tier != tier:
                    logger.info(
                        "T72 fallback: tier %s failed, succeeded on %s",
                        tier, current_tier,
                    )
                return resp
            except (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException) as e:
                last_exc = e
                logger.warning(
                    "T72 fallback: tier %s failed (%s), trying next",
                    current_tier, type(e).__name__,
                )
                continue
            except LLMUnavailableError:
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("complete_with_fallback: empty chain")

    async def complete_json_with_fallback(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "cheap",
        temperature: float = 0.1,
        max_tokens: int = 500,
        fallback_chain: tuple[Tier, ...] | None = None,
    ) -> dict[str, Any]:
        """T72: complete_json + 自动 fallback chain."""
        resp = await self.complete_with_fallback(
            messages, tier=tier, temperature=temperature,
            max_tokens=max_tokens, json_mode=True, fallback_chain=fallback_chain,
        )
        # T112 audit fix: dedup — 同 complete_json.
        from semantic_browser.llm.json_utils import loads_json_strip_fence
        return loads_json_strip_fence(resp.content)

    def stats(self) -> dict[str, Any]:
        """返回调用统计 (provider + cheap/medium/smart 各自次数)."""
        return {
            "available": self.is_available(),
            "provider": self.provider_name,
            "models": {
                "cheap": self.model_cheap,
                "medium": self.model_medium,
                "smart": self.model_smart,
            },
            "call_counts": dict(self.call_counts),
        }


# ── 简化全局访问 ─────────────────────────────────────────────────

_default_service: Optional[LLMService] = None


def get_default_service() -> LLMService:
    """获取 (懒初始化) 默认 LLMService. 测试可用 reset_default_service() 重置."""
    global _default_service
    if _default_service is None:
        _default_service = LLMService()
    return _default_service


def reset_default_service() -> None:
    """重置默认 service (测试用)."""
    global _default_service
    _default_service = None
