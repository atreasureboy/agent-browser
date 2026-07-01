"""
增量爬取引擎 — BFS 站内爬虫。

基于 SemanticBrowser + MemoryStore 实现：
  - BFS 遍历（按深度逐层）
  - 同域名过滤
  - 深度 / 数量限制
  - 去重（内存集合 + MemoryStore.visited）
  - 断点续跑：从 MemoryStore 读取未访问链接继续
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, urldefrag

from semantic_browser.engine import BrowseResult, SemanticBrowser

logger = logging.getLogger(__name__)


# ── 结果数据结构 ──────────────────────────────────────────────

@dataclass
class CrawlResult:
    """一次 crawl() 的汇总结果。"""

    visited_urls: list[str] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)
    skipped_urls: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "visited_urls": list(self.visited_urls),
            "failed_urls": list(self.failed_urls),
            "skipped_urls": list(self.skipped_urls),
            "stats": dict(self.stats),
        }


# ── 队列条目 ─────────────────────────────────────────────────

@dataclass
class _QueueItem:
    url: str
    depth: int


# ── 主类 ─────────────────────────────────────────────────────

class Crawler:
    """
    增量 BFS 爬虫。

    用法:
        crawler = Crawler()
        result = await crawler.crawl(
            "https://blog.example.com",
            max_pages=20,
            max_depth=3,
        )
        print(result.stats)

    也可通过注入已有的 SemanticBrowser 实例（避免重复启动浏览器）:
        sb = SemanticBrowser()
        await sb.start()
        crawler = Crawler(sb)
        result = await crawler.crawl(...)
        await sb.close()
    """

    def __init__(
        self,
        browser: Optional[SemanticBrowser] = None,
        *,
        headless: bool = True,
        db_path: str = "~/.semantic-browser/memory.db",
        request_delay: float = 0.5,
    ) -> None:
        # 如果调用方传入 browser，则我们仅“借用”，不负责其生命周期
        self._external_browser = browser is not None
        self.browser = browser or SemanticBrowser(db_path=db_path, headless=headless)
        self.request_delay = request_delay

    # ── 工具方法 ─────────────────────────────────────────────

    @staticmethod
    def _normalize(url: str) -> str:
        """规范化 URL：去 fragment、去尾部斜杠（根路径除外）。"""
        url, _frag = urldefrag(url)
        parsed = urlparse(url)
        # 去掉多余的空 query 等；保留 scheme/netloc/path/query
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        rebuilt = f"{scheme}://{netloc}{path}"
        if parsed.query:
            rebuilt += f"?{parsed.query}"
        return rebuilt

    @staticmethod
    def _domain_of(url: str) -> str:
        return urlparse(url).netloc.lower()

    @staticmethod
    def _is_http(url: str) -> bool:
        return urlparse(url).scheme in ("http", "https")

    def _is_internal(self, url: str, seed_domain: str) -> bool:
        d = self._domain_of(url)
        if d == seed_domain:
            return True
        # 允许 www 前缀差异 / 子域名
        if d.endswith("." + seed_domain) or seed_domain.endswith("." + d):
            return True
        return False

    def _is_crawlable_url(self, url: str) -> bool:
        """跳过 mailto:/javascript:/tel: 等非页面链接。"""
        if not url:
            return False
        lower = url.strip().lower()
        if lower.startswith(("mailto:", "javascript:", "tel:", "data:", "#")):
            return False
        return self._is_http(url)

    # ── 队列种子构建（断点续跑） ─────────────────────────────

    def _load_unvisited_from_store(
        self, domain: str, seen: set[str]
    ) -> list[_QueueItem]:
        """从 MemoryStore 读取未访问链接，用于断点续跑。"""
        items: list[_QueueItem] = []
        try:
            rows = self.browser.store.get_unvisited_links(domain, limit=200)
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("读取未访问链接失败: %s", e)
            return items

        for row in rows:
            raw = row.get("to_url", "")
            if not raw:
                continue
            norm = self._normalize(raw)
            if norm in seen:
                continue
            if not self._is_internal(norm, domain):
                continue
            seen.add(norm)
            items.append(_QueueItem(url=norm, depth=1))
            logger.debug("续跑载入未访问链接: %s", norm)
        if items:
            logger.info("从记忆库续跑载入 %d 个未访问链接", len(items))
        return items

    # ── 主入口 ───────────────────────────────────────────────

    async def crawl(
        self,
        start_url: str,
        max_pages: int = 20,
        max_depth: int = 3,
        same_domain_only: bool = True,
    ) -> CrawlResult:
        """
        从 start_url 开始 BFS 爬取。

        参数:
            start_url: 起点 URL
            max_pages: 本次最多访问页面数（不含此前已访问的）
            max_depth: 最大爬取深度（起点为 0）
            same_domain_only: True 则仅爬同域名页面

        返回:
            CrawlResult
        """
        if not start_url or not self._is_http(start_url):
            raise ValueError(f"非法起始 URL: {start_url!r}")

        result = CrawlResult()
        seed_domain = self._domain_of(start_url)
        start_norm = self._normalize(start_url)

        # visited（内存去重，进程内有效）
        seen: set[str] = set()
        visited: set[str] = set()
        # 已在数据库里访问过的（断点续跑时不重复访问）
        db_visited: set[str] = self._load_db_visited(seed_domain)

        # BFS 队列
        queue: deque[_QueueItem] = deque()
        queue.append(_QueueItem(url=start_norm, depth=0))
        seen.add(start_norm)

        # 断点续跑：补充未访问链接（depth=1，因为它们的“来源深度”未知，
        # 用 1 保证它们会被处理；max_depth 仍会限制整体深度推进）
        if same_domain_only:
            for item in self._load_unvisited_from_store(seed_domain, seen):
                queue.append(item)

        logger.info(
            "开始 BFS 爬取: seed=%s domain=%s max_pages=%d max_depth=%d (队列初始=%d)",
            start_norm, seed_domain, max_pages, max_depth, len(queue),
        )

        # 启动浏览器（如果由本 Crawler 拥有）
        await self.browser.start()
        t_start = time.time()

        pages_this_run = 0

        try:
            while queue and pages_this_run < max_pages:
                item = queue.popleft()

                # 深度限制
                if item.depth > max_depth:
                    logger.debug("跳过（深度超限 %d>%d）: %s", item.depth, max_depth, item.url)
                    result.skipped_urls.append(item.url)
                    continue

                url = item.url

                # 域名过滤
                if same_domain_only and not self._is_internal(url, seed_domain):
                    logger.debug("跳过（非同域名）: %s", url)
                    result.skipped_urls.append(url)
                    continue

                # 去重：内存或数据库已访问
                if url in visited or url in db_visited:
                    logger.debug("跳过（已访问）: %s", url)
                    continue

                # 实际访问
                browse_result = await self._safe_browse(url)
                if browse_result is None:
                    result.failed_urls.append(url)
                    # 失败也标记为 visited，避免无限重试
                    visited.add(url)
                    continue

                visited.add(url)
                result.visited_urls.append(url)
                pages_this_run += 1
                logger.info(
                    "[%d/%d] 已爬 (%d/%d) %s",
                    pages_this_run, max_pages, item.depth, max_depth, url,
                )

                # 从快照提取新链接入队
                new_enqueued = 0
                for link in browse_result.snapshot.links:
                    raw_href = getattr(link, "href", "") or ""
                    if not self._is_crawlable_url(raw_href):
                        continue
                    norm = self._normalize(raw_href)
                    if norm in seen or norm in visited or norm in db_visited:
                        continue
                    if same_domain_only and not self._is_internal(norm, seed_domain):
                        continue
                    seen.add(norm)
                    queue.append(_QueueItem(url=norm, depth=item.depth + 1))
                    new_enqueued += 1

                if new_enqueued:
                    logger.debug("从 %s 入队 %d 个新链接", url, new_enqueued)

                # 标记这些链接已“被发现并访问来源”
                # （MemoryStore 中的 visited 字段由 browse() 流程间接维护，
                #  这里不重复 mark，保持单一数据源）

                # 限速，避免给目标站点造成压力
                if self.request_delay > 0 and queue and pages_this_run < max_pages:
                    await asyncio.sleep(self.request_delay)
        finally:
            # 仅在我们自己启动的浏览器上关闭
            if not self._external_browser:
                await self.browser.close()

        elapsed = time.time() - t_start
        result.stats = {
            "start_url": start_norm,
            "domain": seed_domain,
            "pages_visited": len(result.visited_urls),
            "pages_failed": len(result.failed_urls),
            "pages_skipped": len(result.skipped_urls),
            "queue_remaining": len(queue),
            "max_pages": max_pages,
            "max_depth": max_depth,
            "same_domain_only": same_domain_only,
            "elapsed_sec": round(elapsed, 2),
        }
        logger.info(
            "爬取完成: 已访问=%d 失败=%d 跳过=%d 队列剩余=%d 耗时=%.2fs",
            len(result.visited_urls),
            len(result.failed_urls),
            len(result.skipped_urls),
            len(queue),
            elapsed,
        )
        return result

    # ── 内部辅助 ─────────────────────────────────────────────

    def _load_db_visited(self, domain: str) -> set[str]:
        """从 MemoryStore 读出该域名下已访问的 URL 集合（用于断点续跑去重）。"""
        visited: set[str] = set()
        try:
            for page in self.browser.store.get_pages_by_domain(domain, limit=10000):
                url = page.get("url", "")
                if url:
                    visited.add(self._normalize(url))
        except Exception as e:  # pragma: no cover
            logger.warning("读取已访问页面失败: %s", e)
        if visited:
            logger.info("记忆库中 %s 域名已访问 %d 页", domain, len(visited))
        return visited

    async def _safe_browse(self, url: str) -> Optional[BrowseResult]:
        """包装 browse()，捕获异常并记录失败 URL。"""
        try:
            return await self.browser.browse(url)
        except Exception as e:
            logger.warning("访问失败 %s: %s", url, e)
            return None


# ── CLI 入口 ─────────────────────────────────────────────────

def _build_arg_parser():
    import argparse

    p = argparse.ArgumentParser(
        prog="semantic-browser-crawler",
        description="增量 BFS 站内爬虫（基于 SemanticBrowser）",
    )
    p.add_argument("start_url", help="起始 URL")
    p.add_argument("--max-pages", type=int, default=20, help="最大页面数 (默认 20)")
    p.add_argument("--max-depth", type=int, default=3, help="最大深度 (默认 3)")
    p.add_argument(
        "--cross-domain", action="store_true",
        help="允许跨域名爬取（默认仅同域名）",
    )
    p.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=True,
        help="是否无头模式 (默认 --headless, 用 --no-headless 关闭)",
    )
    p.add_argument("--delay", type=float, default=0.5, help="请求间隔秒数 (默认 0.5)")
    p.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    return p


async def _cli_main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    crawler = Crawler(headless=args.headless, request_delay=args.delay)
    result = await crawler.crawl(
        start_url=args.start_url,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        same_domain_only=not args.cross_domain,
    )
    import json
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not result.failed_urls else 1


def main() -> None:
    """CLI 入口（同步包装）。"""
    raise SystemExit(asyncio.run(_cli_main()))


if __name__ == "__main__":
    main()
