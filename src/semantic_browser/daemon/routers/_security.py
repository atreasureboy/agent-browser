"""Security audit tool route handlers — extracted from server.py _dispatch.

T40–T44 + T47: storage, security headers, path probing, API discovery,
JS analysis, secret scanning, WAF/tech fingerprinting, DNS/subdomain/SSL,
wayback, XSS/CSRF/IDOR/auth/2FA/cloud/external-resource/CSP audits,
a11y audit.
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


# ── T40a: 客户端存储探针 (local/session + cookies) ──────────────

def handle_get_storage(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /storage — probe client-side storage (localStorage, sessionStorage, cookies)."""
    return daemon.owner.run(daemon.owner.browser.controller.get_storage())


_register("GET", "/storage", handle_get_storage)


# ── T40f: 安全头结构化 ──────────────────────────────────────────

def handle_security_headers(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /security-headers — fetch and parse security-relevant HTTP headers."""
    url: str = str(args.get("url", ""))
    if not url:
        raise ValueError("url required")
    # T111 audit fix: get_security_headers 也走 httpx fetch — 需 SSRF.
    daemon._check_url(url, where="security_headers")
    return daemon.owner.run(daemon.owner.browser.controller.get_security_headers(url))


_register("GET", "/security-headers", handle_security_headers)


# ── T40b: Hidden paths probe (httpx 探测常见路径) ────────────────

def handle_probe_paths(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /probe-paths — probe common hidden paths via httpx."""
    url: str = str(args.get("url", ""))
    if not url:
        raise ValueError("url required")
    # T111 audit fix: probe_paths 内部 httpx 发请求, SSRF.
    daemon._check_url(url, where="probe_paths")
    cats_raw: str = str(args.get("categories", ""))
    categories: list[str] | None = (
        [c for c in cats_raw.split(",") if c] if cats_raw else None
    )
    return daemon.owner.run(
        daemon.owner.browser.controller.probe_paths(url, categories=categories)
    )


_register("GET", "/probe-paths", handle_probe_paths)


# ── T40g: 从页面 JS 提取 API endpoints ──────────────────────────

def handle_extract_api_endpoints(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /extract-api-endpoints — extract API endpoints from page JavaScript."""
    return daemon.owner.run(daemon.owner.browser.controller.extract_api_endpoints())


_register("GET", "/extract-api-endpoints", handle_extract_api_endpoints)


# ── T42b: JS 库版本 + CVE 识别 ──────────────────────────────────

def handle_extract_js_libraries(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /extract-js-libraries — identify JS library versions and known CVEs."""
    return daemon.owner.run(daemon.owner.browser.controller.extract_js_libraries())


_register("GET", "/extract-js-libraries", handle_extract_js_libraries)


# ── T42g: GraphQL introspection ──────────────────────────────────

def handle_detect_graphql(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /detect-graphql — run GraphQL introspection query against an endpoint."""
    endpoint: str = str(args.get("endpoint", ""))
    if not endpoint:
        raise ValueError("endpoint required")
    # T111 audit fix: detect_graphql 也发 introspection POST.
    daemon._check_url(endpoint, where="detect_graphql")
    return daemon.owner.run(daemon.owner.browser.controller.detect_graphql(endpoint))


_register("GET", "/detect-graphql", handle_detect_graphql)


# ── T43a: 子域名枚举 ────────────────────────────────────────────

def handle_enumerate_subdomains(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /enumerate-subdomains — enumerate subdomains via DoH + TLS SAN."""
    host: str = args["host"]
    # T111 audit fix: enumerate_subdomains 用 DoH + TLS SAN, 需 SSRF.
    daemon._check_url(f"https://{host}", where="enumerate_subdomains")
    return daemon.owner.run(
        daemon.owner.browser.controller.enumerate_subdomains(
            host=host,
            include_tls_san=str(args.get("include_tls_san", "true")).lower() != "false",
        )
    )


_register("GET", "/enumerate-subdomains", handle_enumerate_subdomains)


# ── T43b: JS secret 扫描 ────────────────────────────────────────

def handle_extract_secrets_from_js(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /extract-secrets-from-js — scan page JavaScript for secrets/keys/tokens."""
    return daemon.owner.run(daemon.owner.browser.controller.extract_secrets_from_js())


_register("GET", "/extract-secrets-from-js", handle_extract_secrets_from_js)


# ── T43c: WAF 指纹 ──────────────────────────────────────────────

def handle_detect_waf(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /detect-waf — fingerprint Web Application Firewall in use."""
    return daemon.owner.run(daemon.owner.browser.controller.detect_waf())


_register("GET", "/detect-waf", handle_detect_waf)


# ── T43d: 开放重定向 sink ───────────────────────────────────────

def handle_find_open_redirect_sinks(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /find-open-redirect-sinks — find open-redirect sinks in page DOM/JS."""
    return daemon.owner.run(daemon.owner.browser.controller.find_open_redirect_sinks())


_register("GET", "/find-open-redirect-sinks", handle_find_open_redirect_sinks)


# ── T43e: 敏感信息泄露 ──────────────────────────────────────────

def handle_find_disclosure(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /find-disclosure — detect sensitive data disclosure in page content."""
    return daemon.owner.run(daemon.owner.browser.controller.find_disclosure())


_register("GET", "/find-disclosure", handle_find_disclosure)


# ── T43f: 备份/源码/配置文件 ────────────────────────────────────

def handle_analyze_exposed_files(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /analyze-exposed-files — probe for backup/source/config files."""
    base_url = args.get("base_url") or None
    if base_url:
        # T111 audit fix: 内部 httpx 探测 paths, SSRF.
        daemon._check_url(base_url, where="analyze_exposed_files")
    return daemon.owner.run(
        daemon.owner.browser.controller.analyze_exposed_files(base_url=base_url)
    )


_register("GET", "/analyze-exposed-files", handle_analyze_exposed_files)


# ── T43g: OpenAPI/Swagger 发现 ──────────────────────────────────

def handle_discover_api_specs(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /discover-api-specs — discover OpenAPI/Swagger specs at common locations."""
    base_url = args.get("base_url") or None
    if base_url:
        # T111 audit fix: 同上.
        daemon._check_url(base_url, where="discover_api_specs")
    return daemon.owner.run(
        daemon.owner.browser.controller.discover_api_specs(base_url=base_url)
    )


_register("GET", "/discover-api-specs", handle_discover_api_specs)


# ── T43h: TLS 证书 SAN ──────────────────────────────────────────

def handle_tls_subdomains(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /tls-subdomains — extract subdomain names from TLS certificate SAN."""
    host: str = args["host"]
    # T111 audit fix: 直接发起 TLS 连接 — 需 SSRF 闸.
    daemon._check_url(f"https://{host}", where="tls_subdomains")
    return daemon.owner.run(
        daemon.owner.browser.controller.tls_subdomains(
            host=host, port=int(args.get("port", 443))
        )
    )


_register("GET", "/tls-subdomains", handle_tls_subdomains)


# ── T43i: 技术栈指纹 ────────────────────────────────────────────

def handle_fingerprint_tech(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /fingerprint-tech — fingerprint technology stack of current page."""
    return daemon.owner.run(daemon.owner.browser.controller.fingerprint_tech())


_register("GET", "/fingerprint-tech", handle_fingerprint_tech)


# ── T43j: JWT 解码 ──────────────────────────────────────────────

def handle_decode_jwts(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /decode-jwts — decode JWTs found in page storage/network."""
    return daemon.owner.run(daemon.owner.browser.controller.decode_jwts())


_register("GET", "/decode-jwts", handle_decode_jwts)


# ── T44a: DNS 记录 ──────────────────────────────────────────────

def handle_dns_records(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /dns-records — query DNS records for the given host (via DoH)."""
    host: str = args["host"]
    # T111 audit fix: DoH resolver 也算 SSRF 入口 (泄露内部 DNS).
    daemon._check_url(f"https://{host}", where="dns_records")
    return daemon.owner.run(daemon.owner.browser.controller.dns_records(host=host))


_register("GET", "/dns-records", handle_dns_records)


# ── T44b: Wayback Machine ────────────────────────────────────────

def handle_wayback_urls(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /wayback-urls — retrieve historical URLs from Wayback Machine."""
    url: str = args["url"]
    # T111 audit fix: wayback_urls 自身通过 archive.org 但 url 要过 SSRF 闸.
    daemon._check_url(url, where="wayback_urls")
    return daemon.owner.run(
        daemon.owner.browser.controller.wayback_urls(
            url=url, limit=int(args.get("limit", 200))
        )
    )


_register("GET", "/wayback-urls", handle_wayback_urls)


# ── T44c: DOM XSS sinks ─────────────────────────────────────────

def handle_find_xss_sinks(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /find-xss-sinks — find DOM XSS sinks in page JavaScript."""
    return daemon.owner.run(daemon.owner.browser.controller.find_xss_sinks())


_register("GET", "/find-xss-sinks", handle_find_xss_sinks)


# ── T44d: auth methods ──────────────────────────────────────────

def handle_detect_auth_methods(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /detect-auth-methods — detect authentication methods from page/headers."""
    return daemon.owner.run(daemon.owner.browser.controller.detect_auth_methods())


_register("GET", "/detect-auth-methods", handle_detect_auth_methods)


# ── T44e: CSRF coverage ─────────────────────────────────────────

def handle_check_csrf_coverage(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /check-csrf-coverage — audit CSRF protection coverage on forms/requests."""
    return daemon.owner.run(daemon.owner.browser.controller.check_csrf_coverage())


_register("GET", "/check-csrf-coverage", handle_check_csrf_coverage)


# ── T44f: IDOR URLs ─────────────────────────────────────────────

def handle_find_idor_urls(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /find-idor-urls — find URLs with patterns suggestive of IDOR."""
    return daemon.owner.run(daemon.owner.browser.controller.find_idor_urls())


_register("GET", "/find-idor-urls", handle_find_idor_urls)


# ── T44g: cloud resources ───────────────────────────────────────

def handle_find_cloud_resources(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /find-cloud-resources — find references to cloud resources (S3, GCS, etc.)."""
    return daemon.owner.run(daemon.owner.browser.controller.find_cloud_resources())


_register("GET", "/find-cloud-resources", handle_find_cloud_resources)


# ── T44h: HTTP methods ──────────────────────────────────────────

def handle_probe_http_methods(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /probe-http-methods — probe which HTTP methods a target accepts."""
    base_url = args.get("base_url") or None
    if base_url:
        # T111 audit fix: 内部 httpx send 不同 method, SSRF.
        daemon._check_url(base_url, where="probe_http_methods")
    paths = args.get("paths")
    return daemon.owner.run(
        daemon.owner.browser.controller.probe_http_methods(
            base_url=base_url,
            paths=paths if isinstance(paths, list) else None,
        )
    )


_register("GET", "/probe-http-methods", handle_probe_http_methods)


# ── T44i: 2FA ──────────────────────────────────────────────────

def handle_detect_2fa(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /detect-2fa — detect two-factor authentication indicators."""
    return daemon.owner.run(daemon.owner.browser.controller.detect_2fa())


_register("GET", "/detect-2fa", handle_detect_2fa)


# ── T44j: external resources ────────────────────────────────────

def handle_inventory_external_resources(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /inventory-external-resources — inventory external resources loaded by page."""
    return daemon.owner.run(daemon.owner.browser.controller.inventory_external_resources())


_register("GET", "/inventory-external-resources", handle_inventory_external_resources)


# ── T44k: CSP parse ─────────────────────────────────────────────

def handle_parse_csp(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /parse-csp — parse and audit Content-Security-Policy header."""
    return daemon.owner.run(daemon.owner.browser.controller.parse_csp())


_register("GET", "/parse-csp", handle_parse_csp)


# ── T44l: subdomain takeover (two dispatch paths merged) ─────────

def handle_check_subdomain_takeover(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /check-subdomain-takeover — check for subdomain takeover vulnerabilities.

    Two forms:
      - With ``host`` param: SSRF-check the host, enumerate its subdomains first.
      - Without ``host`` param: use provided ``subdomains`` list directly.
    """
    if "host" in args:
        host: str = args["host"]
        # T111 audit fix: 同 dns_records.
        daemon._check_url(f"https://{host}", where="check_subdomain_takeover")
        subs = args.get("subdomains")
        return daemon.owner.run(
            daemon.owner.browser.controller.check_subdomain_takeover(
                host=host,
                subdomains=subs if isinstance(subs, list) else None,
            )
        )
    # Without host — direct subdomain list
    subs = args.get("subdomains")
    return daemon.owner.run(
        daemon.owner.browser.controller.check_subdomain_takeover(
            subdomains=subs if isinstance(subs, list) else None,
        )
    )


_register("GET", "/check-subdomain-takeover", handle_check_subdomain_takeover)


# ── T47: a11y audit (axe-core) ──────────────────────────────────

def handle_a11y_audit(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /a11y-audit — run axe-core accessibility audit on current page."""
    max_nodes: int = int(args.get("max_nodes_per_violation", 5))
    standards = args.get("standards")
    if not isinstance(standards, list):
        standards = None
    return daemon.owner.run(
        daemon.owner.browser.controller.a11y_audit(
            max_nodes_per_violation=max_nodes,
            standards=standards,
        )
    )


_register("GET", "/a11y-audit", handle_a11y_audit)
