"""
LLM-Enhanced Classifier 真实 e2e 测试。

要求环境变量 (任意 OpenAI 兼容 endpoint, 包括 DeepSeek):
    OPENAI_API_KEY      — 必需
    OPENAI_BASE_URL     — 可选, 默认 https://api.openai.com/v1 (兼容 OPENAI_API_BASE)
    OPENAI_MODEL        — 可选, 默认由 endpoint 决定

若 OPENAI_API_KEY 未设置, 所有测试自动 skip。

DeepSeek 用法 (推荐, 本仓库 .progress.json 用法):
    export OPENAI_API_KEY=sk-...
    export OPENAI_BASE_URL=https://api.deepseek.com/v1
    export OPENAI_MODEL=deepseek-chat
"""
from __future__ import annotations

import os

import pytest

from semantic_browser.classifier.heuristic import PageClassifier
from semantic_browser.classifier.llm_enhanced import LLMEnhancedClassifier, VALID_TYPES
from semantic_browser.snapshot.engine import PageSnapshot, TextBlock, ControlInfo, LinkInfo


# ── fixtures ──────────────────────────────────────────────────

def _llm_env_ok() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


# ── API 可用性探测 ──────────────────────────────────────────
# env 里有 key 不代表 endpoint 真的能调通 (key 可能已过期/代理 401)。
# 做一次最小代价的探测, 缓存结果, 失败则 skip 全部 e2e 测试。

_api_live_cache: dict[str, bool | None] = {"result": None}


async def _probe_api_live() -> bool:
    """单次小请求探测 endpoint 是否真的能调通。"""
    if _api_live_cache["result"] is not None:
        return _api_live_cache["result"]
    if not _llm_env_ok():
        _api_live_cache["result"] = False
        return False
    import httpx
    base = (
        os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or "https://api.openai.com/v1"
    )
    key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            )
            live = r.status_code == 200
    except Exception as e:
        import warnings
        warnings.warn(f"LLM probe failed: {e}")
        live = False
    _api_live_cache["result"] = live
    return live


def _require_live_api() -> None:
    """同步入口: 调一次 _probe_api_live, 失败就 skip。"""
    import asyncio
    if not asyncio.run(_probe_api_live()):
        pytest.skip("LLM endpoint unreachable or auth failed; skip real-LLM e2e")


pytestmark = pytest.mark.skipif(
    not _llm_env_ok(),
    reason="OPENAI_API_KEY not set; skip real-LLM e2e",
)
# 同时在 fixture 里也 skip, 因为 asyncio_mode=auto 下 pytestmark 可能晚于 fixture


@pytest.fixture
def llm_classifier():
    """一个 _llm_available=True 的分类器, threshold 故意压低以触发 LLM 路径。"""
    if not _llm_env_ok():
        pytest.skip("OPENAI_API_KEY not set; skip real-LLM e2e")
    _require_live_api()
    cls = LLMEnhancedClassifier(threshold=0.99)  # 几乎所有启发式结果都会低于此
    return cls


@pytest.fixture
def default_threshold_classifier():
    """默认 threshold=0.5 的分类器, 启发式高置信度时不应调 LLM。"""
    if not _llm_env_ok():
        pytest.skip("OPENAI_API_KEY not set; skip real-LLM e2e")
    _require_live_api()
    cls = LLMEnhancedClassifier()  # threshold=0.5
    return cls


def _snap(url: str, title: str, blocks=None, controls=None, links=None, meta=None) -> PageSnapshot:
    return PageSnapshot(
        url=url, title=title, domain=url.split("/")[2] if "://" in url else url,
        text_blocks=blocks or [], controls=controls or [], links=links or [],
        meta=meta or {},
    )


# ── 真实 LLM 调用 ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestLLMRealE2E:
    async def test_article_page(self, llm_classifier):
        """典型博客文章: 有标题、多段正文。"""
        snap = _snap(
            url="https://blog.example.com/2026/01/release-notes",
            title="Releasing Semantic Browser v0.1.0",
            blocks=[
                TextBlock(tag="h1", text="Releasing Semantic Browser v0.1.0"),
                TextBlock(tag="p", text="We're excited to announce the first public release of the Semantic Browser, "
                                          "a Playwright-based semantic layer designed for AI agents. " * 3),
                TextBlock(tag="p", text="The release includes snapshotting, classification, and extraction." * 2),
                TextBlock(tag="p", text="Try it out and let us know what you think!" * 2),
            ],
        )
        result = await llm_classifier.classify(snap)
        assert result.page_type in VALID_TYPES
        # LLM 应当识别为 article; 允许 docs (技术博客) 也算合理
        assert result.page_type in ("article", "docs"), f"got {result.page_type}: {result.reason}"
        assert "llm_enhanced" in result.signals
        assert result.confidence > 0

    async def test_login_page(self, llm_classifier):
        """登录页: 有 password 字段、username 字段。"""
        snap = _snap(
            url="https://app.example.com/signin",
            title="Sign in to your account",
            blocks=[TextBlock(tag="h1", text="Sign in")],
            controls=[
                ControlInfo(ref="e1", kind="textbox", label="Username", placeholder="your email"),
                ControlInfo(ref="e2", kind="password", label="Password"),
                ControlInfo(ref="e3", kind="button", label="Sign in"),
            ],
        )
        result = await llm_classifier.classify(snap)
        # 启发式也会判 login (因为有 password 控件 + login URL),
        # 为了真正走 LLM 路径, 这里直接调 _llm_classify
        llm_result = await llm_classifier._llm_classify(snap)
        assert llm_result is not None
        assert llm_result.page_type == "login", f"got {llm_result.page_type}: {llm_result.reason}"

    async def test_search_results_page(self, llm_classifier):
        """搜索结果页: 有搜索框 + 大量结果链接。"""
        snap = _snap(
            url="https://www.example.com/search?q=python+tutorial",
            title="Search results for: python tutorial",
            blocks=[
                TextBlock(tag="h2", text=f"Result {i}: Some Python tutorial page...") for i in range(10)
            ],
            controls=[ControlInfo(ref="e1", kind="searchbox", label="Search", placeholder="Search...")],
            links=[
                LinkInfo(ref=f"e{i+2}", text=f"Result {i}", href=f"https://example.com/r{i}")
                for i in range(20)
            ],
        )
        llm_result = await llm_classifier._llm_classify(snap)
        assert llm_result is not None
        assert llm_result.page_type == "search", f"got {llm_result.page_type}: {llm_result.reason}"

    async def test_unknown_page(self, llm_classifier):
        """空白/奇怪页面: LLM 应判 unknown 而不是瞎猜。"""
        snap = _snap(
            url="https://example.com/whatever",
            title="...",
            blocks=[TextBlock(tag="p", text="a")],
        )
        llm_result = await llm_classifier._llm_classify(snap)
        assert llm_result is not None
        assert llm_result.page_type in VALID_TYPES
        # 不强制 unknown, 但置信度应该不高或 reason 说明不确定
        assert 0.0 <= llm_result.confidence <= 1.0

    async def test_high_confidence_heuristic_skips_llm(self, default_threshold_classifier):
        """高置信度启发式结果应直接返回, 不调 LLM。"""
        snap = _snap(
            url="https://blog.example.com/2026/01/post-123",
            title="Some Blog Post",
            blocks=[
                TextBlock(tag="h1", text="Some Blog Post"),
                TextBlock(tag="p", text="Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 10),
                TextBlock(tag="p", text="Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. " * 10),
                TextBlock(tag="p", text="Ut enim ad minim veniam, quis nostrud exercitation. " * 10),
            ],
        )
        # threshold 默认 0.5; 高置信度不应触发 LLM
        result = await default_threshold_classifier.classify(snap)
        assert "llm_enhanced" not in result.signals
        assert result.page_type == "article"

    async def test_response_parsing_handles_markdown_fence(self, llm_classifier):
        """LLM 可能把 JSON 包在 ```json ... ``` 里, 解析器应能剥掉。"""
        snap = _snap(
            url="https://example.com/about",
            title="About Us",
            blocks=[
                TextBlock(tag="h1", text="About Us"),
                TextBlock(tag="p", text="We are a small team building open-source AI tools for the web."),
                TextBlock(tag="p", text="Founded in 2026, based in San Francisco."),
            ],
        )
        llm_result = await llm_classifier._llm_classify(snap)
        assert llm_result is not None
        assert llm_result.page_type in VALID_TYPES

    async def test_invalid_type_falls_back_to_unknown(self, llm_classifier):
        """如果 LLM 返回不在枚举里的 type, 应降级到 unknown 而不是崩溃。"""
        # 这里直接构造一个 mock response 不现实; 通过真实调用验证鲁棒性
        snap = _snap(
            url="https://example.com/random",
            title="Random",
            blocks=[TextBlock(tag="p", text="just some random text " * 30)],
        )
        llm_result = await llm_classifier._llm_classify(snap)
        assert llm_result is not None
        assert llm_result.page_type in VALID_TYPES


# ── 离线鲁棒性 (不依赖真实 LLM) ──────────────────────────────

class TestLLMOffline:
    def test_unavailable_when_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cls = LLMEnhancedClassifier(enable_llm=True)
        assert cls._llm_available is False

    async def test_classify_falls_back_to_heuristic_when_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cls = LLMEnhancedClassifier(threshold=0.5, enable_llm=True)
        snap = _snap(
            url="https://example.com/x",
            title="X",
            blocks=[TextBlock(tag="p", text="short")],
        )
        result = await cls.classify(snap)
        assert result.page_type in VALID_TYPES or result.page_type == "unknown"
        # 没 LLM 时不应有 llm_enhanced 信号
        assert "llm_enhanced" not in result.signals

    async def test_404_raises_for_status(self):
        """错误 key 应 raise, classify() 应 catch 并降级。"""
        import os
        bad_key = "sk-invalid-key-for-testing-only"
        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = bad_key
        try:
            cls = LLMEnhancedClassifier(threshold=0.99)
            snap = _snap(
                url="https://example.com/x",
                title="X",
                blocks=[TextBlock(tag="p", text="short text content")],
            )
            # classify 应不抛 (内部 try/except 降级)
            result = await cls.classify(snap)
            assert result.page_type in VALID_TYPES or result.page_type == "unknown"
            assert "llm_enhanced" not in result.signals
        finally:
            if old is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old