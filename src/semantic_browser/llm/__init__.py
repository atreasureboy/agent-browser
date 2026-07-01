"""LLM 服务抽象层 — 支持 cheap/medium/smart 三档模型路由."""

from semantic_browser.llm.service import (
    LLMService,
    LLMResponse,
    LLMUnavailableError,
    Tier,
    get_default_service,
    reset_default_service,
)
from semantic_browser.llm.helpers import (
    slice_refs_for_goal,
    summarize_text,
    extract_fields,
    find_ref_by_label,
    build_smart_snapshot_excerpt,
)
from semantic_browser.llm.diagnostics import (
    collect_diagnostics,
    format_diagnostics_for_llm,
)

__all__ = [
    "LLMService",
    "LLMResponse",
    "LLMUnavailableError",
    "Tier",
    "get_default_service",
    "reset_default_service",
    "slice_refs_for_goal",
    "summarize_text",
    "extract_fields",
    "find_ref_by_label",
    "build_smart_snapshot_excerpt",
    "collect_diagnostics",
    "format_diagnostics_for_llm",
]


# T30: 站点图自动发现
from semantic_browser.graph.discoverer import (
    discover,
    DiscoveryResult,
    format_for_llm as format_sitemap_for_llm,
)

__all__ += ["discover", "DiscoveryResult", "format_sitemap_for_llm"]