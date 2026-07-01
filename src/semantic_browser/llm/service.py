"""
T23: LLMService — 统一的 LLM 调用抽象层, 支持模型分层 (cheap / medium / smart).

设计动机 (来自 T22 后的反思):
  - 不是每个决策都需要最聪明的模型
  - 大量 "智能辅助" 任务 (snapshot 切片 / 文本摘要 / 字段抽取 / ref 重定位)
    用便宜模型就能搞定, 而且更快更省
  - 只有 GoalAgent 这种复杂决策循环才需要强模型

模型分层 (Anthropic 自己的 Haiku/Sonnet/Opus 思路):
  - cheap:   默认用于高频低风险任务 (snapshot 切片, 摘要, 抽取, 校验)
  - medium:  默认用于中等复杂度 (单步决策, 短 prompt 推理)
  - smart:   默认用于复杂决策 (GoalAgent 多步规划, 长 horizon)

环境变量:
  LLM_API_KEY      API key (单 key 多 model 时)
  LLM_BASE_URL     API base URL (默认 https://api.deepseek.com/v1)
  LLM_MODEL_CHEAP  便宜模型 (默认 deepseek-chat)
  LLM_MODEL_MEDIUM 中等模型 (默认 deepseek-chat)
  LLM_MODEL_SMART  强模型  (默认 deepseek-chat — 用户自己改)

用法:
  svc = LLMService()
  result = await svc.complete(
      messages=[{"role": "user", "content": "..."}],
      tier="cheap", max_tokens=300,
  )
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional

import httpx

logger = logging.getLogger(__name__)


Tier = Literal["cheap", "medium", "smart"]


@dataclass
class LLMResponse:
    """统一的 LLM 响应."""
    content: str
    model: str
    tier: str
    usage: dict[str, int]  # prompt_tokens / completion_tokens / total_tokens
    raw: dict[str, Any]


class LLMUnavailableError(RuntimeError):
    """LLM 未配置或不可用."""


class LLMService:
    """统一的 LLM 调用层, 支持 cheap/medium/smart 三档."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_cheap: Optional[str] = None,
        model_medium: Optional[str] = None,
        model_smart: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
        )
        # 三档模型, 默认都用 deepseek-chat (兼容 + 便宜)
        self.model_cheap = (
            model_cheap
            or os.getenv("LLM_MODEL_CHEAP")
            or os.getenv("OPENAI_MODEL", "deepseek-chat")
        )
        self.model_medium = (
            model_medium
            or os.getenv("LLM_MODEL_MEDIUM")
            or os.getenv("OPENAI_MODEL", "deepseek-chat")
        )
        self.model_smart = (
            model_smart
            or os.getenv("LLM_MODEL_SMART")
            or os.getenv("OPENAI_MODEL", "deepseek-chat")
        )
        self.timeout = timeout
        # 统计: 哪个 tier 被调了多少次
        self.call_counts: dict[str, int] = {"cheap": 0, "medium": 0, "smart": 0}

    def is_available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    def model_for(self, tier: Tier) -> str:
        if tier == "cheap":
            return self.model_cheap
        if tier == "medium":
            return self.model_medium
        return self.model_smart

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

        json_mode=True 时让模型返回 JSON (一些 API 支持 response_format).
        """
        if not self.is_available():
            raise LLMUnavailableError(
                "LLM not configured: set LLM_API_KEY + LLM_BASE_URL "
                "(or OPENAI_API_KEY + OPENAI_BASE_URL)"
            )
        model = self.model_for(tier)
        self.call_counts[tier] += 1
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # OpenAI / DeepSeek 都支持 response_format={"type": "json_object"}
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=model,
            tier=tier,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            raw=data,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        tier: Tier = "cheap",
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> dict[str, Any]:
        """便捷: 调用并 parse JSON. 自动剥 ```json ... ``` 包裹.

        Raises:
            LLMUnavailableError: LLM 未配置
            json.JSONDecodeError: 模型没返回有效 JSON
        """
        resp = await self.complete(
            messages, tier=tier, temperature=temperature, max_tokens=max_tokens,
            json_mode=True,
        )
        content = resp.content
        # 部分 API 不严格遵守 json_mode, 兜底再剥一次
        if "```" in content:
            m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
            if m:
                content = m.group(1).strip()
        return json.loads(content)

    def stats(self) -> dict[str, Any]:
        """返回调用统计 (cheap/medium/smart 各自次数)."""
        return {
            "available": self.is_available(),
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