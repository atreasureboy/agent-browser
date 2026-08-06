"""T40-T44 Security audit tools: recon, fingerprinting, vulnerability detection."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections import Counter
from typing import Any

from semantic_browser.browser._utils import (
    _version_lt,
    _tls_subdomains,
    _server_hint,
    _generator_hint,
)
from semantic_browser.snapshot.engine import SnapshotEngine

logger = logging.getLogger(__name__)


class _SecurityToolsMixin:
    """Security audit tools (T40-T44) — mixed into BrowserController."""

    # ── T40g: API endpoint extraction patterns ─────────────────────────

    _API_PATTERNS: tuple[tuple[str, str], ...] = (
        # fetch("...") / fetch(`...`)
        (r'''fetch\s*\(\s*[`"']([^`"']{3,300})[`"']''', "fetch"),
        # axios.<method>("...")
        (r'''axios\.(?:get|post|put|delete|patch|head|options)\s*\(\s*[`"']([^`"']{3,300})[`"']''', "axios"),
        # xhr.open("METHOD", "URL")
        (r'''\.open\s*\(\s*[`"'](?:GET|POST|PUT|DELETE|PATCH|HEAD)["']\s*,\s*[`"']([^`"']{3,300})[`"']''', "xhr"),
        # $.ajax({url: "..."})
        (r'''\$\.ajax\s*\(\s*\{[^}]*?url\s*:\s*[`"']([^`"']{3,300})[`"']''', "jquery"),
        # superagent / got: .get("/api/...") .post("/api/...")
        (r'''\.(?:get|post|put|delete|patch)\s*\(\s*[`"'](/[a-zA-Z][^`"']{2,300})[`"']''', "rest-method"),
    )

    # ── T42b: JS library fingerprinting ────────────────────────

    _JS_LIB_FINGERPRINTS: tuple[dict[str, Any], ...] = (
        {
            "name": "jQuery",
            "patterns": (
                r"jquery[/-](\d+\.\d+(?:\.\d+)?)",
                r"jquery[.-](\d+\.\d+(?:\.\d+)?)",
            ),
            "cves": (
                ("3.5.0", "CVE-2020-11022/CVE-2020-11023", "XSS via untrusted HTML passed to DOM manipulation methods"),
                ("3.0.0", "CVE-2019-11358", "Prototype pollution in jQuery.extend"),
                ("3.4.0", "CVE-2016-10706", "Prototype pollution via jQuery.uniqueSort"),
            ),
        },
        {
            "name": "AngularJS",
            "patterns": (r"angular[/-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (
                ("1.8.0", "CVE-2020-7676", "XSS in angular.copy"),
            ),
        },
        {
            "name": "Bootstrap",
            "patterns": (r"bootstrap[/-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (
                ("4.0.0", "CVE-2019-8331", "XSS in tooltip/popover data-template"),
            ),
        },
        {
            "name": "Lodash",
            "patterns": (r"lodash[.-](\d+\.\d+(?:\.\d+)?)", r"lodash@(\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("4.17.21", "CVE-2020-8203", "Prototype pollution in zipObjectDeep"),
            ),
        },
        {
            "name": "Moment.js",
            "patterns": (r"moment[.-](\d+\.\d+(?:\.\d+)?)", r"moment[/-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("2.29.0", "CVE-2022-24785", "Path traversal in moment.locale"),
            ),
        },
        {
            "name": "Vue.js",
            "patterns": (r"vue[/@](\d+\.\d+(?:\.\d+)?)", r"vue[.-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
        {
            "name": "React",
            "patterns": (r"react[/@](\d+\.\d+(?:\.\d+)?)", r"react[.-](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
        {
            "name": "Backbone.js",
            "patterns": (r"backbone[.-](\d+\.\d+(?:\.\d+)?)",),
            "cves": (),
        },
        {
            "name": "Handlebars",
            "patterns": (r"handlebars[.-](\d+\.\d+(?:\.\d+)?)", r"handlebars[/-]v?(\d+\.\d+(?:\.\d+)?)"),
            "cves": (
                ("4.3.0", "CVE-2019-19919", "Arbitrary code execution via lookup helper"),
                ("4.0.14", "CVE-2017-16016", "XSS via templates"),
            ),
        },
        {
            "name": "axios",
            "patterns": (r"axios[.-](\d+\.\d+(?:\.\d+)?)", r"axios[/@](\d+\.\d+(?:\.\d+)?)"),
            "cves": (),
        },
    )

    # ── T40b: Hidden paths probe ─────────────────────────────

    _WELL_KNOWN_PATHS: tuple[str, ...] = (
        "/.well-known/security.txt",
        "/.well-known/openid-configuration",
        "/.well-known/change-password",
        "/.well-known/apple-app-site-association",
        "/.well-known/assetlinks.json",
        "/.well-known/mta-sts.txt",
        "/.well-known/acme-challenge/",
    )
    _DISCOVERY_PATHS: tuple[str, ...] = (
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/llms.txt",
        "/humans.txt",
        "/manifest.json",
        "/crossdomain.xml",
        "/clientaccesspolicy.xml",
        "/.git/HEAD",
        "/.env",
    )
    _ADMIN_PATHS: tuple[str, ...] = (
        "/admin",
        "/admin/login",
        "/administrator",
        "/login",
        "/wp-admin/",
        "/wp-login.php",
        "/user/login",
        "/api",
        "/api/v1",
        "/graphql",
        "/cgi-bin/",
        "/phpmyadmin/",
        "/server-status",
        "/.htaccess",
    )
    _DEBUG_PATHS: tuple[str, ...] = (
        "/debug",
        "/debug/vars",
        "/debug/pprof",
        "/trace",
        "/actuator",
        "/actuator/env",
        "/actuator/health",
        "/actuator/info",
        "/actuator/metrics",
        "/actuator/beans",
        "/actuator/mappings",
        "/actuator/configprops",
        "/actuator/heapdump",
        "/actuator/threaddump",
        "/actuator/loggers",
        "/env",
        "/info",
        "/health",
        "/metrics",
        "/_debug",
        "/__debug__",
        "/_profiler",
        "/phpinfo.php",
        "/server-info",
        "/status",
        "/.env.production",
        "/.env.local",
        "/config",
        "/configuration",
        "/swagger",
        "/swagger-ui.html",
        "/swagger-ui/",
        "/v1/api-docs",
        "/v2/api-docs",
        "/v3/api-docs",
        "/openapi.json",
        "/openapi.yaml",
        "/api-docs",
        "/redoc",
        "/graphiql",
        "/playground",
    )

    # ── T39: fetch_script_source ─────────────────────────────────────────────

    async def fetch_script_source(self, url: str, *, timeout_ms: int = 5000) -> str:
        """T39: deep 模式专用 — 按 URL 抓 JS 源码 (httpx).

        不通过浏览器 — 因为浏览器里 fetch 受 CORS 限制.
        直接服务端 fetch (允许任意 origin), 给 agent 看完整 JS.
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
                r = await client.get(url)
                return r.text[:50000]  # 50K 上限, 防止 OOM
        except Exception as e:
            return f"(fetch failed: {type(e).__name__}: {e})"

    # ── T40g: extract_api_endpoints ──────────────────────────────────────────

    async def extract_api_endpoints(
        self,
        *,
        max_scripts: int = 25,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T40g: 从页面 JS 中提取 API endpoint.

        流程:
          1. page.evaluate 列出所有 <script src=...> (含 inline src)
          2. httpx 直抓每个 JS 源码 (避开 CORS)
          3. 走 _API_PATTERNS regex, 提取候选 URL/path
          4. 去重 + 分类 + 返回

        Returns {
          "page_url",
          "scripts_scanned": int,
          "scripts_failed": int,
          "endpoints": [
            {"value": "/api/users", "method": "GET", "sources": ["fetch"], "script": "https://..."},
            ...
          ],
          "by_method": {"GET": N, "POST": M, ...},
        }
        """
        import re
        import httpx
        from urllib.parse import urljoin

        page = await self._ensure_page()

        # 1. 列出 scripts (只 external, inline 太难 dedup)
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        # 限制总数
        scripts = scripts[:max_scripts]

        # 2. 转绝对 URL
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        endpoints: dict[str, dict[str, Any]] = {}
        scripts_scanned = 0
        scripts_failed = 0

        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:200000]  # 200K 上限
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue

                for pat, source in self._API_PATTERNS:
                    for m in re.finditer(pat, body, re.DOTALL):
                        val = m.group(1).strip()
                        if not val:
                            continue
                        # 过滤: 必须以 / 开头 (path) 或 http 开头 (absolute url)
                        if not (val.startswith("/") or val.startswith("http")):
                            continue
                        # 跳过太短/太通用
                        if len(val) < 3:
                            continue
                        if val in ("/", "//"):
                            continue
                        # 截断模板字符串 (含 ${} 或 backtick 不完整)
                        val = val.split("${")[0].rstrip("/")
                        if not val:
                            continue
                        ep = endpoints.setdefault(val, {
                            "value": val,
                            "sources": set(),
                            "scripts": set(),
                            "first_method": source,
                        })
                        ep["sources"].add(source)
                        ep["scripts"].add(url)

        # 3. 序列化 + 简单分类
        out_list = []
        by_source: dict[str, int] = {}
        for v, ep in sorted(endpoints.items()):
            out_list.append({
                "value": ep["value"],
                "sources": sorted(ep["sources"]),
                "scripts_count": len(ep["scripts"]),
            })
            for s in ep["sources"]:
                by_source[s] = by_source.get(s, 0) + 1

        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "endpoint_count": len(out_list),
            "endpoints": out_list,
            "by_source": by_source,
        }

    # ── T42b: extract_js_libraries ───────────────────────────────────────────

    async def extract_js_libraries(
        self,
        *,
        max_scripts: int = 30,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T42b: 从 <script src> URL 中识别 JS 库 + 版本 + 已知 CVE.

        流程:
          1. 收集所有 <script src=...> URLs
          2. 对每个 URL 用 _JS_LIB_FINGERPRINTS 里的 regex 扫
          3. 命中后解析版本, 对照已知 CVE 表 (用 < 字符串比对)
          4. 多个 URL 命中同一 lib 只保留版本最高的

        Returns {
          "page_url", "scripts_scanned", "scripts_failed",
          "libraries": [
            {"name", "version", "urls": [...], "cves": [{id, max_version, desc}]}
          ],
          "vulnerable_count": int  # 有 known CVE 的 lib 数
        }
        """
        import re
        from urllib.parse import urljoin

        def _vuln_to_cve_entry(threshold: str, cve_id: str, desc: str) -> dict[str, str]:
            return {"max_vuln_version": threshold, "id": cve_id, "desc": desc}

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        # 收集所有 URL 文本 (script src 字符串 + 未来可能 fetch 源码)
        url_corpus = "\n".join(abs_urls)
        scripts_scanned = len(abs_urls)
        scripts_failed = 0

        # 解析 lib 命中
        lib_hits: dict[str, dict[str, Any]] = {}
        for fp in self._JS_LIB_FINGERPRINTS:
            name = fp["name"]
            for pat in fp["patterns"]:
                for m in re.finditer(pat, url_corpus, re.IGNORECASE):
                    ver = m.group(1)
                    hit = lib_hits.setdefault(name, {
                        "name": name,
                        "_versions": {},  # ver -> [urls]
                        "cves": [],
                    })
                    hit["_versions"].setdefault(ver, set()).add(abs_urls[0] if not abs_urls else "")
                    # 找到对应的 url — 用 match.start() 反推
                    for u in abs_urls:
                        if m.group(0) in u:
                            hit["_versions"][ver].add(u)
                            break

        # 计算 CVE
        libraries_out = []
        vulnerable_count = 0
        for name, hit in lib_hits.items():
            fp = next((f for f in self._JS_LIB_FINGERPRINTS if f["name"] == name), None)
            if not fp:
                continue
            # 选最高版本
            best_ver = max(hit["_versions"].keys(), key=lambda v: tuple(int(x) for x in v.split(".")))
            # 选最 representative url (出现次数最多)
            best_urls = sorted(hit["_versions"][best_ver])
            cves: list[dict[str, str]] = []
            for threshold, cve_id, desc in fp["cves"]:
                if _version_lt(best_ver, threshold):
                    cves.append(_vuln_to_cve_entry(threshold, cve_id, desc))
            if cves:
                vulnerable_count += 1
            libraries_out.append({
                "name": name,
                "version": best_ver,
                "urls": best_urls[:5],
                "cves": cves,
            })
        libraries_out.sort(key=lambda x: x["name"])

        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "library_count": len(libraries_out),
            "libraries": libraries_out,
            "vulnerable_count": vulnerable_count,
        }

    # ── T42g: detect_graphql ─────────────────────────────────────────────────

    async def detect_graphql(
        self,
        endpoint: str,
        *,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        """T42g: 给定 GraphQL 端点 URL, 跑 introspection query dump schema.

        经典 introspection:
          {
            __schema {
              queryType { name }
              mutationType { name }
              types { name kind }
            }
          }

        Returns {
          "endpoint", "is_graphql": bool, "error": str or None,
          "query_type": str or None, "mutation_type": str or None,
          "types": [str, ...]   # 所有 type name
          "type_count": int,
        }
        """
        import httpx
        introspection = {
            "query": (
                "{ __schema { queryType { name } mutationType { name } "
                "types { name kind } } }"
            )
        }
        try:
            async with httpx.AsyncClient(
                timeout=timeout_ms / 1000,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "semantic-browser-probe/1.0",
                    "Accept": "application/json",
                },
            ) as client:
                r = await client.post(endpoint, json=introspection)
            if r.status_code >= 400:
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            try:
                data = r.json()
            except Exception as e:
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": f"non-JSON response: {e}"}
            if "data" not in data or "__schema" not in data.get("data", {}):
                return {"endpoint": endpoint, "is_graphql": False,
                        "error": "response missing __schema (likely not GraphQL)"}
            schema = data["data"]["__schema"]
            types = [t["name"] for t in schema.get("types", []) if not t["name"].startswith("__")]
            return {
                "endpoint": endpoint,
                "is_graphql": True,
                "error": None,
                "query_type": (schema.get("queryType") or {}).get("name"),
                "mutation_type": (schema.get("mutationType") or {}).get("name"),
                "types": sorted(types),
                "type_count": len(types),
            }
        except Exception as e:
            return {"endpoint": endpoint, "is_graphql": False,
                    "error": f"{type(e).__name__}: {e}"}

    # ── T40b: probe_paths ────────────────────────────────────────────────────

    async def probe_paths(
        self,
        base_url: str,
        *,
        categories: list[str] | None = None,
        timeout_ms: int = 5000,
        max_concurrency: int = 6,
    ) -> dict[str, Any]:
        """T40b: 探测常见隐藏路径 — 给 agent / 安全审计用.

        探测三类 path:
          - well_known:  /.well-known/* (RFC 8615 + 行业标准)
          - discovery:  robots.txt / sitemap.xml / .git/HEAD 等发现类
          - admin:      /admin /login /api /graphql 等常见管理/API 入口

        不通过浏览器 — 用 httpx 直发 (避开 CORS, 不污染浏览历史).
        所有 path 并发探测 (max_concurrency 控制并发).

        Args:
            base_url: 起点 URL, 自动从其中解析 origin
            categories: 子集白名单 (None = 全部三类); 可选 "well_known"/"discovery"/"admin"
            timeout_ms: 单 path 超时
            max_concurrency: 并发上限

        Returns: {
          "base_url", "origin",
          "found": [{"path", "status", "category", "content_type", "size", "redirect"}],
          "missing": [{"path", "category", "status": 404}],
          "total_probed": int,
          "duration_ms": int,
        }
        """
        import httpx
        import time as _time
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        wanted_cats = categories or ["well_known", "discovery", "admin", "debug"]
        all_paths: list[tuple[str, str]] = []
        if "well_known" in wanted_cats:
            all_paths += [("well_known", p) for p in self._WELL_KNOWN_PATHS]
        if "discovery" in wanted_cats:
            all_paths += [("discovery", p) for p in self._DISCOVERY_PATHS]
        if "admin" in wanted_cats:
            all_paths += [("admin", p) for p in self._ADMIN_PATHS]
        if "debug" in wanted_cats:  # T42f
            all_paths += [("debug", p) for p in self._DEBUG_PATHS]

        sem = asyncio.Semaphore(max_concurrency)
        found: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        soft_404_count = 0  # T42e
        t0 = _time.monotonic()

        # T42e: 第一次先拿一个肯定不存在的 path, 用它的 body length 作 soft-404 baseline.
        baseline_size: int | None = None
        try:
            async with httpx.AsyncClient(
                timeout=timeout_ms / 1000,
                follow_redirects=False,
                headers={"User-Agent": "semantic-browser-probe/1.0"},
            ) as client:
                r = await client.get(origin + "/zzz-sb-probe-nonexistent-zzz")
                baseline_size = len(r.content)
        except Exception:
            pass

        def _is_soft_404(content: bytes, status: int) -> bool:
            """T42e: 检测 soft-404 — 200 但内容是 404 页.
            启发式: 内容很短 (<= baseline+10%) 且包含 '404'/'not found' 关键字.
            """
            if status != 200 or baseline_size is None:
                return False
            size = len(content)
            # 体积异常小 (与 baseline 几乎一致, 误差 < 10%)
            if baseline_size > 0 and abs(size - baseline_size) < max(50, baseline_size * 0.10):
                text = content[:5000].decode("utf-8", errors="ignore").lower()
                if "404" in text or "not found" in text or "page not found" in text:
                    return True
            return False

        async def _probe_one(cat: str, path: str) -> None:
            nonlocal soft_404_count
            url = origin + path
            try:
                async with sem:
                    async with httpx.AsyncClient(
                        timeout=timeout_ms / 1000,
                        follow_redirects=False,
                        headers={"User-Agent": "semantic-browser-probe/1.0"},
                    ) as client:
                        r = await client.get(url)
                status = r.status_code
                entry: dict[str, Any] = {
                    "path": path,
                    "status": status,
                    "category": cat,
                    "url": url,
                }
                if status in (200, 301, 302, 307, 308, 401, 403):
                    entry["content_type"] = r.headers.get("content-type", "")
                    entry["size"] = len(r.content)
                    if 300 <= status < 400:
                        entry["redirect"] = r.headers.get("location", "")
                    # T42e: soft-404 标记
                    if status == 200 and _is_soft_404(r.content, status):
                        entry["soft_404"] = True
                        soft_404_count += 1
                    found.append(entry)
                else:
                    missing.append({"path": path, "category": cat, "status": status})
            except Exception as e:
                missing.append({
                    "path": path, "category": cat,
                    "status": -1, "error": f"{type(e).__name__}: {e}",
                })

        await asyncio.gather(*[_probe_one(c, p) for c, p in all_paths])

        return {
            "base_url": base_url,
            "origin": origin,
            "found": sorted(found, key=lambda x: (x["category"], x["path"])),
            "missing": sorted(missing, key=lambda x: (x["category"], x["path"])),
            "total_probed": len(all_paths),
            "soft_404_count": soft_404_count,  # T42e
            "duration_ms": int((_time.monotonic() - t0) * 1000),
        }

    # ── T43a: enumerate_subdomains ───────────────────────────────────────────

    async def enumerate_subdomains(
        self,
        host: str,
        *,
        include_tls_san: bool = True,
        crtsh_timeout: float = 12.0,
    ) -> dict[str, Any]:
        """T43a: 子域名枚举 — pen-tester recon 第一步.

        1. crt.sh JSON API (Certificate Transparency logs)
        2. 可选: TLS cert SAN 解析 (fallback / 补全)

        Returns {
          "host",
          "subdomains": [sorted unique list ending in host],
          "by_source": {"crtsh": N, "tls_san": M},
          "crtsh_status": "ok" | "timeout" | "error",
          "crtsh_error": str | None,
          "subdomain_count": int,
        }
        """
        import json as _json
        from urllib.request import urlopen, Request
        from urllib.error import URLError

        seen: dict[str, set[str]] = {}
        crtsh_status = "ok"
        crtsh_error: str | None = None

        # 1) crt.sh
        try:
            url = f"https://crt.sh/?q={host}&output=json"
            req = Request(url, headers={"User-Agent": "semantic-browser-recon/1.0"})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urlopen(req, timeout=crtsh_timeout, context=ctx) as r:  # noqa: S310 - intentional CT log query
                body = r.read()
            entries = _json.loads(body)
            for e in entries:
                nv = e.get("name_value") or ""
                for s in nv.split("\n"):
                    s = s.strip().lower().lstrip("*.")
                    if not s or s == host:
                        continue
                    if s.endswith("." + host) or s == host:
                        seen.setdefault(s, set()).add("crtsh")
        except (URLError, TimeoutError, ValueError) as e:
            crtsh_status = "error" if "timeout" not in str(e).lower() else "timeout"
            crtsh_error = str(e)[:200]

        # 2) TLS SAN (optional)
        if include_tls_san:
            for s in _tls_subdomains(host):
                seen.setdefault(s, set()).add("tls_san")

        subdomains = sorted(seen.keys())
        by_source: dict[str, int] = {}
        for srcs in seen.values():
            for src in srcs:
                by_source[src] = by_source.get(src, 0) + 1
        return {
            "host": host,
            "subdomains": subdomains,
            "by_source": by_source,
            "crtsh_status": crtsh_status,
            "crtsh_error": crtsh_error,
            "subdomain_count": len(subdomains),
        }

    # ── T43b: extract_secrets_from_js ────────────────────────────────────────

    async def extract_secrets_from_js(
        self,
        *,
        max_scripts: int = 20,
        max_body: int = 200_000,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        r"""T43b: 扫页面所有 <script src> 源码, 找硬编码 secret.

        模式:
          - AWS access key:    AKIA[0-9A-Z]{16}
          - AWS secret key:    [A-Za-z0-9/+=]{40} 紧跟 aws_secret
          - GitHub token:      ghp_[A-Za-z0-9]{36} / gho_/ghs_/ghr_
          - Slack token:       xox[baprs]-[A-Za-z0-9-]+
          - Google API key:    AIza[0-9A-Za-z_-]{35}
          - Generic Bearer:    Bearer [A-Za-z0-9._-]{20,}
          - api_key=:          api[_-]?key["']?\s*[:=]\s*["']?([A-Za-z0-9_\-]{16,})
          - password=:         (?:password|passwd|pwd)\s*[:=]\s*["']([^"']{6,})["']
          - private key:       -----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----

        Returns {
          "page_url",
          "scripts_scanned", "scripts_failed",
          "findings": [
            {"type", "value" (truncated 80), "script", "sample" (50 chars around match)},
            ...
          ],
          "by_type": {"aws_access_key": N, "github_token": M, ...},
          "secret_count": int,
        }
        """
        import re as _re
        from urllib.parse import urljoin
        import httpx

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        # secret patterns: (name, regex, group_idx_for_value)
        patterns: list[tuple[str, _re.Pattern[str], int]] = [
            ("aws_access_key", _re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
            ("github_token",   _re.compile(r"\b(gh[ps]_[A-Za-z0-9]{36})\b"), 1),
            ("slack_token",    _re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"), 1),
            ("google_api_key", _re.compile(r"\b(AIza[0-9A-Za-z_-]{35})\b"), 1),
            ("bearer",         _re.compile(r"Bearer\s+([A-Za-z0-9._\-]{20,})"), 1),
            ("api_key",        _re.compile(r"""api[_-]?key["']?\s*[:=]\s*["']?([A-Za-z0-9_\-]{16,})""", _re.IGNORECASE), 1),
            ("password",       _re.compile(r"""(?:password|passwd|pwd)\s*[:=]\s*["']([^"']{6,})["']""", _re.IGNORECASE), 1),
            ("private_key",    _re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), 0),
        ]

        findings: list[dict[str, Any]] = []
        scripts_scanned = 0
        scripts_failed = 0
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:max_body]
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue
                for name, pat, g in patterns:
                    for m in pat.finditer(body):
                        val = m.group(g) if g else m.group(0)
                        start = max(0, m.start() - 30)
                        end = min(len(body), m.end() + 30)
                        sample = body[start:end].replace("\n", " ")
                        findings.append({
                            "type": name,
                            "value": (val or "")[:80],
                            "script": url,
                            "sample": sample[:120],
                        })

        # dedup by (type, value, script)
        seen = set()
        uniq = []
        for f in findings:
            k = (f["type"], f["value"], f["script"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(f)

        by_type: dict[str, int] = {}
        for f in uniq:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "findings": uniq,
            "by_type": by_type,
            "secret_count": len(uniq),
        }

    # ── T43c: detect_waf ─────────────────────────────────────────────────────

    async def detect_waf(self) -> dict[str, Any]:
        """T43c: WAF 指纹 — 综合 response headers / cookies / 页面内容.

        检测对象: Cloudflare, Akamai, Imperva, AWS WAF, Fastly, Vercel, Netlify, Sucuri.

        Returns {
          "page_url",
          "detected": [waf_name, ...],     # 可能多个 (WAF 链)
          "signals": [{waf, indicator, value, kind: "header"|"cookie"|"content"}],
          "confidence": "high" | "medium" | "low" | "none",
        }
        """
        page = await self._ensure_page()
        # 1) 当前页的 response headers (来自最近一次请求)
        try:
            resp = await page.request.fetch(page.url, method="GET", max_redirects=5)
            headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        except Exception:
            headers = {}
        # 也加上 document 的 main resource headers
        try:
            main_resource = await page.evaluate("() => performance.getEntriesByType('navigation')[0] || {}")
            for k, v in (main_resource or {}).items():
                if isinstance(v, str) and ":" in v:
                    pass  # not standard headers
        except Exception:
            pass

        # 2) cookies
        try:
            cookies = await self._context.cookies()
            cookie_names = {c.get("name", "").lower() for c in cookies}
        except Exception:
            cookie_names = set()

        # 3) 页面内容 (title + meta)
        try:
            content = await page.content()
        except Exception:
            content = ""
        content_lower = content[:20000].lower()

        # WAF signatures: (name, header_pattern, cookie_pattern, content_pattern)
        # pattern = None means skip
        waf_sigs: list[tuple[str, str | None, str | None, str | None]] = [
            ("Cloudflare", r"cloudflare|cf-ray|cf-cache-status", r"__cfuid|cf_clearance|cf_bm", r"cloudflare"),
            ("Akamai",     r"akamai|x-akamai", r"_abck|ak_bmsc|bmuid", r"akamai"),
            ("Imperva",    r"x-iinfo|x-cdn|incapsula", r"incap_ses|visid_incap|nlbi_", r"incapsula|imperva"),
            ("AWS WAF",    r"x-amzn-waf|awsalb|awselb|x-amz-cf-id", r"awsalb|awselb", None),
            ("Fastly",     r"x-served-by|x-fastly|x-fasto", r"fastly", None),
            ("Vercel",     r"x-vercel-id|server:\s*vercel", r"__vercel", None),
            ("Netlify",    r"server:\s*netlify|x-nf-request-id", r"netlify", None),
            ("Sucuri",     r"x-sucuri-id|x-sucuri-cache", r"sucuri", r"sucuri"),
            ("CloudFront", r"x-amz-cf-id|x-amz-cf-pop|via:\s*cloudfront", None, None),
            ("Wordfence",  None, r"wfwaf-authcookie|wfvt_", r"wordfence"),
        ]

        signals: list[dict[str, str]] = []
        detected: list[str] = []
        import re as _re
        for waf, hp, cp, ctp in waf_sigs:
            hit = False
            if hp:
                for k, v in headers.items():
                    if _re.search(hp, f"{k}: {v}", _re.IGNORECASE):
                        signals.append({"waf": waf, "indicator": f"{k}: {v[:60]}", "kind": "header"})
                        hit = True
                        break
            if not hit and cp:
                for cn in cookie_names:
                    if _re.search(cp, cn, _re.IGNORECASE):
                        signals.append({"waf": waf, "indicator": f"cookie: {cn}", "kind": "cookie"})
                        hit = True
                        break
            if not hit and ctp and _re.search(ctp, content_lower, _re.IGNORECASE):
                signals.append({"waf": waf, "indicator": f"content match: {ctp}", "kind": "content"})
                hit = True
            if hit:
                detected.append(waf)

        if len(detected) >= 2:
            confidence = "high"
        elif len(detected) == 1:
            # 多个 signals → high, 单 signal → medium
            waf_signals = [s for s in signals if s["waf"] == detected[0]]
            confidence = "high" if len(waf_signals) >= 2 else "medium"
        else:
            confidence = "none"
        return {
            "page_url": page.url,
            "detected": detected,
            "signals": signals,
            "confidence": confidence,
        }

    # ── T43d: find_open_redirect_sinks ───────────────────────────────────────

    async def find_open_redirect_sinks(self) -> dict[str, Any]:
        """T43d: 扫页面所有链接 + form action, 找可能开放重定向/SSRF 的参数.

        Sink params: returnUrl, redirect, url, next, return, return_to, continue,
                     back, target, redir, redirect_uri, callback, image, fetch
        Sink 路径:   /api/redirect?url=, /login?next=, /logout?redirect=
        Returns {
          "page_url",
          "sinks": [
            {"source": "link" | "form", "href": "...", "param": "next", "value": "/dashboard"},
            ...
          ],
          "sink_count": int,
        }
        """
        import re as _re
        page = await self._ensure_page()
        # 抓所有 link href + form action
        links = await page.evaluate("""() => {
            const out = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const h = a.getAttribute('href');
                if (h) out.push(h);
            }
            for (const f of document.querySelectorAll('form[action]')) {
                const a = f.getAttribute('action');
                if (a) out.push(a);
            }
            return out;
        }""")
        # sink param names (lowercase)
        SINK_PARAMS = {
            "returnurl", "redirect", "url", "next", "return",
            "return_to", "continue", "back", "target", "redir",
            "redirect_uri", "callback", "image", "fetch", "site",
            "view", "page", "dest", "destination", "out",
        }
        # sink path patterns
        SINK_PATHS = _re.compile(r"/(?:api/redirect|login|logout|oauth/authorize|auth/callback)", _re.IGNORECASE)

        sinks: list[dict[str, str]] = []
        seen = set()
        for href in links:
            # 拆 query
            if "?" not in href:
                # 也看 path 模式
                if SINK_PATHS.search(href):
                    key = ("path", href)
                    if key not in seen:
                        seen.add(key)
                        sinks.append({"source": "path", "href": href[:300], "param": "path", "value": href[:120]})
                continue
            path_part, _, query = href.partition("?")
            try:
                from urllib.parse import parse_qs
                params = parse_qs(query)
            except Exception:
                continue
            for k, vals in params.items():
                if k.lower() in SINK_PARAMS:
                    v = vals[0] if vals else ""
                    key = (k, v, path_part[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    sinks.append({
                        "source": "query",
                        "href": href[:300],
                        "param": k,
                        "value": v[:200],
                    })
            if SINK_PATHS.search(path_part):
                key = ("path", path_part)
                if key not in seen:
                    seen.add(key)
                    sinks.append({
                        "source": "path",
                        "href": href[:300],
                        "param": "path",
                        "value": path_part[:200],
                    })
        return {
            "page_url": page.url,
            "sinks": sinks[:100],
            "sink_count": len(sinks),
        }

    # ── T43e: find_disclosure ──────────────────────────────────────────────

    async def find_disclosure(self) -> dict[str, Any]:
        """T43e: 扫页面 HTML 找敏感泄露.

        检测:
          - email
          - 内网 IP (RFC1918 + 127.x + 169.254.x)
          - AWS access key (AKIA[0-9A-Z]{16})
          - GitHub token (gh*_*)
          - Private key header
          - debug 字符串 ("Stack trace", "Exception in", "DEBUG =", "Traceback")
          - 注释里的 TODO/FIXME/HACK/XXX

        Returns {
          "page_url",
          "findings": [{type, value, context}],
          "by_type": {email: N, internal_ip: M, ...},
        }
        """
        import re as _re
        page = await self._ensure_page()
        content = await page.content()

        patterns: list[tuple[str, _re.Pattern[str], int]] = [
            ("email",       _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), 0),
            ("internal_ip", _re.compile(r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|127\.\d+\.\d+\.\d+|169\.254\.\d+\.\d+)\b"), 0),
            ("aws_key",     _re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), 1),
            ("github_tok",  _re.compile(r"\b(gh[ps]_[A-Za-z0-9]{36})\b"), 1),
            ("private_key", _re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), 0),
            ("debug_str",   _re.compile(r"(?i)(?:stack trace|traceback \(most recent|exception in|debug\s*=\s*True|tb_last)"), 0),
            ("code_marker", _re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]"), 0),
        ]

        findings: list[dict[str, str]] = []
        seen = set()
        for name, pat, g in patterns:
            for m in pat.finditer(content):
                val = m.group(g) if g else m.group(0)
                start = max(0, m.start() - 30)
                end = min(len(content), m.end() + 30)
                ctx = content[start:end].replace("\n", " ")[:120]
                k = (name, val[:80])
                if k in seen:
                    continue
                seen.add(k)
                findings.append({
                    "type": name,
                    "value": (val or "")[:120],
                    "context": ctx,
                })
        by_type: dict[str, int] = {}
        for f in findings:
            by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        return {
            "page_url": page.url,
            "findings": findings[:200],
            "by_type": by_type,
            "finding_count": len(findings),
        }

    # ── T43f: analyze_exposed_files ─────────────────────────────────────────

    async def analyze_exposed_files(
        self,
        base_url: str | None = None,
        *,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T43f: 探常见备份/源码/配置文件, 解析暴露内容.

        探针:
          /.git/HEAD, /.git/config, /.svn/entries
          /.env, /.env.local, /.env.production
          /.DS_Store, /Thumbs.db
          /backup.zip, /backup.tar.gz, /dump.sql, /db.sqlite
          /phpinfo.php, /server-status, /server-info
          /wp-config.php.bak, /config.php.bak, /config.yml.bak

        Returns {
          "base_url",
          "exposed": [
            {"path", "status", "size", "kind": "git"|"env"|"backup"|"config"|"other", "info": {...}}
          ],
          "exposed_count": int,
        }
        """
        import re as _re
        from urllib.parse import urlparse
        import httpx

        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"

        PROBES = [
            ("/.git/HEAD", "git"),
            ("/.git/config", "git"),
            ("/.svn/entries", "svn"),
            ("/.env", "env"),
            ("/.env.local", "env"),
            ("/.env.production", "env"),
            ("/.DS_Store", "macos"),
            ("/Thumbs.db", "windows"),
            ("/backup.zip", "backup"),
            ("/backup.tar.gz", "backup"),
            ("/dump.sql", "backup"),
            ("/db.sqlite", "backup"),
            ("/db.sqlite3", "backup"),
            ("/phpinfo.php", "phpinfo"),
            ("/server-status", "apache"),
            ("/server-info", "apache"),
            ("/wp-config.php.bak", "config"),
            ("/config.php.bak", "config"),
            ("/config.yml.bak", "config"),
            ("/.htaccess", "htaccess"),
            ("/web.config", "config"),
        ]

        exposed: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-recon/1.0"},
            follow_redirects=False,
        ) as client:
            for path, kind in PROBES:
                url = origin + path
                try:
                    r = await client.get(url)
                except Exception:
                    continue
                if r.status_code >= 400:
                    continue
                body = r.content[:20000]
                size = len(body)
                info: dict[str, Any] = {}
                if kind == "git":
                    text = body.decode("utf-8", errors="ignore").strip()
                    info["ref"] = text[:120]
                    if text.startswith("ref:"):
                        info["branch"] = text.split("/")[-1].strip()
                elif kind == "env":
                    text = body.decode("utf-8", errors="ignore")
                    # 只列 key 不列 value (避免误报出真密码)
                    keys = []
                    for line in text.splitlines():
                        if "=" in line and not line.strip().startswith("#"):
                            k = line.split("=", 1)[0].strip()
                            if k and _re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                                keys.append(k)
                    info["key_count"] = len(keys)
                    info["keys_sample"] = keys[:10]
                elif kind == "phpinfo":
                    text = body.decode("utf-8", errors="ignore")
                    v = _re.search(r"PHP Version\s*=>\s*([\d.]+)", text)
                    if v:
                        info["php_version"] = v.group(1)
                    else:
                        info["php_version"] = "unknown"
                elif kind == "apache":
                    text = body.decode("utf-8", errors="ignore")
                    if "Apache" in text:
                        info["server"] = "Apache (status page exposed)"
                elif kind in ("backup", "svn", "macos", "windows", "config", "htaccess"):
                    text = body.decode("utf-8", errors="ignore")
                    if kind == "htaccess":
                        # 只看第一行 (RewriteRule / Deny / AuthType)
                        info["first_line"] = text.splitlines()[0][:120] if text else ""
                exposed.append({
                    "path": path,
                    "status": r.status_code,
                    "size": size,
                    "kind": kind,
                    "info": info,
                })
        return {
            "base_url": base_url,
            "exposed": exposed,
            "exposed_count": len(exposed),
        }

    # ── T43g: discover_api_specs ───────────────────────────────────────────

    async def discover_api_specs(
        self,
        base_url: str | None = None,
        *,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T43g: 探常见 OpenAPI / Swagger 路径, 解析 path + method.

        探针:
          /swagger.json, /openapi.json, /api/swagger.json,
          /api/openapi.json, /api/v1/openapi.json, /api/v2/openapi.json,
          /v3/api-docs, /api-docs, /swagger/v1/swagger.json

        Returns {
          "base_url",
          "specs": [
            {"url", "version", "title", "path_count", "method_count", "by_method": {GET:N,POST:M}}
          ],
          "spec_count": int,
        }
        """
        from urllib.parse import urlparse
        import httpx

        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"

        PROBES = [
            "/swagger.json", "/openapi.json",
            "/api/swagger.json", "/api/openapi.json",
            "/api/v1/openapi.json", "/api/v2/openapi.json",
            "/api/v1/swagger.json",
            "/v3/api-docs", "/api-docs",
            "/swagger/v1/swagger.json",
        ]

        specs: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-recon/1.0", "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for path in PROBES:
                url = origin + path
                try:
                    r = await client.get(url)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    doc = r.json()
                except Exception:
                    continue
                if not isinstance(doc, dict):
                    continue
                # OpenAPI 3: doc.get("openapi").startswith("3.")
                # Swagger 2:  doc.get("swagger") == "2.0"
                is_spec = (
                    (isinstance(doc.get("openapi"), str) and doc["openapi"].startswith("3."))
                    or doc.get("swagger") == "2.0"
                    or (isinstance(doc.get("paths"), dict) and doc["paths"])
                )
                if not is_spec:
                    continue
                paths = doc.get("paths", {}) or {}
                by_method: dict[str, int] = {}
                for p, ops in paths.items():
                    if isinstance(ops, dict):
                        for m in ops:
                            if m.lower() in ("get", "post", "put", "delete", "patch", "options", "head"):
                                by_method[m.upper()] = by_method.get(m.upper(), 0) + 1
                info = doc.get("info", {}) or {}
                specs.append({
                    "url": url,
                    "version": doc.get("openapi") or doc.get("swagger") or "unknown",
                    "title": info.get("title", ""),
                    "path_count": len(paths),
                    "method_count": sum(by_method.values()),
                    "by_method": by_method,
                    "sample_paths": list(paths.keys())[:5],
                })
        return {
            "base_url": base_url,
            "specs": specs,
            "spec_count": len(specs),
        }

    # ── T43h: tls_subdomains ────────────────────────────────────────────────

    async def tls_subdomains(self, host: str, port: int = 443, timeout: float = 5.0) -> dict[str, Any]:
        """T43h: TLS 证书 SAN 解析 — 取 subjectAltName / issuer / 有效期.

        Returns {
          "host", "tls_version", "issuer" (str), "not_before", "not_after",
          "sans" (sorted unique DNS list), "san_count",
          "subdomains" (sans ending with host),
        }
        """
        from datetime import datetime, timezone
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ss:
                    der = ss.getpeercert(binary_form=True)
                    cert = ss.getpeercert(binary_form=False) or {}
                    tls_version = ss.version()
            # parse SANs from binary form via regex on DER (subjectAltName ext OID 2.5.29.17)
            import re as _re
            sans = []
            # 1) 优先用 binary_form=True 的 SAN
            try:
                for entry in cert.get("subjectAltName", []):
                    if entry and entry[0].lower() == "dns":
                        sans.append(entry[1].lower())
            except Exception:
                pass
            # 2) fallback: parse DER bytes for SAN extension (crude: 找 DNS: 后的 host)
            if not sans and der:
                text = der.decode("latin-1", errors="ignore")
                # 找 DNS: 后的 fqdn 字符
                for m in _re.finditer(r"DNS:([A-Za-z0-9._-]+)", text):
                    sans.append(m.group(1).lower())
            sans = sorted(set(sans))
            # issuer
            issuer = ""
            try:
                iret = cert.get("issuer", ())
                if iret:
                    parts = []
                    for tup in iret:
                        for k, v in tup:
                            if k == "commonName":
                                parts.append(v)
                            elif k == "organizationName":
                                parts.insert(0, v)
                    issuer = ", ".join(parts)
            except Exception:
                pass
            # not_before / not_after → ISO
            def _parse_dt(s: str | None) -> str | None:
                if not s:
                    return None
                try:
                    return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).isoformat()
                except Exception:
                    return s
            subs = sorted({s for s in sans if s == host or s.endswith("." + host)})
            return {
                "host": host,
                "tls_version": tls_version,
                "issuer": issuer,
                "not_before": _parse_dt(cert.get("notBefore")),
                "not_after": _parse_dt(cert.get("notAfter")),
                "sans": sans,
                "san_count": len(sans),
                "subdomains": subs,
            }
        except Exception as e:
            return {
                "host": host,
                "error": str(e)[:200],
                "sans": [],
                "san_count": 0,
                "subdomains": [],
            }

    # ── T43i: fingerprint_tech ────────────────────────────────────────────────

    async def fingerprint_tech(self) -> dict[str, Any]:
        """T43i: 综合 meta / cookie / header 推断技术栈.

        检测:
          - Server / X-Powered-By / X-AspNet-Version / X-Runtime (Rails)
          - meta name=generator (WordPress / Drupal / Ghost 版本)
          - 框架 session cookie: PHPSESSID, JSESSIONID, ASP.NET_SessionId, _rails_session, connect.sid, JSESSIONID
          - 已知 meta name 模式

        Returns {
          "page_url",
          "server": str, "x_powered_by": str, "generator": str,
          "framework_cookies": [name, ...],
          "signals": [{kind, name, value, hint}],
        }
        """
        page = await self._ensure_page()
        # 1) headers from current page response
        try:
            resp = await page.request.fetch(page.url, method="GET", max_redirects=5)
            headers = {k.lower(): v for k, v in (resp.headers or {}).items()}
        except Exception:
            headers = {}
        # 2) meta generator
        generator = ""
        try:
            generator = await page.evaluate("""() => {
                const m = document.querySelector('meta[name="generator"]');
                return m ? m.getAttribute('content') || '' : '';
            }""")
        except Exception:
            generator = ""
        # 3) cookies
        try:
            cookies = await self._context.cookies()
            cookie_names = [c.get("name", "") for c in cookies]
        except Exception:
            cookie_names = []

        # 框架 cookie 签名
        FRAMEWORK_COOKIE_HINTS = {
            "PHPSESSID": "PHP",
            "JSESSIONID": "Java (Tomcat/jetty)",
            "ASP.NET_SessionId": "ASP.NET",
            "_rails_session": "Ruby on Rails",
            "connect.sid": "Express (Node.js)",
            "sessionid": "Django",
            "csrftoken": "Django / generic",
            "laravel_session": "Laravel (PHP)",
            "XSRF-TOKEN": "Laravel / generic",
            "wp-settings-": "WordPress",
            "wordpress_logged_in": "WordPress",
            "ghost": "Ghost (blog)",
            "shopify_session": "Shopify",
            "mage-cache-storage": "Magento",
        }
        framework_cookies: list[dict[str, str]] = []
        for cn in cookie_names:
            cn_l = cn.lower()
            for sig, hint in FRAMEWORK_COOKIE_HINTS.items():
                if sig.lower() in cn_l:
                    framework_cookies.append({"name": cn, "hint": hint})
                    break

        signals: list[dict[str, str]] = []
        srv = headers.get("server", "")
        if srv:
            signals.append({"kind": "header", "name": "server", "value": srv, "hint": _server_hint(srv)})
        xpb = headers.get("x-powered-by", "")
        if xpb:
            signals.append({"kind": "header", "name": "x-powered-by", "value": xpb, "hint": xpb})
        aspv = headers.get("x-aspnet-version") or headers.get("x-aspnetmvc-version")
        if aspv:
            signals.append({"kind": "header", "name": "x-aspnet-version", "value": aspv, "hint": "ASP.NET"})
        runtime = headers.get("x-runtime", "")
        if runtime:
            signals.append({"kind": "header", "name": "x-runtime", "value": runtime, "hint": "Ruby/Rails"})
        if generator:
            signals.append({"kind": "meta", "name": "generator", "value": generator, "hint": _generator_hint(generator)})
        for fc in framework_cookies:
            signals.append({"kind": "cookie", "name": fc["name"], "value": "", "hint": fc["hint"]})
        return {
            "page_url": page.url,
            "server": srv,
            "x_powered_by": xpb,
            "generator": generator,
            "framework_cookies": framework_cookies,
            "signals": signals,
        }

    # ── T43j: decode_jwts ──────────────────────────────────────────────────

    async def decode_jwts(self) -> dict[str, Any]:
        """T43j: 在 localStorage / sessionStorage / cookie / 页面内容中找 JWT, 解码 payload.

        JWT 格式: header.payload.signature (base64url 编码)
        解码 header + payload (不做签名校验, 仅供 agent 看清结构).
        Returns {
          "page_url",
          "tokens": [
            {"source": "localStorage"|"cookie"|"page", "key": "name", "token": "...", "header": {...}, "payload": {...}, "is_expired": bool}
          ],
          "token_count": int,
        }
        """
        import re as _re
        import base64
        import json as _json
        page = await self._ensure_page()
        # 1) storage
        storage = await self.get_storage()
        # 2) page content (HTML + inline scripts)
        try:
            content = await page.content()
        except Exception:
            content = ""
        # 3) cookies
        cookies = storage.get("cookies", []) or []

        def _b64url_decode(s: str) -> bytes | None:
            try:
                pad = "=" * (-len(s) % 4)
                return base64.urlsafe_b64decode(s + pad)
            except Exception:
                return None

        JWT_RE = _re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,})\.(eyJ[A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\b")
        tokens: list[dict[str, Any]] = []
        seen = set()

        def _record(source: str, key: str, token: str) -> None:
            if token in seen:
                return
            seen.add(token)
            h, p, s = token.split(".", 2)
            header_raw = _b64url_decode(h)
            payload_raw = _b64url_decode(p)
            try:
                header = _json.loads(header_raw) if header_raw else {}
            except Exception:
                header = {"_raw": header_raw.decode("utf-8", errors="ignore")[:80]}
            try:
                payload = _json.loads(payload_raw) if payload_raw else {}
            except Exception:
                payload = {"_raw": payload_raw.decode("utf-8", errors="ignore")[:200]}
            expired = False
            if isinstance(payload, dict):
                exp = payload.get("exp")
                if isinstance(exp, (int, float)):
                    import time as _t
                    expired = exp < _t.time()
            tokens.append({
                "source": source,
                "key": key,
                "token": token[:80] + ("..." if len(token) > 80 else ""),
                "header": header,
                "payload": payload,
                "is_expired": expired,
            })

        # localStorage / sessionStorage
        for kind in ("localStorage", "sessionStorage"):
            for k, v in (storage.get(kind) or {}).items():
                for m in JWT_RE.finditer(v or ""):
                    _record(kind, k, m.group(0))
        # cookies (value 直接是 JWT 或含 JWT)
        for c in cookies:
            v = c.get("value", "") or ""
            for m in JWT_RE.finditer(v):
                _record("cookie", c.get("name", ""), m.group(0))
        # page content
        for m in JWT_RE.finditer(content):
            _record("page", "(html)", m.group(0))

        return {
            "page_url": page.url,
            "tokens": tokens,
            "token_count": len(tokens),
        }

    # ── T44a: dns_records ──────────────────────────────────────────────────

    async def dns_records(
        self,
        host: str,
        *,
        doh_endpoint: str = "https://dns.google/resolve",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """T44a: DNS 记录查询 — 用 DoH (DNS-over-HTTPS) 避开 dig 依赖.

        查询类型: A / AAAA / MX / NS / TXT (SPF 提取) / _dmarc.<host>.TXT (DMARC 提取).
        Returns {
          "host",
          "a":       [ip, ...],
          "aaaa":    [ip, ...],
          "mx":      [{priority, exchange}, ...],
          "ns":      [ns_host, ...],
          "spf":     [spf_record, ...] (从 TXT 提取 v=spf1),
          "dmarc":   [dmarc_record, ...] (从 _dmarc.<host>.TXT),
          "security_grade": "ok" | "weak" | "missing"   (spf + dmarc + mx 综合),
          "notes":   [str, ...]  (pen-tester 视角的解读),
          "errors":  {rtype: err, ...}  (部分失败不阻塞)
        }
        """
        import httpx

        async def _query(rtype: str, qname: str) -> list[dict[str, Any]]:
            url = f"{doh_endpoint}?name={qname}&type={rtype}"
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, headers={"Accept": "application/dns-json"})
            if r.status_code != 200:
                raise RuntimeError(f"DoH status={r.status_code}")
            doc = r.json()
            return doc.get("Answer", []) or []

        result: dict[str, Any] = {
            "host": host,
            "a": [],
            "aaaa": [],
            "mx": [],
            "ns": [],
            "spf": [],
            "dmarc": [],
            "security_grade": "ok",
            "notes": [],
            "errors": {},
        }

        async def _safe(rtype: str, qname: str) -> list[dict[str, Any]]:
            try:
                return await _query(rtype, qname)
            except Exception as e:
                result["errors"][rtype] = str(e)[:200]
                return []

        # A
        for ans in await _safe("A", host):
            if ans.get("type") == 1:
                result["a"].append(ans.get("data", ""))
        # AAAA
        for ans in await _safe("AAAA", host):
            if ans.get("type") == 28:
                result["aaaa"].append(ans.get("data", ""))
        # MX
        for ans in await _safe("MX", host):
            if ans.get("type") == 15:
                data = ans.get("data", "")
                # format: "10 mail.example.com."
                parts = data.split(None, 1)
                if len(parts) == 2:
                    result["mx"].append({"priority": int(parts[0]), "exchange": parts[1].rstrip(".")})
        # NS
        for ans in await _safe("NS", host):
            if ans.get("type") == 2:
                result["ns"].append(ans.get("data", "").rstrip("."))
        # TXT — 提取 SPF
        for ans in await _safe("TXT", host):
            if ans.get("type") == 16:
                data = ans.get("data", "").strip('"')
                if data.lower().startswith("v=spf1"):
                    result["spf"].append(data)
        # DMARC
        for ans in await _safe("TXT", f"_dmarc.{host}"):
            if ans.get("type") == 16:
                data = ans.get("data", "").strip('"')
                if data.lower().startswith("v=dmarc1"):
                    result["dmarc"].append(data)

        # 解读
        if not result["mx"]:
            result["notes"].append("no MX — 域名不收邮件 (或不接受 SMTP)")
        if not result["spf"]:
            result["notes"].append("no SPF — 邮件伪造无任何 SPF 防线")
        else:
            # 检查 SPF 是否 -all (硬失败)
            spf0 = result["spf"][0]
            if "~all" in spf0:
                result["notes"].append("SPF ends with ~all (softfail) — 伪造邮件更易通过")
            elif " -all" not in spf0 and "-all" not in spf0:
                result["notes"].append("SPF 不含 -all — 末尾策略弱, 易被绕过")
        if not result["dmarc"]:
            result["notes"].append("no DMARC — 无报告/无拒绝策略")
        else:
            d0 = result["dmarc"][0].lower()
            if "p=none" in d0 or "p=monitor" in d0:
                result["notes"].append("DMARC p=none — 不拒绝不合规邮件 (监控模式)")
            elif "p=quarantine" in d0:
                result["notes"].append("DMARC p=quarantine — 隔离不合规邮件")
            elif "p=reject" in d0:
                result["notes"].append("DMARC p=reject — 完全拒绝不合规邮件 (最好)")

        # 安全分
        score = 0
        if result["spf"]:
            score += 1
        if result["dmarc"]:
            score += 1
            d0 = result["dmarc"][0].lower()
            if "p=reject" in d0:
                score += 1
        if result["mx"]:
            score += 1
        if score <= 1:
            result["security_grade"] = "missing"
        elif score == 2:
            result["security_grade"] = "weak"
        else:
            result["security_grade"] = "ok"
        return result

    # ── T44b: wayback_urls ──────────────────────────────────────────────

    async def wayback_urls(
        self,
        url: str,
        *,
        limit: int = 200,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """T44b: Wayback Machine 历史 URL 探测.

        查 web.archive.org/web/timemap/link/<url> — 返回该 URL 在历史上的所有快照的指向 URL.
        pen-tester 视角: 旧端点 / 旧 secret / 旧 API 常在历史快照里没清理.

        Returns {
          "url",
          "snapshot_count": int,
          "unique_targets": [url, ...],  # 去重
          "first_snapshot": str | None,  # 最早一条
          "last_snapshot": str | None,
          "samples": [url, ...] (前 10),
        }
        """
        from urllib.parse import quote as _q
        import httpx
        target = f"https://web.archive.org/web/timemap/link/{_q(url, safe='/:?=&')}"
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                r = await client.get(target, headers={"User-Agent": "semantic-browser-recon/1.0"})
            if r.status_code != 200:
                return {"url": url, "snapshot_count": 0, "unique_targets": [],
                        "first_snapshot": None, "last_snapshot": None, "samples": [],
                        "error": f"status={r.status_code}"}
            lines = r.text.splitlines()
            # timemap 格式: <timestamp> <original_url> <mimetype> "<target_url>"
            # 跳过 header (前 2 行)
            targets: list[str] = []
            for line in lines[2:]:
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    target_url = parts[3].strip('"').strip()
                    if target_url:
                        targets.append(target_url[:500])
            uniq = list(dict.fromkeys(targets))  # 保序去重
            uniq_limited = uniq[:limit]
            return {
                "url": url,
                "snapshot_count": len(targets),
                "unique_targets": uniq_limited,
                "unique_target_count": len(uniq),
                "first_snapshot": targets[0] if targets else None,
                "last_snapshot": targets[-1] if targets else None,
                "samples": uniq_limited[:10],
            }
        except Exception as e:
            return {"url": url, "snapshot_count": 0, "unique_targets": [],
                    "first_snapshot": None, "last_snapshot": None, "samples": [],
                    "error": str(e)[:200]}

    # ── T44c: find_xss_sinks ──────────────────────────────────────────────

    async def find_xss_sinks(
        self,
        *,
        max_scripts: int = 15,
        max_body: int = 100_000,
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        r"""T44c: 扫页面所有 <script src> 源码, 找 DOM XSS sinks.

        检测 sinks:
          - eval(                          (eval arbitrary string)
          - new Function(                  (function constructor)
          - innerHTML\s*=                  (HTML injection)
          - outerHTML\s*=
          - document.write(                (DOM write)
          - document.writeln(
          - setTimeout("...", )            (string form, not function)
          - setInterval("...", )           (string form)
          - .insertAdjacentHTML(
          - location\s*=                   (location override)
          - window.location\s*=
          - location.href\s*=
          - document.cookie                (sensitive read)
          - .src\s*=\s*location            (URL injection)

        Returns {
          "page_url", "scripts_scanned", "scripts_failed",
          "findings": [{sink, count, script, samples: [snippet, ...]}],
          "by_sink": {sink: total_count},
          "sink_count": int,
        }
        """
        import re as _re
        from urllib.parse import urljoin
        import httpx

        page = await self._ensure_page()
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        scripts = scripts[:max_scripts]
        page_url = page.url
        abs_urls = [urljoin(page_url, s) for s in scripts if s]

        SINK_PATTERNS = [
            ("eval",                   r"\beval\s*\("),
            ("function_constructor",   r"\bnew\s+Function\s*\("),
            ("innerHTML",              r"\.innerHTML\s*="),
            ("outerHTML",              r"\.outerHTML\s*="),
            ("document.write",         r"\bdocument\.write(?:ln)?\s*\("),
            ("setTimeout_string",      r"\bsetTimeout\s*\(\s*['\"]"),
            ("setInterval_string",     r"\bsetInterval\s*\(\s*['\"]"),
            ("insertAdjacentHTML",     r"\.insertAdjacentHTML\s*\("),
            ("location_assignment",    r"\b(?:window\.)?location(?:\.href)?\s*="),
            ("document.cookie_read",   r"\bdocument\.cookie\b"),
            ("src_from_location",      r"\.src\s*=\s*(?:window\.)?location"),
        ]

        findings: list[dict[str, Any]] = []
        scripts_scanned = 0
        scripts_failed = 0
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=True,
        ) as client:
            for url in abs_urls:
                try:
                    r = await client.get(url)
                    body = r.text[:max_body]
                    scripts_scanned += 1
                except Exception:
                    scripts_failed += 1
                    continue
                for name, pat in SINK_PATTERNS:
                    matches = list(_re.finditer(pat, body))
                    if not matches:
                        continue
                    samples = []
                    for m in matches[:3]:
                        start = max(0, m.start() - 30)
                        end = min(len(body), m.end() + 30)
                        samples.append(body[start:end].replace("\n", " ")[:120])
                    findings.append({
                        "sink": name,
                        "count": len(matches),
                        "script": url,
                        "samples": samples,
                    })

        by_sink: dict[str, int] = {}
        for f in findings:
            by_sink[f["sink"]] = by_sink.get(f["sink"], 0) + f["count"]
        return {
            "page_url": page_url,
            "scripts_scanned": scripts_scanned,
            "scripts_failed": scripts_failed,
            "findings": findings,
            "by_sink": by_sink,
            "sink_count": len(findings),
        }

    # ── T44d: detect_auth_methods ─────────────────────────────────────────────

    async def detect_auth_methods(self) -> dict[str, Any]:
        """T44d: 检测页面里出现的 auth/CAPTCHA/OAuth 组件.

        检测:
          - reCAPTCHA v2/v3 (grecaptcha.render / google.com/recaptcha)
          - hCaptcha
          - Cloudflare Turnstile
          - FunCaptcha / Arkose Labs
          - Google OAuth
          - GitHub OAuth
          - Facebook OAuth
          - Apple OAuth
          - Microsoft OAuth
          - Twitter/X OAuth
          - WebAuthn / Passkey
          - Magic link / passwordless (含 "magic link" 文字)
          - SAML (saml/acs/SingleSignOn)

        Returns {
          "page_url",
          "captcha": [name, ...],
          "oauth_providers": [name, ...],
          "mfa": [name, ...],   # 2FA / MFA 信号 (WebAuthn, TOTP, SMS, backup)
          "sso": [name, ...],   # SAML / OIDC generic
          "signals": [{kind, name, hint}],
        }
        """
        import re as _re
        page = await self._ensure_page()
        try:
            content = await page.content()
        except Exception:
            content = ""
        # 也看脚本 src (CDN 引用可能没在 inline HTML 里)
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            return out;
        }""")
        script_blob = " ".join(scripts)
        combined = (content + " " + script_blob)[:50000]

        captcha_sigs = [
            (r"grecaptcha\.render|google\.com/recaptcha|Recaptcha\.create", "reCAPTCHA v2/v3"),
            (r"hcaptcha\.com|hcaptcha\.render|h-captcha", "hCaptcha"),
            (r"challenges\.cloudflare\.com/turnstile|cf-turnstile", "Cloudflare Turnstile"),
            (r"funcaptcha|arkoselabs|arkose\.com", "FunCaptcha/Arkose"),
        ]
        oauth_sigs = [
            (r"Sign in with Google|accounts\.google\.com/o/oauth|gsi/client", "Google"),
            (r"Sign in with GitHub|github\.com/login/oauth", "GitHub"),
            (r"Sign in with Facebook|facebook\.com/v\d+\.\d+/dialog/oauth|fbcdn\.net", "Facebook"),
            (r"Sign in with Apple|appleid\.apple\.com|appleid\.sdk", "Apple"),
            (r"Sign in with Microsoft|login\.microsoftonline\.com|msal", "Microsoft"),
            (r"Sign in with Twitter|Sign in with X|twitter\.com/oauth|x\.com/oauth", "Twitter/X"),
            (r"Sign in with Discord|discord\.com/oauth2", "Discord"),
            (r"Sign in with LinkedIn|linkedin\.com/oauth", "LinkedIn"),
        ]
        mfa_sigs = [
            (r"webauthn|public[-_]key[-_]credential|navigator\.credentials", "WebAuthn/Passkey"),
            (r"totp|google[-_]authenticator|authy|1password|2fa|two[-_]factor|authenticator", "TOTP-based 2FA"),
            (r"sms[-_]code|verification[-_]code|2fa[-_]sms", "SMS 2FA"),
            (r"backup[-_]code|recovery[-_]code", "Backup codes"),
            (r"duo[-_]factor|duo\.com", "Duo 2FA"),
        ]
        sso_sigs = [
            (r"/saml/acs|/|saml2|SAMLResponse|SPEntityID|IdPEntityID", "SAML SSO"),
            (r"/oidc|/oauth2/authorize|openid-connect", "OIDC/OAuth2 generic"),
        ]

        captcha = [name for pat, name in captcha_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        oauth = [name for pat, name in oauth_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        mfa = [name for pat, name in mfa_sigs if _re.search(pat, combined, _re.IGNORECASE)]
        sso = [name for pat, name in sso_sigs if _re.search(pat, combined, _re.IGNORECASE)]

        signals: list[dict[str, str]] = []
        for n in captcha:
            signals.append({"kind": "captcha", "name": n, "hint": "Bot protection"})
        for n in oauth:
            signals.append({"kind": "oauth", "name": n, "hint": "OAuth provider"})
        for n in mfa:
            signals.append({"kind": "mfa", "name": n, "hint": "Multi-factor auth"})
        for n in sso:
            signals.append({"kind": "sso", "name": n, "hint": "SSO protocol"})
        return {
            "page_url": page.url,
            "captcha": captcha,
            "oauth_providers": oauth,
            "mfa": mfa,
            "sso": sso,
            "signals": signals,
        }

    # ── T44e: check_csrf_coverage ─────────────────────────────────────────────

    async def check_csrf_coverage(self) -> dict[str, Any]:
        """T44e: 对当前页每个 form 检查 CSRF token 是否存在.

        CSRF token 字段名: csrf_token, authenticity_token, _csrf, csrfmiddlewaretoken,
                           antiforgerytoken, __requestverificationtoken, _token, csrfToken
        只对会改变状态的 form (login/signup/checkout/contact/upload) 报警.

        Returns {
          "page_url",
          "form_count": int,
          "forms_with_csrf": int,
          "forms_without_csrf": int,
          "vulnerable": [{action, method, kind, field_names}],
        }
        """
        page = await self._ensure_page()
        snap = await SnapshotEngine(page).capture(base_url=page.url, detail_level="full")
        CSRF_NAMES = {
            "csrf_token", "authenticity_token", "_csrf", "csrfmiddlewaretoken",
            "antiforgerytoken", "__requestverificationtoken", "_token", "csrftoken",
            "csrfToken", "anti_csrf_token", "x-csrf-token", "csrf", "_csrf_token",
        }
        STATE_CHANGING = {"login", "signup", "checkout", "contact", "upload", "search", "unknown"}
        vulnerable: list[dict[str, Any]] = []
        for f in snap.forms:
            has_csrf = any(
                h.get("name", "").lower() in CSRF_NAMES
                for h in f.hidden_fields
            )
            if not has_csrf and f.classification in STATE_CHANGING:
                vulnerable.append({
                    "action": f.action[:200],
                    "method": f.method or "get",
                    "kind": f.classification,
                    "field_names": f.input_names[:10],
                })
        return {
            "page_url": page.url,
            "form_count": len(snap.forms),
            "forms_with_csrf": sum(
                1 for f in snap.forms
                if any(h.get("name", "").lower() in CSRF_NAMES for h in f.hidden_fields)
            ),
            "forms_without_csrf": len(vulnerable),
            "vulnerable": vulnerable,
        }

    # ── T44f: find_idor_urls ─────────────────────────────────────────────────

    async def find_idor_urls(self) -> dict[str, Any]:
        """T44f: 扫链接 + form action 找 IDOR-prone URLs.

        模式: /user/{N}, /users/{N}, /order/{N}, /orders/{N}, /invoice/{N},
              /account/{N}, /profile/{N}, /api/v1/users/{N}, /api/v1/orders/{N}, ...
        数字 ID (1-12 位) 视为可疑.
        Returns {
          "page_url",
          "idor_urls": [{href, kind, id}],
          "idor_count": int,
        }
        """
        import re as _re
        page = await self._ensure_page()
        snap = await SnapshotEngine(page).capture(base_url=page.url, detail_level="full")
        IDOR_RE = _re.compile(
            r"/(users?|orders?|invoices?|accounts?|profiles?|customers?|tickets?)"
            r"/(\d{1,12})(?:\b|/)",
            _re.IGNORECASE,
        )
        idor: list[dict[str, Any]] = []
        seen = set()
        for link in snap.links:
            for m in IDOR_RE.finditer(link.href or ""):
                key = (m.group(1).lower(), m.group(2), link.href[:200])
                if key in seen:
                    continue
                seen.add(key)
                idor.append({"href": link.href[:300], "kind": m.group(1).lower(), "id": m.group(2)})
        for f in snap.forms:
            for m in IDOR_RE.finditer(f.action or ""):
                key = (m.group(1).lower(), m.group(2), f.action[:200])
                if key in seen:
                    continue
                seen.add(key)
                idor.append({"href": f.action[:300], "kind": m.group(1).lower(), "id": m.group(2), "in_form": True})
        return {
            "page_url": page.url,
            "idor_urls": idor[:100],
            "idor_count": len(idor),
        }

    # ── T44g: find_cloud_resources ─────────────────────────────────────────

    async def find_cloud_resources(self) -> dict[str, Any]:
        """T44g: 扫 page source + script srcs, 找云资源 URL 泄露.

        检测:
          - AWS S3:            <bucket>.s3.amazonaws.com / s3-<region>.amazonaws.com/<bucket>
          - Azure Blob:        <account>.blob.core.windows.net
          - Azure Files:       <account>.file.core.windows.net
          - GCP Storage:       storage.googleapis.com/<bucket>
          - Heroku:            <app>.herokuapp.com
          - Firebase DB:       <app>.firebaseio.com
          - Firebase Hosting:  <app>.web.app / <app>.firebaseapp.com
          - CloudFront:        <id>.cloudfront.net
          - DigitalOcean:      <bucket>.nyc3.digitaloceanspaces.com

        Returns {
          "page_url",
          "resources": [{provider, url, kind}],
          "by_provider": {aws_s3: N, azure_blob: M, ...},
        }
        """
        import re as _re
        page = await self._ensure_page()
        try:
            content = await page.content()
        except Exception:
            content = ""
        scripts = await page.evaluate("""() => {
            const out = [];
            for (const s of document.querySelectorAll('script[src]')) {
                const src = s.getAttribute('src');
                if (src) out.push(src);
            }
            for (const l of document.querySelectorAll('link[href]')) {
                const h = l.getAttribute('href');
                if (h) out.push(h);
            }
            return out;
        }""")
        blob = content + "\n" + "\n".join(scripts)
        PATTERNS = [
            ("aws_s3",         r"https?://[a-z0-9.\-]+\.s3(?:\.[a-z0-9\-]+)?\.amazonaws\.com[^\s\"'<>]*", "S3 bucket"),
            ("aws_s3_path",    r"https?://s3(?:\.[a-z0-9\-]+)?\.amazonaws\.com/[a-z0-9.\-]+[^\s\"'<>]*", "S3 path-style"),
            ("azure_blob",     r"https?://[a-z0-9]+\.blob\.core\.windows\.net[^\s\"'<>]*", "Azure Blob"),
            ("azure_file",     r"https?://[a-z0-9]+\.file\.core\.windows\.net[^\s\"'<>]*", "Azure Files"),
            ("gcp_storage",    r"https?://storage\.googleapis\.com/[a-z0-9.\-]+[^\s\"'<>]*", "GCP Storage"),
            ("heroku_app",     r"https?://[a-z0-9\-]+\.herokuapp\.com[^\s\"'<>]*", "Heroku app"),
            ("firebase_db",    r"https?://[a-z0-9\-]+\.firebaseio\.com[^\s\"'<>]*", "Firebase DB"),
            ("firebase_host",  r"https?://[a-z0-9\-]+\.(?:web\.app|firebaseapp\.com)[^\s\"'<>]*", "Firebase Hosting"),
            ("cloudfront",     r"https?://[a-z0-9]+\.cloudfront\.net[^\s\"'<>]*", "CloudFront"),
            ("do_spaces",      r"https?://[a-z0-9\-]+\.[a-z0-9]+\.digitaloceanspaces\.com[^\s\"'<>]*", "DigitalOcean Spaces"),
        ]
        resources: list[dict[str, str]] = []
        seen = set()
        for prov, pat, kind in PATTERNS:
            for m in _re.finditer(pat, blob, _re.IGNORECASE):
                url = m.group(0).rstrip(".,);\"'")
                if url in seen:
                    continue
                seen.add(url)
                resources.append({"provider": prov, "url": url[:300], "kind": kind})
        by_provider: dict[str, int] = {}
        for r in resources:
            by_provider[r["provider"]] = by_provider.get(r["provider"], 0) + 1
        return {
            "page_url": page.url,
            "resources": resources[:200],
            "by_provider": by_provider,
            "resource_count": len(resources),
        }

    # ── T44h: probe_http_methods ────────────────────────────────────────────

    async def probe_http_methods(
        self,
        base_url: str | None = None,
        *,
        paths: list[str] | None = None,
        timeout_ms: int = 4000,
    ) -> dict[str, Any]:
        """T44h: OPTIONS 请求探测每个 path 的 Allow header, 看是否支持危险方法.

        Returns {
          "base_url",
          "results": [
            {"path", "allow" (parsed), "dangerous" (bool, 含 PUT/DELETE/PATCH/CONNECT/TRACE)},
            ...
          ],
        }
        """
        import httpx
        page = await self._ensure_page()
        if not base_url:
            base_url = page.url
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port and not (
            (parsed.scheme == "http" and parsed.port == 80)
            or (parsed.scheme == "https" and parsed.port == 443)
        ):
            origin += f":{parsed.port}"
        if not paths:
            paths = ["/", "/api", "/api/v1", "/users", "/admin", "/login"]
        DANGEROUS = {"PUT", "DELETE", "PATCH", "CONNECT", "TRACE"}
        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            headers={"User-Agent": "semantic-browser-probe/1.0"},
            follow_redirects=False,
        ) as client:
            for path in paths:
                url = origin + path
                try:
                    r = await client.request("OPTIONS", url)
                except Exception as e:
                    results.append({"path": path, "allow": [], "dangerous": False, "error": str(e)[:200]})
                    continue
                allow = r.headers.get("allow") or r.headers.get("Allow") or ""
                methods = [m.strip().upper() for m in allow.split(",") if m.strip()]
                dangerous = any(m in DANGEROUS for m in methods)
                results.append({
                    "path": path,
                    "status": r.status_code,
                    "allow": methods,
                    "dangerous": dangerous,
                })
        return {
            "base_url": base_url,
            "results": results,
        }

    # ── T44i: detect_2fa ────────────────────────────────

    async def detect_2fa(self) -> dict[str, Any]:
        """T44i: 专门检测 2FA / MFA 信号 (WebAuthn / TOTP / SMS / backup code / Duo)."""
        r = await self.detect_auth_methods()
        return {
            "page_url": r["page_url"],
            "mfa": r["mfa"],
            "mfa_count": len(r["mfa"]),
            "has_webauthn": "WebAuthn/Passkey" in r["mfa"],
            "has_totp": any("TOTP" in m for m in r["mfa"]),
            "has_sms": any("SMS" in m for m in r["mfa"]),
            "has_backup_code": any("Backup" in m for m in r["mfa"]),
        }

    # ── T44j: inventory_external_resources ─────────────────────────────

    async def inventory_external_resources(self) -> dict[str, Any]:
        """T44j: 当前页所有外部资源分组 (供 trust boundary 分析).

        分组维度:
          - 外部 link domain: <a href> 指向外站的 host
          - 外部 script host: <script src> 外站 host
          - 外部 iframe: <iframe src> 外站 host
          - 跨域 form action: <form action> 外站 host
          - 跨域 redirect: 链接中含其他 host 的 redirect target

        Returns {
          "page_url",
          "external_link_domains": [{domain, count}],
          "external_script_hosts": [{host, urls}],
          "external_iframes": [{host, src}],
          "cross_origin_forms": [{host, action}],
        }
        """
        from urllib.parse import urlparse, urljoin
        from collections import Counter
        page = await self._ensure_page()
        page_url = page.url
        page_host = urlparse(page_url).hostname
        snap = await SnapshotEngine(page).capture(base_url=page_url, detail_level="full")
        # 1) 外部 link 域名
        link_domains: Counter[str] = Counter()
        for link in snap.links:
            href = link.href or ""
            if not href.startswith("http"):
                continue
            h = urlparse(href).hostname
            if h and h != page_host:
                link_domains[h] += 1
        # 2) 外部 script hosts
        script_hosts: dict[str, list[str]] = {}
        for s in snap.scripts:
            if not (s.has_src and s.src):
                continue
            try:
                u = urlparse(s.src)
                if u.hostname and u.hostname != page_host:
                    script_hosts.setdefault(u.hostname, []).append(s.src[:300])
            except Exception:
                pass
        # 3) iframe: snapshot 里没有直接拿, 走 page.evaluate
        iframes = await page.evaluate("""() => {
            const out = [];
            for (const f of document.querySelectorAll('iframe[src]')) {
                out.push(f.getAttribute('src'));
            }
            return out;
        }""")
        external_iframes: list[dict[str, str]] = []
        for src in iframes:
            try:
                full = urljoin(page_url, src)
                h = urlparse(full).hostname
                if h and h != page_host:
                    external_iframes.append({"host": h, "src": full[:300]})
            except Exception:
                pass
        # 4) 跨域 form action
        cross_origin_forms: list[dict[str, str]] = []
        for f in snap.forms:
            try:
                full = urljoin(page_url, f.action or "")
                h = urlparse(full).hostname
                if h and h != page_host:
                    cross_origin_forms.append({"host": h, "action": full[:300]})
            except Exception:
                pass
        return {
            "page_url": page_url,
            "external_link_domains": [
                {"domain": d, "count": c} for d, c in link_domains.most_common(50)
            ],
            "external_script_hosts": [
                {"host": h, "urls": urls[:5]} for h, urls in list(script_hosts.items())[:30]
            ],
            "external_iframes": external_iframes[:30],
            "cross_origin_forms": cross_origin_forms[:30],
        }

    # ── T44l: check_subdomain_takeover ───────────────────────────────

    async def check_subdomain_takeover(
        self,
        host: str | None = None,
        subdomains: list[str] | None = None,
        *,
        doh_endpoint: str = "https://dns.google/resolve",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """T44l: 对每个子域查 CNAME, 跟已知"易被接管"服务签名比对.

        签名表 (fingerprint → risk):
          - s3.amazonaws.com / s3-website → "S3 bucket (check ownership)"
          - .herokuapp.com               → "Heroku app (check deletion)"
          - .azurewebsites.net           → "Azure Web App (check deletion)"
          - .cloudfront.net              → "CloudFront (check distribution)"
          - .elasticbeanstalk.com        → "Elastic Beanstalk"
          - .github.io                   → "GitHub Pages (check repo)"
          - .pantheonsite.io             → "Pantheon"
          - .tumblr.com                  → "Tumblr (custom domain)"
          - .wordpress.com               → "WordPress.com"
          - .shopify.com                 → "Shopify (check claim)"

        Returns {
          "host",
          "checked": int,
          "risky": [{subdomain, cname, provider, risk, http_status (if any)}],
        }
        """
        import re as _re
        import httpx
        if not host:
            try:
                page = await self._ensure_page()
                from urllib.parse import urlparse
                host = urlparse(page.url).hostname
            except Exception:
                pass
        if not subdomains:
            # 默认查常见子域
            subdomains = [f"{prefix}.{host}" for prefix in (
                "www", "api", "staging", "dev", "test", "beta", "admin",
                "blog", "shop", "mail", "cdn", "static", "app",
            )]
        SIGS = [
            (r"\.s3(?:\-[a-z0-9\-]+)?\.amazonaws\.com",    "AWS S3",      "check if bucket exists / is yours"),
            (r"\.s3-website(?:\-[a-z0-9\-]+)?\.amazonaws\.com", "AWS S3 website", "check bucket ownership"),
            (r"\.herokuapp\.com",                          "Heroku",      "check if app is deleted"),
            (r"\.azurewebsites\.net",                      "Azure Web App", "check if app is deleted"),
            (r"\.cloudfront\.net",                         "CloudFront",  "check distribution ownership"),
            (r"\.elasticbeanstalk\.com",                   "Elastic Beanstalk", "check environment"),
            (r"\.github\.io",                               "GitHub Pages", "check repo exists"),
            (r"\.pantheonsite\.io",                        "Pantheon",    "check site status"),
            (r"\.tumblr\.com$",                            "Tumblr",      "check blog claim"),
            (r"\.wordpress\.com$",                         "WordPress.com", "check site claim"),
            (r"\.shopify\.com$",                           "Shopify",     "check store claim"),
        ]
        risky: list[dict[str, Any]] = []
        checked = 0

        async def _get_cname(sub: str) -> str | None:
            """Try CNAME via DoH, fallback to socket.gethostbyname (A record)."""
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.get(f"{doh_endpoint}?name={sub}&type=CNAME",
                                         headers={"Accept": "application/dns-json"})
                if r.status_code == 200:
                    for ans in r.json().get("Answer", []):
                        if ans.get("type") == 5:
                            return ans.get("data", "").rstrip(".")
            except Exception:
                pass
            return None

        async def _http_status(sub: str) -> int | None:
            try:
                async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
                    for scheme in ("https", "http"):
                        try:
                            r = await client.get(f"{scheme}://{sub}/", headers={"User-Agent": "semantic-browser-recon/1.0"})
                            return r.status_code
                        except Exception:
                            continue
            except Exception:
                pass
            return None

        for sub in subdomains:
            checked += 1
            cname = await _get_cname(sub)
            target = cname or sub
            matched_provider: str | None = None
            matched_risk: str | None = None
            for pat, provider, risk in SIGS:
                if _re.search(pat, target, _re.IGNORECASE):
                    matched_provider = provider
                    matched_risk = risk
                    break
            if matched_provider:
                # 拿 HTTP 状态辅助判断
                status = await _http_status(sub)
                # 404 / 503 / NXDOMAIN-like 强烈提示可接管
                suspicious_status = status in (404, 503) or status is None
                risky.append({
                    "subdomain": sub,
                    "cname": cname,
                    "provider": matched_provider,
                    "risk": matched_risk,
                    "http_status": status,
                    "suspicious_status": suspicious_status,
                })
        return {
            "host": host,
            "checked": checked,
            "risky": risky,
        }

    # ── T47: a11y_audit ─────────────────────────────────────────────────

    async def a11y_audit(
        self,
        max_nodes_per_violation: int = 5,
        standards: list[str] | None = None,
    ) -> dict[str, Any]:
        """T47: 注入 axe-core 跑 WCAG 审计, 返回结构化 violations.

        Args:
          max_nodes_per_violation: 每个 violation 最多保留几个 node, 默认 5
                                   (axe 可能返回几百个相同 rule 的 element).
          standards: WCAG 标准 tag 列表, 默认 ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"].

        Returns: {
          "url", "axe_version",
          "summary": {violations, passes, incomplete, inapplicable, by_impact},
          "violations": [{id, impact, description, help, help_url, tags,
                          node_count, nodes: [{html, target, failure_summary}]}],
          "error": str  # 仅在 axe 注入 / 跑失败时
        }
        """
        page = await self._ensure_page()
        if standards is None:
            standards = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]

        # 找 vendored axe.min.js (兼容 editable install)
        from pathlib import Path
        try:
            from importlib.resources import files
            axe_path = str(files("semantic_browser.assets").joinpath("axe.min.js"))
        except Exception:
            axe_path = str(Path(__file__).resolve().parent.parent / "assets" / "axe.min.js")

        try:
            await page.add_script_tag(path=axe_path)
        except Exception as e:
            return {
                "url": page.url,
                "axe_version": None,
                "summary": {"violations": 0, "passes": 0, "incomplete": 0,
                            "inapplicable": 0, "by_impact": {}},
                "violations": [],
                "error": f"failed to inject axe-core: {e}",
            }

        try:
            raw = await page.evaluate(
                """async ({standards, maxNodes}) => {
                    const opts = {
                        runOnly: { type: 'tag', values: standards },
                        resultTypes: ['violations', 'passes', 'incomplete', 'inapplicable'],
                    };
                    const r = await axe.run(document, opts);
                    return {
                        version: axe.version,
                        violations: r.violations.map(v => ({
                            id: v.id,
                            impact: v.impact,
                            description: v.description,
                            help: v.help,
                            helpUrl: v.helpUrl,
                            tags: v.tags,
                            nodes: v.nodes.slice(0, maxNodes).map(n => ({
                                html: n.html.slice(0, 500),
                                target: n.target,
                                failureSummary: n.failureSummary,
                            })),
                            helpUrl: v.helpUrl,
                            _total_nodes: v.nodes.length,
                        })),
                        passes_count: r.passes.length,
                        incomplete_count: r.incomplete.length,
                        inapplicable_count: r.inapplicable.length,
                    };
                }""",
                {"standards": standards, "maxNodes": max_nodes_per_violation},
            )
        except Exception as e:
            return {
                "url": page.url,
                "axe_version": None,
                "summary": {"violations": 0, "passes": 0, "incomplete": 0,
                            "inapplicable": 0, "by_impact": {}},
                "violations": [],
                "error": f"axe.run failed: {e}",
            }

        violations = raw.get("violations", [])
        # axe 用 camelCase (helpUrl), 项目统一 snake_case
        for v in violations:
            if "helpUrl" in v:
                v["help_url"] = v.pop("helpUrl")
            if "failureSummary" in v.get("nodes", [{}])[0] if v.get("nodes") else False:
                for n in v["nodes"]:
                    if "failureSummary" in n:
                        n["failure_summary"] = n.pop("failureSummary")

        by_impact: dict[str, int] = {}
        for v in violations:
            imp = v.get("impact") or "minor"
            by_impact[imp] = by_impact.get(imp, 0) + 1

        # 把 _total_nodes 提出来, 不污染返回
        for v in violations:
            v["node_count"] = v.pop("_total_nodes", len(v["nodes"]))

        return {
            "url": page.url,
            "axe_version": raw.get("version"),
            "summary": {
                "violations": len(violations),
                "passes": raw.get("passes_count", 0),
                "incomplete": raw.get("incomplete_count", 0),
                "inapplicable": raw.get("inapplicable_count", 0),
                "by_impact": by_impact,
            },
            "violations": violations,
        }
