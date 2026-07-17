"""T92: 真 HTTP cache 失效场景 e2e.

启动本地 mock HTTP server (用 stdlib http.server, 不依赖外部):
- /page 返回 HTML + ETag "v1" (首次 GET 返 200 + body)
- 修改 underlying content (PUT /page) 触发 ETag "v2"
- 客户端用 cache_freshness_check=True 跑:
  - 第一次 GET → 抓 ETag v1, 存 cache
  - 第二次 GET (content 没变) → HEAD 304, cache fresh
  - 改 content (ETag v2)
  - 第三次 GET → HEAD 200, cache stale → invalidate
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socketserver
import threading
import time
from urllib.parse import urlparse

import pytest

from semantic_browser.query import SemanticQuery


# ── Mock HTTP server ───────────────────────────────────────────────

PAGE_CONTENT_V1 = """<html><body><h1>Version 1</h1><p>Python 3.13 free-threading is experimental.</p></body></html>"""
PAGE_CONTENT_V2 = """<html><body><h1>Version 2</h1><p>Python 3.13 free-threading is experimental. UPDATED!</p></body></html>"""


class _MockHandler(http.server.BaseHTTPRequestHandler):
    """支持 ETag + HEAD + content mutation via PUT /update."""

    page_content = PAGE_CONTENT_V1
    etag = '"v1"'
    last_modified = "Wed, 21 Oct 2026 07:28:00 GMT"
    update_count = 0

    def log_message(self, format, *args):
        pass  # silent

    def do_HEAD(self):
        if self.path == "/page":
            # T92: 真正的 HTTP 304 行为 — 检查 If-None-Match / If-Modified-Since
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            print(f"[mock HEAD /page] inm={inm!r} ims={ims!r} server_etag={_MockHandler.etag!r}", flush=True)
            if inm and inm == _MockHandler.etag:
                self.send_response(304)
                self.send_header("ETag", _MockHandler.etag)
                self.end_headers()
                return
            if ims and ims == _MockHandler.last_modified:
                self.send_response(304)
                self.send_header("Last-Modified", _MockHandler.last_modified)
                self.end_headers()
                return
            # 没匹配 → 资源变了 (或者无 conditional header)
            self.send_response(200)
            self.send_header("ETag", _MockHandler.etag)
            self.send_header("Last-Modified", _MockHandler.last_modified)
            self.send_header("Content-Length", str(len(_MockHandler.page_content.encode())))
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self):
        if self.path == "/page":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("ETag", _MockHandler.etag)
            self.send_header("Last-Modified", _MockHandler.last_modified)
            self.send_header("Content-Length", str(len(_MockHandler.page_content.encode())))
            self.end_headers()
            self.wfile.write(_MockHandler.page_content.encode())
            return
        self.send_error(404)

    def do_PUT(self):
        # Test helper: 切换 content + 更新 ETag
        if self.path == "/update":
            _MockHandler.update_count += 1
            # 每次 PUT 都更新 content + 切到下一个 ETag (v2, v3, ...)
            new_version = _MockHandler.update_count + 1  # v2, v3, ...
            _MockHandler.page_content = PAGE_CONTENT_V2.replace("UPDATED!", f"UPDATED v{new_version}!")
            _MockHandler.etag = f'"v{new_version}"'
            print(f"[mock PUT /update] count={_MockHandler.update_count} new_etag={_MockHandler.etag}", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/json")
            self.end_headers()
            self.wfile.write(f'{{"updated": true, "etag": {_MockHandler.etag!r}}}'.encode())
            return
        self.send_error(404)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """多线程 HTTP server — 让 handler 不会阻塞主测试."""
    daemon_threads = True


def start_mock_server(port: int) -> http.server.HTTPServer:
    """Start mock HTTP server in a daemon thread."""
    server = _ThreadingHTTPServer(("127.0.0.1", port), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Wait for ready
    import urllib.request
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/page", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return server


def update_content(port: int):
    """调 PUT /update 触发内容变化."""
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/update", method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


# ── Tests ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mock_server():
    """Start mock server once per test module."""
    port = 18181
    server = start_mock_server(port)
    yield port
    server.shutdown()


class TestHttpCacheFreshnessE2E:
    """T92: 跑真 HTTP server, 验证 cache freshness check 真能失效 cache."""

    @pytest.mark.asyncio
    async def test_cache_hit_then_invalidate_on_content_change(self, mock_server):
        """T92: 1st query → cache miss + etag 存; 2nd query → cache hit (HEAD 304).
        3rd query (after PUT /update changes content) → HEAD 200 → cache stale.
        """
        sq = SemanticQuery(budget=200, cache_freshness_check=True)
        try:
            # 1st: cache miss + 抓 ETag v1
            r1 = await sq.run(
                "test query",
                start_url=f"http://127.0.0.1:{mock_server}/page",
            )
            assert r1.success, f"1st should succeed: {r1.error}"
            # cache 应已写入
            assert len(sq._cache) == 1

            # 2nd: HEAD 304 → cache hit
            t0 = time.time()
            r2 = await sq.run(
                "test query",
                start_url=f"http://127.0.0.1:{mock_server}/page",
            )
            t2 = time.time() - t0
            assert r2.tokens_used.get("cache_hit") is True, (
                f"2nd should be cache hit, got {r2.tokens_used}"
            )
            assert t2 < 1.0, f"2nd should be fast: {t2}s"

            # 改 content → ETag 从 v1 变 v2
            update_content(mock_server)

            # 3rd: HEAD If-None-Match: "v1" → server 现在 etag 是 "v2" → 返 200 → cache stale
            r3 = await sq.run(
                "test query",
                start_url=f"http://127.0.0.1:{mock_server}/page",
            )
            # cache 应该被失效 → cache_hit 应该是 None 或 False
            assert r3.tokens_used.get("cache_hit") in (None, False), (
                f"3rd should NOT be cache hit (content changed): "
                f"cache_hit={r3.tokens_used.get('cache_hit')}, "
                f"cache_freshness_checked={r3.tokens_used.get('cache_freshness_checked')}"
            )
            # freshness check 应该被触发了
            assert r3.tokens_used.get("cache_freshness_checked") is True
        finally:
            await sq.close()

    @pytest.mark.asyncio
    async def test_no_freshness_check_skips_head(self, mock_server):
        """cache_freshness_check=False (默认) → 不发 HEAD, 只靠 TTL."""
        sq = SemanticQuery(budget=200, cache_freshness_check=False)
        try:
            # 1st
            r1 = await sq.run(
                "test query",
                start_url=f"http://127.0.0.1:{mock_server}/page",
            )
            assert r1.success

            # 2nd: 直接 cache hit (没 HEAD check)
            r2 = await sq.run(
                "test query",
                start_url=f"http://127.0.0.1:{mock_server}/page",
            )
            assert r2.tokens_used.get("cache_hit") is True
            # 没 cache_freshness_checked 字段 (因为没做 freshness check)
            assert r2.tokens_used.get("cache_freshness_checked") is None or r2.tokens_used.get("cache_freshness_checked") is False
        finally:
            await sq.close()


class TestHttpCacheETagPersistence:
    """T92: 跨 cache miss → 再次 fetch → etag 持久化."""

    @pytest.mark.asyncio
    async def test_etag_persists_across_instances(self, tmp_path):
        """Instance A 跑 query + 存 cache + 写 etag
        Instance B 从磁盘加载 → 应该看到 etag (然后 HEAD 验证)"""
        cache_file = tmp_path / "cache.json"
        url = "http://127.0.0.1:18181/page"

        # Instance A: 跑 + 写 cache + 持久化 (包括 ETag)
        # 由于 mock server 不在, 我们手动构造 cache entry with ETag
        from semantic_browser.query.semantic_query import SemanticAnswer
        sq1 = SemanticQuery(budget=100, cache_persist_path=str(cache_file))
        ans = SemanticAnswer(query="test", answer="cached answer", success=True,
                            confidence=0.9, tokens_used={"used": {"total": 100}})
        ans._cached_etag = '"v1"'
        ans._cached_last_modified = "Wed, 21 Oct 2026 07:28:00 GMT"
        sq1._cache[("test", url)] = (time.time(), ans)
        sq1._save_cache(str(cache_file))

        # Instance B: 加载
        sq2 = SemanticQuery(budget=100, cache_persist_path=str(cache_file),
                            cache_freshness_check=True)
        try:
            # 检查内部状态
            cached = sq2._cache.get(("test", url))
            assert cached is not None
            ts, loaded_ans = cached
            assert loaded_ans._cached_etag == '"v1"', "etag not loaded from disk"
            assert loaded_ans._cached_last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
        finally:
            await sq2.close()
