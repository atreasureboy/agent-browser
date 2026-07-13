"""
SemanticQuery — 顶层 API, query → structured answer.

这是"模型驱动的浏览器语义层"的核心对外接口. 顶级 agent 用一次调用拿回:
  - markdown 精炼答案 (≤ max_chars)
  - sources URL 列表
  - confidence (M3 自评)
  - tokens_used 字段 (透明披露)
  - steps 详情 (调试用, 可关闭)

内部流程:
  1. plan:       M3 cheap 拆高层 query
  2. browse:     本地 Playwright open page + extract (无 token)
  3. relevance:  M3 cheap 给 sections 打分 (省 token)
  4. synthesize: M3 cheap 把相关 sections 合成 markdown (省 token)
  5. confidence: M3 报告 overall → 够用就停

token 经济:
  - browser side: 无 (本地 Chromium)
  - LLM 调用全部走 cheap (M3): plan ~50t, relevance ~100t, synth ~200t
  - 总计 ≤ 500t 输出给顶层 agent, 节省 99% 对比直接传 DOM

cache:
  - 内存 LRU 64, TTL DEFAULT_CACHE_TTL_S (默认 600s)
  - 可选持久化到 disk (cache_persist_path)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from semantic_browser.llm.service import LLMService
from semantic_browser.query.token_budget import TokenBudget, BudgetExceeded
from semantic_browser.query.planner import QueryPlanner, QueryPlan
from semantic_browser.query.relevance import (
    RelevanceFilter, RelevanceResult, SectionInput,
)
from semantic_browser.query.synthesizer import Synthesizer
from semantic_browser.query.link_selector import (
    LinkSelector, candidates_from_snapshot,
)

logger = logging.getLogger(__name__)


_DEFAULT_PERSIST_PATH = Path.home() / ".semantic-browser" / "query_cache.json"


@dataclass
class SemanticAnswer:
    """顶层 agent 消费的结构化答案."""
    query: str
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    tokens_used: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "sources": list(self.sources),
            "confidence": self.confidence,
            "tokens_used": self.tokens_used,
            "steps": list(self.steps),
            "plan": self.plan,
            "success": self.success,
            "error": self.error,
            "elapsed_s": self.elapsed_s(),
        }

    def elapsed_s(self) -> Optional[float]:
        """T70.5: 计算本次 run 总耗时 (秒). 从 steps 首/末 ts 计算.

        Returns None 如果 steps 为空.
        """
        if not self.steps:
            return None
        ts_values = [s.get("ts") for s in self.steps if isinstance(s.get("ts"), (int, float))]
        if len(ts_values) < 2:
            return None
        return round(max(ts_values) - min(ts_values), 2)

    def to_markdown(self) -> str:
        """给顶层 agent 看的紧凑 markdown (含元数据)."""
        lines = []
        if self.answer:
            lines.append(self.answer)
        else:
            lines.append(f"_(no answer: {self.error or 'unknown'})_")
        if self.sources:
            lines.append("\n## Sources")
            for i, s in enumerate(self.sources, 1):
                lines.append(f"[{i}] {s}")
        meta = []
        meta.append(f"confidence: {self.confidence:.2f}")
        # tokens_used 结构 = {"max_total":N, "used":{prompt,completion,total}, "remaining":M, "exhausted":bool}
        used_block = self.tokens_used.get("used") if isinstance(self.tokens_used, dict) else None
        if isinstance(used_block, dict) and used_block.get("total"):
            meta.append(f"tokens: {used_block['total']}/{self.tokens_used.get('max_total', '?')}")
        if meta:
            lines.append("\n_" + " · ".join(meta) + "_")
        return "\n".join(lines)


class SemanticQuery:
    """顶层 query 入口.

    用法:
        # 默认模式: 自动创建 SemanticBrowser (每次启动 Chromium, ~2s 启动费)
        sq = SemanticQuery()
        result = await sq.run("find GitHub PEP 703 discussions", start_url="https://github.com/python/peps")

        # 推荐模式: 注入已有 SemanticBrowser (避免重复启动)
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser()
        await sb.start()
        sq = SemanticQuery(browser=sb)
        result = await sq.run(...)
        await sb.close()

        # 最推荐模式: 通过 daemon 调用 (多 agent 共享 chromium)
        # → 待 daemon 暴露 /v1/query 端点
    """

    DEFAULT_BUDGET = 2000
    DEFAULT_MAX_PAGES = 1
    DEFAULT_SUFFICIENCY = 0.7
    DEFAULT_RELEVANCE_THRESHOLD = 0.3
    DEFAULT_ANSWER_MAX_CHARS = 2000
    DEFAULT_CACHE_TTL_S = 600  # 10 分钟同 query 复用 cache (节省 token)
    DEFAULT_CACHE_MAX_SIZE = 64  # 内存 cache LRU 上限

    def __init__(
        self,
        *,
        llm: Optional[LLMService] = None,
        browser=None,  # SemanticBrowser instance (推荐)
        budget: int = DEFAULT_BUDGET,
        max_pages: int = DEFAULT_MAX_PAGES,
        sufficiency_threshold: float = DEFAULT_SUFFICIENCY,
        relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
        answer_max_chars: int = DEFAULT_ANSWER_MAX_CHARS,
        include_steps: bool = True,
        cache_enabled: bool = True,
        cache_ttl_s: float = DEFAULT_CACHE_TTL_S,
        cache_persist_path: Optional[str] = None,  # T68: 持久化到磁盘
        cache_max_size: int = DEFAULT_CACHE_MAX_SIZE,  # T69: 可配置 LRU 上限
        on_phase: "Optional[callable]" = None,  # T67+: async/sync callable(phase_dict)
    ):
        self.llm = llm or LLMService()
        self.browser = browser
        # T70.15: 边界检查 (避免 run() 时才崩)
        if budget < 1:
            raise ValueError(f"budget must be >= 1 (got {budget}); use cache + plan-only for zero-cost path")
        if max_pages < 0:
            raise ValueError(f"max_pages must be >= 0 (got {max_pages})")
        self.default_budget = budget
        self.max_pages = max_pages
        self.sufficiency_threshold = sufficiency_threshold
        self.relevance_threshold = relevance_threshold
        self.answer_max_chars = answer_max_chars
        self.include_steps = include_steps
        self.cache_enabled = cache_enabled
        self.cache_ttl_s = cache_ttl_s
        self.cache_persist_path = cache_persist_path  # T68
        self.cache_max_size = cache_max_size  # T69
        self.on_phase = on_phase  # SSE 流式回调: 每步 (plan / browse / relevance / synth) 触发

        # 子模块: 都复用 self.llm
        self.planner = QueryPlanner(self.llm)
        self.relevance = RelevanceFilter(self.llm, threshold=relevance_threshold)
        self.synthesizer = Synthesizer(self.llm)
        self.link_selector = LinkSelector(self.llm)

        # query cache: {(query, start_url) → (ts, SemanticAnswer)}
        self._cache: dict[tuple, tuple[float, SemanticAnswer]] = {}
        # T68+ metrics: cache 命中/未命中 + 总调用
        self._cache_hits = 0
        self._cache_misses = 0
        self._call_count = 0
        # 从磁盘加载 (如果指定了 path)
        if cache_persist_path:
            self._load_cache(cache_persist_path)

        self._owns_browser = browser is None  # 默认创建模式负责 close

    def cache_stats(self) -> dict[str, Any]:
        """T68+: cache 统计 — 给监控 / 运维用."""
        return {
            "enabled": self.cache_enabled,
            "ttl_s": self.cache_ttl_s,
            "size": len(self._cache),
            "max_size": self.cache_max_size,
            "persist_path": self.cache_persist_path,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "calls": self._call_count,
            "hit_rate": (
                round(self._cache_hits / (self._cache_hits + self._cache_misses), 3)
                if (self._cache_hits + self._cache_misses) > 0
                else None
            ),
        }

    def clear_cache(self) -> dict[str, Any]:
        """T69+: 清空缓存 (运维 / 测试用).

        Returns {"cleared": N} where N is number of entries removed.
        也清 hits/misses/calls 计数器 (供运维重置 SLA 窗口).
        """
        cleared = len(self._cache)
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        # 不重置 calls — 它是 instance lifetime counter, 给用户长期观察
        # 如果想完全重置也可以, 但 clear_cache 通常是 cache-only 操作
        return {"cleared": cleared, "remaining": len(self._cache)}

    async def _ensure_browser(self):
        if self.browser is None:
            from semantic_browser.engine import SemanticBrowser
            self.browser = SemanticBrowser()
            self._owns_browser = True
            await self.browser.start()
        return self.browser

    async def close(self) -> None:
        if self._owns_browser and self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass

    async def run(
        self,
        query: str,
        *,
        start_url: Optional[str] = None,
        budget: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> SemanticAnswer:
        """主入口 — 顶层 agent 调用这一个方法就拿回所有需要的.

        Args:
            query: 自然语言问题 ("find GitHub PEP 703 discussions and give 3 perspectives").
                  不能为空 — 抛 ValueError.
            start_url: 可选入口 URL.
                     - 不传 (None): 不浏览网页, 只让 M3 给出研究 plan (用于顶层 agent 选 URL)
                     - 传 URL: 自动 Chromium + browse + M3 relevance + synthesize
            budget: 覆盖默认 token 预算 (LLM 调用硬上限, 超了 fall back 不崩溃).
            max_pages: 覆盖默认 max_pages.
                     - 1: 单页模式
                     - >1: M3 选 next URL 多页 follow-link (T68)

        Returns:
            SemanticAnswer: 结构化答案.
                .answer:    markdown 答案 (~max_chars)
                .sources:   去重的 URL 列表
                .confidence: M3 自评 0-1
                .tokens_used: {used: {prompt, completion, total}, max_total, cache_hit, cache_age_s}
                .steps:     phase 详情 (调试用)
                .success:   True / False (graceful fallback 后 False 也返部分 answer)

        Raises:
            ValueError: query 为空.
            LLMUnavailableError: LLM 完全不可用且 query 没 fallback 路径.

        Examples:
            >>> sq = SemanticQuery()
            >>> r = await sq.run("Python 3.13 free-threading executable")
            >>> r = await sq.run("Same query", start_url="https://docs.python.org/3/whatsnew/3.13.html")
            >>> r = await sq.run("Need URL first")  # plan-only
        """
        # Cache 命中检查: 相同 (query, start_url) 在 TTL 内直接复用
        if self.cache_enabled and start_url is not None:
            cache_key = (query.strip().lower(), start_url)
            cached = self._cache.get(cache_key)
            if cached:
                ts, ans = cached
                if (time.time() - ts) < self.cache_ttl_s:
                    logger.info("query cache HIT")
                    import copy
                    hit = copy.deepcopy(ans)
                    hit.tokens_used = {
                        **hit.tokens_used,
                        "cache_hit": True,
                        "cache_age_s": round(time.time() - ts, 1),
                    }
                    hit.steps = [{"phase": "cache_hit", "ts": time.time(),
                                 "original_ts": ts}] if self.include_steps else []
                    self._cache_hits += 1
                    return hit
            self._cache_misses += 1

        result = SemanticAnswer(query=query)
        budget_obj = TokenBudget(budget or self.default_budget)
        max_p = max_pages if max_pages is not None else self.max_pages
        self._call_count += 1
        steps: list[dict[str, Any]] = []

        def _record_step(phase: str, **kw):
            if not self.include_steps:
                return
            steps.append({"phase": phase, "ts": time.time(), **kw})
            # T67+ T69: SSE callback — 真同步支持 (cb 同步返回立即;
            #           async 返回用 asyncio 调度成 task)
            if self.on_phase:
                try:
                    ret = self.on_phase({"phase": phase, "ts": time.time(), **kw})
                    if hasattr(ret, "__await__"):
                        # async callback — schedule as task
                        try:
                            loop = asyncio.get_event_loop()
                            loop.create_task(ret)
                        except RuntimeError:
                            # no event loop in current thread — run inline
                            pass
                except Exception as e:
                    logger.warning("on_phase callback raised: %s", e)

        try:
            # ── Step 1: plan ─────────────────────────────
            _record_step("plan_start", query=query)
            plan: QueryPlan = await self.planner.plan(query, budget=budget_obj)
            result.plan = plan.to_dict()
            _record_step("plan_done", plan=plan.to_dict(), tokens=budget_obj.usage.to_dict())

            if not start_url:
                # 没 start_url 时本次 query 没法浏览, 仅返回 plan (高层 agent 自己定位 URL)
                result.success = True
                result.confidence = 0.0
                result.answer = (
                    f"_(no start_url provided; plan returned for top-tier agent to choose URL)_\n\n"
                    f"**Primary target**: {plan.primary_target}\n\n"
                    f"**Sub-questions**:\n"
                    + "\n".join(f"- {q}" for q in plan.sub_questions)
                    + f"\n\n**Keywords**: {', '.join(plan.keywords)}"
                )
                result.tokens_used = budget_obj.to_dict()
                result.steps = steps if self.include_steps else []
                return result

            # ── Step 2: browse ───────────────────────────
            browser = await self._ensure_browser()
            sources_visited: list[str] = []
            all_excerpts: list[dict[str, Any]] = []
            overall_confidence = 0.0
            current_url: Optional[str] = start_url

            for page_i in range(max_p):
                if current_url is None:
                    break  # 已无 URL 可看

                page_url = current_url
                _record_step("browse_start", page=page_i + 1, url=page_url)

                try:
                    browse_result = await browser.browse(page_url)
                except Exception as e:
                    logger.warning("browse(%s) failed: %s", page_url, e)
                    _record_step("browse_failed", url=page_url, error=str(e)[:200])
                    current_url = None
                    continue

                sources_visited.append(browse_result.snapshot.url)
                final_url = browse_result.snapshot.url
                _record_step("browse_done", url=final_url,
                             page_type=browse_result.classification.page_type,
                             sections=len(browse_result.article.sections) if browse_result.article else 0)

                # ── Step 3: extract sections for relevance filter ──
                sections = self._extract_sections(browse_result)
                # 构建 relevance score hints (for LinkSelector fallback) — 用本次 relevance 结果
                score_hints: dict[str, float] = {}

                if not sections:
                    _record_step("no_sections", url=final_url)
                else:
                    _record_step("relevance_start", n_sections=len(sections))

                    # ── Step 4: relevance filter ──
                    rel: RelevanceResult = await self.relevance.score(
                        query, sections, budget=budget_obj,
                    )
                    _record_step("relevance_done",
                                 n_kept=len(rel.kept(self.relevance_threshold)),
                                 overall=rel.overall,
                                 useful=rel.useful,
                                 tokens=budget_obj.usage.to_dict())

                    # 累计 kept sections 到 all_excerpts
                    for kept_idx in rel.kept(self.relevance_threshold):
                        sec = sections[kept_idx]
                        all_excerpts.append({
                            "heading": sec.heading,
                            "text": sec.excerpt,
                            "source_idx": len(sources_visited),
                            "url": final_url,
                        })

                    # 收集 score hints 给 LinkSelector (按 link_href)
                    # SectionInput.link_href 有值时, 它的 score 影响该 href 的 score_hint
                    for kept_idx, sc in rel.scored:
                        if 0 <= kept_idx < len(sections):
                            href = sections[kept_idx].link_href
                            if href:
                                score_hints[href] = max(score_hints.get(href, 0.0), sc)

                    overall_confidence = max(overall_confidence, rel.overall)

                    # sufficiency check
                    if rel.overall >= self.sufficiency_threshold:
                        _record_step("early_break_sufficient", reason="confidence>=threshold")
                        current_url = None
                        break

                if budget_obj.exhausted():
                    _record_step("early_break_budget", reason="budget_exhausted")
                    current_url = None
                    break

                # ── Step 4.5: pick next URL (仅当 max_p > 1) ──
                if max_p <= 1:
                    break  # 单页模式

                # 多页模式: 让 M3 选下一个 URL
                # 收集 candidates (含当前页链接; 排除已访问)
                candidates = candidates_from_snapshot(
                    browse_result.snapshot, max_internal=20,
                    score_hints=score_hints,
                )
                candidates = [c for c in candidates if c.url not in sources_visited]

                if not candidates:
                    _record_step("no_more_links", url=final_url)
                    break

                next_url = await self.link_selector.pick_next(
                    query, final_url,
                    getattr(browse_result.snapshot, "title", "") or "",
                    candidates,
                    budget=budget_obj,
                )
                _record_step("link_selector_done",
                             n_candidates=len(candidates),
                             next_url=next_url,
                             tokens=budget_obj.usage.to_dict())

                if next_url is None or next_url in sources_visited:
                    break

                current_url = next_url

            # ── Step 5: synthesize final answer ──
            _record_step("synth_start", n_excerpts=len(all_excerpts),
                         n_sources=len(sources_visited))
            if not all_excerpts:
                result.answer = "_(No relevant content found in visited page(s). Try a different start_url or rephrase.)_"
                result.confidence = 0.0
            else:
                result.answer = await self.synthesizer.synthesize(
                    query, all_excerpts, sources_visited,
                    max_chars=self.answer_max_chars,
                    answer_format=plan.expected_answer_format,
                    budget=budget_obj,
                )
                result.confidence = overall_confidence

            _record_step("synth_done", answer_chars=len(result.answer),
                         tokens=budget_obj.usage.to_dict())

            result.sources = list(dict.fromkeys(sources_visited))  # 去重保序
            result.tokens_used = budget_obj.to_dict()
            result.steps = steps if self.include_steps else []
            result.success = True

            # 写入 cache (限大小, LRU 简单淘汰)
            # T69: 可配置 cache_max_size; 超上限时按 ts 升序淘汰最旧
            if self.cache_enabled and start_url is not None:
                cache_key = (query.strip().lower(), start_url)
                # 超上限 → 找最旧条目淘汰 (简单的 FIFO 淘汰)
                while len(self._cache) >= self.cache_max_size:
                    oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
                    if oldest_key == cache_key:
                        # 不会替换自己; 直接清空
                        self._cache.clear()
                        break
                    del self._cache[oldest_key]
                self._cache[cache_key] = (time.time(), result)
                # T68: 同步到磁盘 (异步 fire-and-forget)
                if self.cache_persist_path:
                    self._save_cache(self.cache_persist_path)

        except BudgetExceeded as e:
            logger.warning("token budget exhausted during query")
            result.error = f"budget_exceeded: {e}"
            result.tokens_used = budget_obj.to_dict()
            result.steps = steps if self.include_steps else []
            result.success = False
            if result.answer:
                # 已合成过, 保留; 否则给个说明
                result.answer += "\n\n_(budget exhausted before full synthesis)_"
        except Exception as e:
            logger.exception("SemanticQuery.run failed")
            result.error = f"{type(e).__name__}: {e}"[:300]
            result.tokens_used = budget_obj.to_dict()
            result.steps = steps if self.include_steps else []

        return result

    @staticmethod
    def _extract_sections(browse_result) -> list[SectionInput]:
        """从 browse_result 提 sections 给 relevance filter.

        三层 fallback:
          1. article.sections (article / docs 页)
          2. snapshot.text_blocks (list / search / forum / dashboard 等没有 article 的页)
          3. snapshot.links (纯链接列表页 — HN 这种)
        """
        out: list[SectionInput] = []
        idx = 0

        # 来源 1: article 段落 (article / docs 类)
        article = getattr(browse_result, "article", None)
        if article and getattr(article, "sections", None):
            for sec in article.sections:
                heading = sec.get("heading", "") or ""
                paras = sec.get("paragraphs", []) or []
                text = "\n".join(paras[:5]) if isinstance(paras, list) else str(paras)[:300]
                text = text[:300]
                out.append(SectionInput(
                    index=idx, heading=heading[:120], excerpt=text,
                ))
                idx += 1

        # 来源 2 + 3: snapshot (list / search / dashboard / forum 等没有 article 的页)
        snap = getattr(browse_result, "snapshot", None)
        if snap:
            # 来源 2: text_blocks
            if not out and getattr(snap, "text_blocks", None):
                for block in snap.text_blocks[:30]:
                    text = (block.text or "")[:300] if hasattr(block, "text") else str(block)[:300]
                    if not text.strip() or len(text.strip()) < 20:
                        continue
                    out.append(SectionInput(
                        index=idx,
                        heading=block.tag if hasattr(block, "tag") else "block",
                        excerpt=text,
                    ))
                    idx += 1

            # 来源 3: links (HN frontpage 这种纯链接列表)
            if not out and getattr(snap, "links", None):
                for ln in snap.links[:30]:
                    text = (ln.text or "") if hasattr(ln, "text") else str(ln)
                    href = (ln.href or "") if hasattr(ln, "href") else ""
                    if not text.strip() or len(text.strip()) < 10:
                        continue
                    out.append(SectionInput(
                        index=idx,
                        heading="link",
                        excerpt=text[:300],
                        link_href=href,
                    ))
                    idx += 1
        return out

    # ── 持久 cache helpers ──────────────────────────────────

    def _save_cache(self, path) -> None:
        """把内存 cache 序列化到磁盘 (JSON).

        格式: {cache_key_str: {"ts": ..., "answer": {...}}}
        失败不抛 (持久化是 best-effort).
        """
        try:
            from pathlib import Path
            data = {}
            for (q, url), (ts, ans) in self._cache.items():
                key = f"{q}|||{url}"
                data[key] = {"ts": ts, "answer": ans.to_dict()}
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(p)
        except Exception as e:
            logger.warning("failed to save query cache: %s", e)

    def _load_cache(self, path) -> None:
        """从磁盘加载 cache 到内存. 失败不抛."""
        try:
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                return
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                return
            for key_str, entry in data.items():
                if not isinstance(entry, dict) or "ts" not in entry or "answer" not in entry:
                    continue
                if "|||" not in key_str:
                    continue
                q, url = key_str.split("|||", 1)
                ans_dict = entry["answer"]
                ans = SemanticAnswer(
                    query=ans_dict.get("query", q),
                    answer=ans_dict.get("answer", ""),
                    sources=list(ans_dict.get("sources", []) or []),
                    confidence=float(ans_dict.get("confidence", 0.0)),
                    tokens_used=dict(ans_dict.get("tokens_used", {}) or {}),
                    steps=list(ans_dict.get("steps", []) or []),
                    plan=dict(ans_dict.get("plan", {}) or {}),
                    success=bool(ans_dict.get("success", False)),
                    error=ans_dict.get("error"),
                )
                ts = float(entry["ts"])
                if (time.time() - ts) > 30 * 86400:
                    continue
                self._cache[(q, url)] = (ts, ans)
            logger.info("loaded %d query cache entries from %s", len(self._cache), path)
        except Exception as e:
            logger.warning("failed to load query cache: %s", e)


async def run_query(
    query: str,
    *,
    start_url: Optional[str] = None,
    budget: int = 2000,
    max_pages: int = 1,
    cache_persist_path: Optional[str] = None,
    cache_ttl_s: float = SemanticQuery.DEFAULT_CACHE_TTL_S,
) -> SemanticAnswer:
    """便捷函数: 单次 query 后自动 close 浏览器.

    支持 query cache: 传 cache_persist_path 后, 同 query+URL 跨调用命中缓存 (0 token).
    """
    sq = SemanticQuery(
        budget=budget,
        max_pages=max_pages,
        cache_persist_path=cache_persist_path,
        cache_ttl_s=cache_ttl_s,
    )
    try:
        return await sq.run(query, start_url=start_url)
    finally:
        await sq.close()
