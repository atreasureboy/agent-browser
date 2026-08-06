"""app_config 集中配置测试 — super_plan Round 3a."""
from __future__ import annotations

import pytest

from semantic_browser import app_config


@pytest.fixture
def clean_env(monkeypatch):
    """清掉所有相关 env, 避免宿主机污染."""
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
              "OPENAI_API_BASE", "OPENAI_MODEL", "ANTHROPIC_API_KEY",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
              "GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_MODEL_CHEAP",
              "LLM_MODEL_MEDIUM", "LLM_MODEL_SMART"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


class TestLlmApiKey:
    def test_generic_key_wins(self, clean_env):
        clean_env.setenv("LLM_API_KEY", "generic")
        clean_env.setenv("OPENAI_API_KEY", "specific")
        assert app_config.llm_api_key("openai") == "generic"

    def test_falls_back_to_specific(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "specific")
        assert app_config.llm_api_key("openai") == "specific"

    def test_anthropic_auth_token_fallback(self, clean_env):
        clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "claude-code-token")
        assert app_config.llm_api_key("anthropic") == "claude-code-token"
        # ANTHROPIC_API_KEY 优先于 AUTH_TOKEN
        clean_env.setenv("ANTHROPIC_API_KEY", "api-key")
        assert app_config.llm_api_key("anthropic") == "api-key"

    def test_gemini_google_fallback(self, clean_env):
        clean_env.setenv("GOOGLE_API_KEY", "goog")
        assert app_config.llm_api_key("gemini") == "goog"
        clean_env.setenv("GEMINI_API_KEY", "gem")
        assert app_config.llm_api_key("gemini") == "gem"

    def test_missing_returns_empty(self, clean_env):
        assert app_config.llm_api_key("openai") == ""


class TestLlmBaseUrl:
    def test_openai_default(self, clean_env):
        assert app_config.llm_base_url("openai") == "https://api.deepseek.com/v1"

    def test_generic_override(self, clean_env):
        clean_env.setenv("LLM_BASE_URL", "http://proxy:9000/v1")
        assert app_config.llm_base_url("anthropic") == "http://proxy:9000/v1"

    def test_anthropic_default(self, clean_env):
        assert app_config.llm_base_url("anthropic") == "https://api.anthropic.com"


class TestEnvBool:
    @pytest.mark.parametrize("val", ["1", "true", "YES", "on", "y", "T"])
    def test_truthy(self, clean_env, val):
        clean_env.setenv("B", val)
        assert app_config.env_bool("B") is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, clean_env, val):
        clean_env.setenv("B", val)
        assert app_config.env_bool("B") is False

    def test_missing_uses_default(self, clean_env):
        assert app_config.env_bool("MISSING", default=True) is True
        assert app_config.env_bool("MISSING", default=False) is False


class TestModelTiers:
    def test_tier_lookup(self, clean_env):
        clean_env.setenv("LLM_MODEL_CHEAP", "mini")
        clean_env.setenv("LLM_MODEL_SMART", "big")
        assert app_config.llm_model_tier("cheap") == "mini"
        assert app_config.llm_model_tier("smart") == "big"
        assert app_config.llm_model_tier("medium") is None

    def test_openai_model(self, clean_env):
        assert app_config.openai_model() is None
        clean_env.setenv("OPENAI_MODEL", "gpt-x")
        assert app_config.openai_model() == "gpt-x"
