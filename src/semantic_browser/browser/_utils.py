"""Browser utility helpers — CORS risk assessment, version compare, TLS SAN, server hints."""

from __future__ import annotations

import re as _re
import socket as _socket
import ssl as _ssl


def _assess_cors_risk(allow_origin: str | None, allow_credentials: bool) -> str:
    """CORS misconfig risk grading — pen-tester's first look.

    high:   ACAO=* + credentials=true — browser will reject, but backend config
            is confused / potentially bypassable
    medium: ACAO=* (no credentials) — any origin can read (sensitivity-dependent)
    low:    ACAO is a specific origin (e.g. https://app.example.com) — normal
    none:   no ACAO header — browser same-origin protection
    """
    if not allow_origin:
        return "none"
    if allow_origin == "*":
        return "high" if allow_credentials else "medium"
    if allow_origin == "null":
        return "high"  # null origin + sandboxed file / data: URI is an attack vector
    return "low"


def _redact_url_secrets(url: str) -> str:
    """T117 audit fix: 把 URL query string 里的 ?token= / ?api_key= / ?session=
    / ?signature= 等敏感字段 mask 成 <redacted>, 路径部分保留. 多个站 URL
    在 INFO log 里被记录, 之前会泄 OAuth callback 的 access_token / 密码
    """
    if not url or "?" not in url:
        return url
    _SENSITIVE = (
        "token", "api_key", "apikey", "api-key", "key",
        "password", "secret", "signature", "sig",
        "access_token", "auth", "session", "sessionid",
        "code", "state",  # OAuth
    )
    # 拆 path 和 query
    if "#" in url:
        url_main, frag = url.split("#", 1)
    else:
        url_main, frag = url, ""
    if "?" not in url_main:
        return url
    base, query = url_main.split("?", 1)
    safe_pairs = []
    for pair in query.split("&"):
        if not pair:
            safe_pairs.append(pair)
            continue
        if "=" in pair:
            k, _ = pair.split("=", 1)
            key_lower = k.lower()
            if any(s == key_lower or key_lower.endswith("_" + s) for s in _SENSITIVE):
                safe_pairs.append(f"{k}=<redacted>")
                continue
        safe_pairs.append(pair)
    out = base + "?" + "&".join(safe_pairs)
    if frag:
        out += "#" + frag
    return out


def _version_lt(a: str, b: str) -> bool:
    """简单 semver-like 比较: a < b ? True."""
    try:
        ap = tuple(int(x) for x in a.split("."))
        bp = tuple(int(x) for x in b.split("."))
        while len(ap) < len(bp):
            ap = ap + (0,)
        while len(bp) < len(ap):
            bp = bp + (0,)
        return ap < bp
    except Exception:
        return False


def _tls_subdomains(host: str, port: int = 443, timeout: float = 5.0) -> list[str]:
    """连 host:port 取证书, 解析 SAN 列表, 过滤出 host 的子域.

    返回 lowercased 去重子域列表 (含 host 本身如果出现在 SAN 里).
    """
    import re as _re2
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        with _socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert(binary_form=False) or {}
                der = ss.getpeercert(binary_form=True) or b""
        sans: list[str] = []
        for entry in cert.get("subjectAltName", []):
            if entry and entry[0].lower() == "dns":
                sans.append(entry[1].lower())
        if not sans and der:
            text = der.decode("latin-1", errors="ignore")
            for m in _re2.finditer(r"DNS:([A-Za-z0-9._-]+)", text):
                sans.append(m.group(1).lower())
        # 过滤 host 的子域
        return sorted({s for s in sans if s == host or s.endswith("." + host)})
    except Exception:
        return []


_SERVER_HINTS = (
    (r"(?i)\bnginx\b",        "nginx"),
    (r"(?i)\bapache\b",       "Apache"),
    (r"(?i)\biis\b",          "IIS (Microsoft)"),
    (r"(?i)\benvoy\b",        "Envoy (often behind K8s)"),
    (r"(?i)\btraefik\b",      "Traefik"),
    (r"(?i)\bhaproxy\b",      "HAProxy"),
    (r"(?i)\bcaddy\b",        "Caddy"),
    (r"(?i)\bcloudfront\b",   "AWS CloudFront"),
    (r"(?i)\bgfe\b",          "Google Frontend (GFE)"),
    (r"(?i)\blite-?speed\b",  "LiteSpeed"),
    (r"(?i)\bgunicorn\b",     "gunicorn (Python)"),
    (r"(?i)\buwsgi\b",        "uWSGI (Python)"),
    (r"(?i)\bjetty\b",        "Jetty (Java)"),
    (r"(?i)\btomcat\b",       "Tomcat (Java)"),
    (r"(?i)\bopenresty\b",    "OpenResty (Lua/nginx)"),
    (r"(?i)\bvercel\b",       "Vercel"),
    (r"(?i)\bnetlify\b",      "Netlify"),
)


def _server_hint(server_header: str) -> str:
    """从 Server header 推断 web server. 失败 → ''."""
    if not server_header:
        return ""
    for pat, name in _SERVER_HINTS:
        if _re.search(pat, server_header):
            return name
    return server_header[:60]


_GENERATOR_HINTS = (
    (r"(?i)wordpress\s*([\d.]+)?",  "WordPress"),
    (r"(?i)drupal\s*([\d.]+)?",     "Drupal"),
    (r"(?i)joomla\s*([\d.]+)?",     "Joomla"),
    (r"(?i)ghost\s*([\d.]+)?",      "Ghost"),
    (r"(?i)hugo\s*([\d.]+)?",       "Hugo (static)"),
    (r"(?i)jekyll\s*([\d.]+)?",     "Jekyll (static)"),
    (r"(?i)eleventy\s*([\d.]+)?",   "Eleventy (static)"),
    (r"(?i)next\.?js",              "Next.js"),
    (r"(?i)nuxt",                   "Nuxt.js"),
    (r"(?i)gatsby",                 "Gatsby"),
    (r"(?i)hexo",                   "Hexo"),
    (r"(?i)typecho",                "Typecho"),
    (r"(?i)mediawiki",              "MediaWiki"),
    (r"(?i)discuz",                 "Discuz!"),
)


def _generator_hint(generator: str) -> str:
    """从 <meta name='generator'> 内容推断 CMS / 框架."""
    if not generator:
        return ""
    for pat, name in _GENERATOR_HINTS:
        m = _re.search(pat, generator)
        if m:
            ver = m.group(1) if m.lastindex and m.group(1) else ""
            return f"{name} {ver}".strip()
    return generator[:60]
