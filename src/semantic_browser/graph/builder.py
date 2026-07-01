"""
Website Graph — 站点拓扑图引擎。

自动构建网站结构：页面之间怎么跳转、哪些是文章、哪些是列表页。
输出树形结构和网络关系。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, urljoin

from semantic_browser.memory.store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class GraphNode:
    """图节点 — 一个页面。"""
    url: str
    title: str = ""
    page_type: str = "unknown"
    depth: int = 0  # 距入口的深度
    visited: bool = False
    children: list[str] = field(default_factory=list)  # 子页面 URL


@dataclass
class SiteGraph:
    """站点图。"""
    root_url: str
    domain: str
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from, to)

    def add_node(self, url: str, **kwargs) -> GraphNode:
        if url not in self.nodes:
            self.nodes[url] = GraphNode(url=url, **kwargs)
        else:
            for k, v in kwargs.items():
                setattr(self.nodes[url], k, v)
        return self.nodes[url]

    def add_edge(self, from_url: str, to_url: str) -> None:
        edge = (from_url, to_url)
        if edge not in self.edges:
            self.edges.append(edge)
        if to_url not in self.nodes:
            self.add_node(to_url)
        if to_url not in self.nodes[from_url].children:
            self.nodes[from_url].children.append(to_url)

    def to_tree_text(self, max_depth: int = 3) -> str:
        """渲染为树形文本。

        节点状态用三态区分:
          📄 ✓ — 访问过, 已知类型 (article/docs/...)
          ❓ ✓ — 访问过, 类型未知 (启发式 + LLM 都判不出)
          🔗 ○ — 仅作为链接发现, 从未访问
        """
        lines = [f"🌐 {self.domain}"]

        def render_node(url: str, depth: int, visited_set: set):
            if depth > max_depth or url in visited_set:
                return
            visited_set.add(url)
            node = self.nodes.get(url)
            if not node:
                return
            prefix = "  " * depth
            if not node.visited:
                icon, status = "🔗", "○"
            elif node.page_type == "unknown":
                icon, status = "❓", "✓"
            else:
                icon = self._type_icon(node.page_type)
                status = "✓"
            short_path = self._short_path(url)
            lines.append(f"{prefix}{icon} {status} {short_path}")
            for child_url in node.children[:20]:  # 限制宽度
                render_node(child_url, depth + 1, visited_set)

        visited = set()
        render_node(self.root_url, 1, visited)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_url": self.root_url,
            "domain": self.domain,
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "nodes": {
                url: {
                    "title": n.title,
                    "page_type": n.page_type,
                    "depth": n.depth,
                    "visited": n.visited,
                    "children_count": len(n.children),
                }
                for url, n in self.nodes.items()
            },
        }

    @staticmethod
    def _type_icon(page_type: str) -> str:
        icons = {
            "article": "📄",
            "list": "📋",
            "search": "🔍",
            "login": "🔐",
            "docs": "📚",
            "dashboard": "📊",
            "error": "❌",
            "video": "🎬",
            "unknown": "❓",
        }
        return icons.get(page_type, "❓")

    @staticmethod
    def _short_path(url: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if len(path) > 50:
            path = path[:47] + "..."
        return path


class GraphBuilder:
    """
    从 MemoryStore 构建站点图。
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def build(self, root_url: str, max_depth: int = 3) -> SiteGraph:
        """从入口 URL 构建站点图。"""
        domain = urlparse(root_url).netloc
        graph = SiteGraph(root_url=root_url, domain=domain)

        # 添加根节点
        root_page = self.store.get_page(root_url)
        if root_page:
            graph.add_node(
                root_url,
                title=root_page.get("title", ""),
                page_type=root_page.get("page_type", "unknown"),
                visited=True,
                depth=0,
            )
        else:
            graph.add_node(root_url, visited=True, depth=0)

        # 从数据库加载该域名的所有页面和链接
        self._populate_from_store(graph, domain, max_depth)
        logger.info(
            "Graph built: %s (%d nodes, %d edges)",
            domain, len(graph.nodes), len(graph.edges),
        )
        return graph

    def _populate_from_store(
        self, graph: SiteGraph, domain: str, max_depth: int
    ) -> None:
        """从 store 填充图数据。"""
        import sqlite3
        db_path = self.store.db_path
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        try:
            # 加载该域名所有页面
            rows = conn.execute(
                "SELECT * FROM pages WHERE domain = ?", (domain,)
            ).fetchall()
            for row in rows:
                url = row["url"]
                if url == graph.root_url:
                    continue
                graph.add_node(
                    url,
                    title=row["title"] or "",
                    page_type=row["page_type"] or "unknown",
                    visited=True,
                    depth=self._guess_depth(graph.root_url, url),
                )

            # 加载该域名的链接关系
            link_rows = conn.execute(
                """SELECT * FROM links
                   WHERE from_url LIKE ? OR to_url LIKE ?""",
                (f"%{domain}%", f"%{domain}%"),
            ).fetchall()
            for row in link_rows:
                from_url = row["from_url"]
                to_url = row["to_url"]
                if from_url not in graph.nodes:
                    graph.add_node(from_url, visited=False)
                if to_url not in graph.nodes:
                    graph.add_node(
                        to_url,
                        visited=bool(row["visited"]),
                        depth=self._guess_depth(graph.root_url, to_url),
                    )
                graph.add_edge(from_url, to_url)
        finally:
            conn.close()

    @staticmethod
    def _guess_depth(root_url: str, url: str) -> int:
        """估算页面深度（基于 URL 路径段数）。"""
        root_path = urlparse(root_url).path.rstrip("/")
        url_path = urlparse(url).path.rstrip("/")
        root_segments = len([s for s in root_path.split("/") if s])
        url_segments = len([s for s in url_path.split("/") if s])
        return max(0, url_segments - root_segments)
