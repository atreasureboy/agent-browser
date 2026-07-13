"""
query/ — model-driven browser semantic layer (T-Queries)

顶层 API: SemanticQuery — 顶级 agent 发高层 query, M3 驱动浏览 + 抽取 + 精炼, 返回
紧凑 markdown 答案. 详见 PLAN.md.

公开:
    SemanticQuery, SemanticAnswer, run_query
    TokenBudget, BudgetExceeded, safe_add
    QueryPlanner, QueryPlan
    RelevanceFilter, RelevanceResult, SectionInput
    LinkSelector, CandidateLink, candidates_from_snapshot
    Synthesizer
"""
from semantic_browser.query.token_budget import (
    TokenBudget,
    TokenUsage,
    BudgetExceeded,
    safe_add,
)
from semantic_browser.query.planner import QueryPlanner, QueryPlan
from semantic_browser.query.relevance import (
    RelevanceFilter, RelevanceResult, SectionInput,
)
from semantic_browser.query.synthesizer import Synthesizer
from semantic_browser.query.link_selector import (
    LinkSelector, CandidateLink, candidates_from_snapshot,
)
from semantic_browser.query.semantic_query import SemanticQuery, SemanticAnswer, run_query

__all__ = [
    "SemanticQuery",
    "SemanticAnswer",
    "run_query",
    "TokenBudget",
    "TokenUsage",
    "BudgetExceeded",
    "safe_add",
    "QueryPlanner",
    "QueryPlan",
    "RelevanceFilter",
    "RelevanceResult",
    "SectionInput",
    "LinkSelector",
    "CandidateLink",
    "candidates_from_snapshot",
    "Synthesizer",
]
