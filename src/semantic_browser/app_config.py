"""集中环境配置 — super_plan Round 3a.

所有 `os.getenv` 访问从各模块收拢到这里。模块不再直接读环境变量,
而是调用本模块的 accessor。好处:

- env 变量名 / fallback 链只在一处定义, 不再散落 12 个文件
- 测试可以 monkeypatch 本模块函数, 不用逐个 mock env
- 未来加 config 文件 (~/.semantic-browser/config.yaml) 只改这一处

兼容性: env 变量名和 fallback 优先级与收拢前完全一致。
"""
from __future__ import annotations

import os

_TRUTHY = ("1", "true", "yes", "on", "y", "t")


def _first_env(*names: str, default: str = "") -> str:
    """按顺序返回第一个非空 env 值; 都没设返 default."""
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def env_bool(name: str, *, default: bool = False) -> bool:
    """canonical bool 解析 — '1'/'true'/'yes'/'on' True; 其余 False.

    T117 audit fix 的统一实现 (原 cli/main.py:_env_bool).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


# ── LLM provider credentials ─────────────────────────────────────────

def llm_api_key(provider: str) -> str:
    """LLM_API_KEY 通用覆盖 → provider 专属 key."""
    if provider == "openai":
        return _first_env("LLM_API_KEY", "OPENAI_API_KEY")
    if provider == "anthropic":
        # Claude Code 用 ANTHROPIC_AUTH_TOKEN (不是 ANTHROPIC_API_KEY)
        return _first_env("LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if provider == "gemini":
        return _first_env("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    return _first_env("LLM_API_KEY")


def llm_base_url(provider: str) -> str:
    """LLM_BASE_URL 通用覆盖 → provider 专属 base → provider 默认."""
    if provider == "openai":
        return _first_env(
            "LLM_BASE_URL", "OPENAI_BASE_URL",
            default="https://api.deepseek.com/v1",
        )
    if provider == "anthropic":
        return _first_env(
            "LLM_BASE_URL", "ANTHROPIC_BASE_URL",
            default="https://api.anthropic.com",
        )
    if provider == "gemini":
        return _first_env(
            "LLM_BASE_URL",
            default="https://generativelanguage.googleapis.com",
        )
    return _first_env("LLM_BASE_URL")


def llm_model_tier(tier: str) -> str | None:
    """LLM_MODEL_CHEAP / LLM_MODEL_MEDIUM / LLM_MODEL_SMART."""
    return os.getenv(f"LLM_MODEL_{tier.upper()}") or None


def anthropic_model() -> str | None:
    """ANTHROPIC_MODEL — Claude Code 全局单一 model id 兜底."""
    return os.getenv("ANTHROPIC_MODEL") or None


def openai_model() -> str | None:
    return os.getenv("OPENAI_MODEL") or None


def openai_base_url() -> str | None:
    """OPENAI_BASE_URL 优先, OPENAI_API_BASE 兼容旧名."""
    return _first_env("OPENAI_BASE_URL", "OPENAI_API_BASE") or None


def openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "")


def anthropic_api_key() -> str:
    return _first_env("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def anthropic_base_url() -> str:
    return os.getenv("ANTHROPIC_BASE_URL", "")


def gemini_api_key() -> str:
    return _first_env("GEMINI_API_KEY", "GOOGLE_API_KEY")
