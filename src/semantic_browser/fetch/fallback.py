"""T108: Fallback fetchers — 在主路径 (browser/antibot) 失败时找替代源.

不是反反爬 (那种要 Patchright + residential proxy, 单机搞不定).
是 "既然我读不到 Amazon, 那去找别人替我读过的 Amazon"。

Sources (顺序尝试):
1. archive.org Wayback Machine — 谁在某个时候替我抓过原 URL
2. (后续可加: DDG HTML / Bing cache / Google cache)

API:
    try_archive(url)        -> (html, final_url) | None
    try_all_fallbacks(url)  -> (html, final_url, source_label) | None

只返回 raw HTML. 后续还是走正常 snapshot/extract 路径, 只是 page
object 装的是 fallback 拿到的内容.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 用 httpx 还是 stdlib urllib — stdlib 0 dep, 但 timeout 调起来麻烦
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


_TIMEOUT = 20  # 秒


def _fetch(url: str, *, headers: dict | None = None, timeout: int = _TIMEOUT) -> Optional[str]:
    """简单 GET, 返回 text 或 None. 不抛."""
    try:
        req = Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (compatible; SemanticBrowser/1.0)")
        req.add_header("Accept", "text/html,application/json")
        req.add_header("Accept-Encoding", "gzip")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            # urllib auto-decompresses gzip if header was sent; 但内容也可能是
            # 已经是 raw text. 检查 content-type 决定是否 decode
            ct = resp.headers.get("Content-Type", "")
            if "text" in ct or "json" in ct or "html" in ct or "xml" in ct:
                # urllib 的 gzip 处理
                return raw.decode("utf-8", errors="replace")
            return raw.decode("utf-8", errors="replace")
    except (URLError, HTTPError, TimeoutError, Exception) as e:
        logger.warning("fetch failed for %s: %s", url, e)
        return None


def _cdx_lookup(url: str, timeout: int = _TIMEOUT) -> Optional[str]:
    """查 archive.org 找最近的可用 snapshot.

    用 /wayback/available 而不是 /cdx/search/cdx — available 端点
    比 CDX 友好 (不会 strict rate-limit 也单独报 CDXSCAPE), 返回的
    timestamp 直接可用.

    Returns: wayback raw URL `https://web.archive.org/web/{ts}id_/{url}` 或 None.
    """
    avail_url = (
        "https://archive.org/wayback/available"
        f"?url={urllib.parse.quote(url, safe=':/?&=')}"
    )
    body = _fetch(avail_url, timeout=timeout)
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    snap = data.get("archived_snapshots", {}).get("closest", {})
    if not snap or snap.get("status") != "200":
        return None
    if not snap.get("available"):
        return None
    ts = snap.get("timestamp")
    if not ts:
        return None
    # 用 id_ modifier 去掉 wayback toolbar (注入的 script 干扰 snapshot)
    return f"https://web.archive.org/web/{ts}id_/{url}"


def try_archive(url: str, *, timeout: int = _TIMEOUT) -> Optional[Tuple[str, str]]:
    """Try to fetch `url` via archive.org Wayback Machine.

    Returns: (final_wayback_url, html_content) 或 None.

    流程:
    1. CDX API 找最近 200 文本快照
    2. 用 `id_/` modifier 拉 raw HTML (无 wayback toolbar)
    """
    wayback = _cdx_lookup(url, timeout=timeout)
    if not wayback:
        logger.info("archive.org: no snapshot for %s", url)
        return None
    logger.info("archive.org: trying %s", wayback[:120])
    html = _fetch(wayback, timeout=timeout)
    if not html:
        return None
    if len(html) < 500:
        logger.info("archive.org: too-short response (%d bytes), skip", len(html))
        return None
    return (wayback, html)


def try_all_fallbacks(url: str, *, timeout: int = _TIMEOUT) -> Optional[Tuple[str, str, str]]:
    """Try all fallback sources in order. Returns (url, html, source_label) or None."""
    # 1. archive.org
    res = try_archive(url, timeout=timeout)
    if res:
        return (res[0], res[1], "archive.org wayback")
    return None
