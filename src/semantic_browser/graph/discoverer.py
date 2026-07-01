"""
T30: Live site map discovery — 现场爬站点生成导航图, 给 GoalAgent 当参考.

跟 GraphBuilder.build() 不同: 那个从 MemoryStore (历史) 读,
discover() 是 live — 现场爬一定深度, 把发现的页面 + 链接 + 控件 记下来.

典型用法:
    agent 在复杂 goal (e.g. "找出所有产品页") 之前先调一次:
        sitemap = await discover(controller, start_url, max_pages=20, max_depth=2)
    拿到 SiteGraph → 喂给 LLM, 让它知道有哪些页面可去.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from semantic_browser.browser.controller import BrowserController
from semantic_browser.graph.builder import GraphNode, SiteGraph
from semantic_browser.snapshot.engine import SnapshotEngine

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    """discover() 返回的总结果."""
    root_url: str
    pages_visited: list[str] = field(default_factory=list)
    pages_failed: list[tuple[str, str]] = field(default_factory=list)  # (url, error)
    graph: SiteGraph = field(default_factory=lambda: SiteGraph(root_url="", domain=""))
    tree_text: str = ""  # 给人/LLM 看的 ASCII 树
    flat_list: list[dict[str, Any]] = field(default_factory=list)  # 扁平: [{url, title, depth}]


async def discover(
    controller: BrowserController,
    start_url: str,
    *,
    max_pages: int = 15,
    max_depth: int = 2,
    same_domain_only: bool = True,
    delay_ms: int = 100,
) -> DiscoveryResult:
    """T30: BFS 现场爬站点, 收集 page graph.

    Args:
        controller: BrowserController
        start_url: 起点 URL
        max_pages: 最多访问多少页 (防止失控)
        max_depth: BFS 深度上限 (从 start_url = depth 0)
        same_domain_only: True → 只跟同域链接, 不跳外站
        delay_ms: 每页之间延迟 (ms), 礼貌爬取

    Returns:
        DiscoveryResult 含 SiteGraph + tree_text + flat_list
    """
    result = DiscoveryResult(root_url=start_url)
    parsed_start = urlparse(start_url)
    start_domain = parsed_start.netloc
    result.graph = SiteGraph(root_url=start_url, domain=start_domain)

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(start_url, 0)]  # (url, depth)

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        try:
            await controller.open(url)
            await asyncio.sleep(delay_ms / 1000)
            page = controller.current_page
            if page is None:
                result.pages_failed.append((url, "no page after open"))
                continue
            title = await page.title()
            engine = SnapshotEngine(page)
            snap = await engine.capture(base_url=url)
        except Exception as e:
            result.pages_failed.append((url, f"{type(e).__name__}: {e}"[:100]))
            logger.warning("discover failed at %s: %s", url, e)
            continue

        visited.add(url)
        result.pages_visited.append(url)
        result.graph.add_node(
            url, title=title, page_type="unknown",  # 不分类 — discover 只看结构
            visited=True, depth=depth,
        )
        result.flat_list.append({"url": url, "title": title, "depth": depth})

        if depth >= max_depth:
            continue

        # BFS: 把同域链接加入队列
        for link in snap.links:
            absolute = urljoin(url, link.href)
            if same_domain_only and urlparse(absolute).netloc != start_domain:
                continue
            if absolute in visited:
                # 仍记 edge, 即便已 visited
                result.graph.add_edge(url, absolute)
                continue
            result.graph.add_edge(url, absolute)
            queue.append((absolute, depth + 1))

    result.tree_text = result.graph.to_tree_text(max_depth=max_depth)
    logger.info(
        "discover() done: visited=%d failed=%d max_pages=%d",
        len(result.pages_visited), len(result.pages_failed), max_pages,
    )
    return result


def format_for_llm(result: DiscoveryResult, max_chars: int = 2000) -> str:
    """把 DiscoveryResult 序列化成 LLM-friendly 文本.

    包含:
    - Root URL + 总页面数
    - 树形结构 (按深度)
    - 失败列表 (URL + 错误)
    """
    lines = [
        f"Site map for {result.root_url}",
        f"Pages discovered: {len(result.pages_visited)} (failed: {len(result.pages_failed)})",
        "",
        "Tree:",
        result.tree_text or "(empty)",
    ]
    if result.pages_failed:
        lines.append("\nFailures:")
        for url, err in result.pages_failed[:10]:
            lines.append(f"  ✗ {url}: {err}")
    out = "\n".join(lines)
    return out[:max_chars]