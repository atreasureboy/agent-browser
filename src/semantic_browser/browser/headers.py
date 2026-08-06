"""Header parsing and security headers analysis — CSP/HSTS/permissions-policy."""

from __future__ import annotations

import logging
from typing import Any

from semantic_browser.browser._utils import _assess_cors_risk

logger = logging.getLogger(__name__)


# ── Module-level header parsers ─────────────────────────────

def _parse_csp(csp: str) -> dict[str, Any]:
    """Parse CSP header —拆 directives, 标常见不安全 source."""
    directives: dict[str, list[str]] = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(None, 1)
        name = bits[0].lower()
        sources = bits[1].split() if len(bits) > 1 else []
        directives[name] = sources
    all_srcs = [s for vs in directives.values() for s in vs]
    return {
        "raw": csp,
        "directives": directives,
        "directive_names": list(directives.keys()),
        "has_unsafe_inline": "'unsafe-inline'" in all_srcs,
        "has_unsafe_eval": "'unsafe-eval'" in all_srcs,
        "allows_wildcard": "*" in all_srcs,
        "allows_data": "data:" in all_srcs,
        "allows_https": "https:" in all_srcs,
        "has_script_src": "script-src" in directives,
        "has_object_src": "object-src" in directives,
        "has_default_src": "default-src" in directives,
    }


def _parse_hsts(hsts: str) -> dict[str, Any]:
    """Strict-Transport-Security."""
    out = {"raw": hsts, "max_age": 0, "include_subdomains": False, "preload": False}
    for tok in hsts.split(";"):
        tok = tok.strip()
        if tok.lower().startswith("max-age="):
            try:
                out["max_age"] = int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif tok.lower() == "includesubdomains":
            out["include_subdomains"] = True
        elif tok.lower() == "preload":
            out["preload"] = True
    return out


def _parse_permissions_policy(pp: str) -> dict[str, Any]:
    """Permissions-Policy 解析成 {directive: allowed-origins 或 []}."""
    directives: dict[str, list[str]] = {}
    for part in pp.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split("=", 1)
        name = bits[0].strip().lower()
        sources: list[str] = []
        if len(bits) > 1:
            sources = bits[1].split()
        directives[name] = sources
    return {"raw": pp, "directives": directives}


def _parse_set_cookie(sc_value: str) -> dict[str, Any]:
    """解析单个 Set-Cookie 字符串."""
    parts = sc_value.split(";")
    first = parts[0].strip()
    name = ""
    value = ""
    if "=" in first:
        name, value = first.split("=", 1)
        name = name.strip()
        value = value.strip()
    out: dict[str, Any] = {
        "name": name,
        "value": value[:500],
        "httpOnly": False,
        "secure": False,
        "sameSite": "",
        "path": "",
        "domain": "",
        "max_age": None,
        "expires": "",
    }
    for tok in parts[1:]:
        tok = tok.strip()
        low = tok.lower()
        if low == "httponly":
            out["httpOnly"] = True
        elif low == "secure":
            out["secure"] = True
        elif low.startswith("samesite="):
            out["sameSite"] = tok.split("=", 1)[1]
        elif low.startswith("path="):
            out["path"] = tok.split("=", 1)[1]
        elif low.startswith("domain="):
            out["domain"] = tok.split("=", 1)[1]
        elif low.startswith("max-age="):
            try:
                out["max_age"] = int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif low.startswith("expires="):
            out["expires"] = tok.split("=", 1)[1]
    return out


# ── _HeadersMixin — mixed into BrowserController ────────────

class _HeadersMixin:
    """Security headers analysis methods — mixed into BrowserController."""

    async def get_security_headers(self, url: str) -> dict[str, Any] | None:
        """T40f: 给定 URL, 把响应头解析成结构化安全审计数据.

        Returns: {
          "url", "raw": {...全部 headers...},
          "csp": {directives, has_unsafe_inline, has_unsafe_eval, ...} 或 None,
          "hsts": {max_age, include_subdomains, preload} 或 None,
          "x_frame_options": str 或 None,
          "x_content_type_options": str 或 None,
          "referrer_policy": str 或 None,
          "coop": str 或 None,
          "coep": str 或 None,
          "permissions_policy": {directives: [...]} 或 None,
          "set_cookie_parsed": [{name, value, httpOnly, secure, sameSite, ...}],
          "score": "OK" | "weak" | "missing"   # 简易评分
        } 或 None (没拿到头).
        """
        raw = await self.get_response_headers(url)
        if raw is None:
            return None
        out: dict[str, Any] = {"url": url, "raw": raw}

        # CSP
        csp_val = raw.get("content-security-policy")
        out["csp"] = _parse_csp(csp_val) if csp_val else None

        # HSTS
        hsts_val = raw.get("strict-transport-security")
        out["hsts"] = _parse_hsts(hsts_val) if hsts_val else None

        out["x_frame_options"] = raw.get("x-frame-options")
        out["x_content_type_options"] = raw.get("x-content-type-options")
        out["referrer_policy"] = raw.get("referrer-policy")
        out["coop"] = raw.get("cross-origin-opener-policy")
        out["coep"] = raw.get("cross-origin-embedder-policy")

        pp_val = raw.get("permissions-policy")
        out["permissions_policy"] = _parse_permissions_policy(pp_val) if pp_val else None

        # Set-Cookie: header 不一定在 response_headers (httpx 通常会按 set-cookie 拆出)
        sc = raw.get("set-cookie") or raw.get("Set-Cookie")
        out["set_cookie_parsed"] = (
            [_parse_set_cookie(s) for s in (sc if isinstance(sc, list) else [sc])]
            if sc else []
        )

        # T42c: CORS 风险评估
        cors_origin = raw.get("access-control-allow-origin")
        cors_creds = raw.get("access-control-allow-credentials", "").lower() == "true"
        out["cors"] = {
            "allow_origin": cors_origin,
            "allow_credentials": cors_creds,
            "allow_methods": raw.get("access-control-allow-methods"),
            "allow_headers": raw.get("access-control-allow-headers"),
            "expose_headers": raw.get("access-control-expose-headers"),
            "max_age": raw.get("access-control-max-age"),
            "risk": _assess_cors_risk(cors_origin, cors_creds),
        }

        # 简易评分 (安全头覆盖度)
        score = self._compute_security_score(out)
        if score >= 6:
            out["score"] = "OK"
        elif score >= 3:
            out["score"] = "weak"
        else:
            out["score"] = "missing"
        # T63: numeric 分数 — 老的 string ("OK" / "weak" / "missing") 含义
        # 不明, agent 推理很难写阈值. 加 score_points / score_max 让 agent
        # 用 numeric 决策 (e.g. "score_points >= 4 才认为安全").
        out["score_points"] = score
        # T63.2: 修正 max — csp=2 + 6×1 (hsts/xfo/xcto/referrer/coop-or-coep) +
        # 2×1 (httpOnly/secure on first cookie) = 9, 不是 8. T63 注释笔误.
        out["score_max"] = 9
        # T63.2 (#10 修): letter grade A-F — 比 string "OK/weak/missing" 更直观,
        # agent 写阈值也方便 (e.g. "score_grade in {A,B}" 才信任). 阈值按 score_max
        # 比例: ≥80% A, ≥60% B, ≥40% C, ≥20% D, 否则 F.
        out["score_grade"] = self._grade_for_score(score)
        return out

    @staticmethod
    def _grade_for_score(score: int) -> str:
        """T63.2: T40f 安全头 score_points → A-F letter grade (单位测友好)."""
        score_pct = score / 9.0
        if score_pct >= 0.8:
            return "A"
        if score_pct >= 0.6:
            return "B"
        if score_pct >= 0.4:
            return "C"
        if score_pct >= 0.2:
            return "D"
        return "F"

    @staticmethod
    def _compute_security_score(parsed: dict[str, Any]) -> int:
        """T63.2: T40f 安全头累计分 (单位测友好). parsed 含 csp/hsts/x_frame_options
        /x_content_type_options/referrer_policy/coop/coep/set_cookie_parsed."""
        score = 0
        if parsed.get("csp"):
            score += 2
        if parsed.get("hsts"):
            score += 1
        if parsed.get("x_frame_options"):
            score += 1
        if parsed.get("x_content_type_options"):
            score += 1
        if parsed.get("referrer_policy"):
            score += 1
        if parsed.get("coop") or parsed.get("coep"):
            score += 1
        for sc_entry in parsed.get("set_cookie_parsed") or []:
            if sc_entry.get("httpOnly"):
                score += 1
            if sc_entry.get("secure"):
                score += 1
            break
        return score

    async def parse_csp(self) -> dict[str, Any]:
        """T44k: 把 CSP 头拆成 directive × source 列表, 标出危险配置.

        Returns {
          "page_url",
          "csp_raw": str | None,
          "directives": {directive_name: [source, ...], ...},
          "flags": [str, ...],   # 危险配置: unsafe-inline, unsafe-eval, * wildcard, data:, ...
          "missing_recommended": [str, ...],  # 缺失建议的 directive (script-src, frame-ancestors, base-uri)
        }
        """
        page = await self._ensure_page()
        hdrs = await self.get_security_headers(page.url)
        csp = hdrs.get("csp")
        if not csp:
            return {"page_url": page.url, "csp_raw": None, "directives": {},
                    "flags": ["no_csp"], "missing_recommended": ["script-src", "frame-ancestors", "base-uri"]}
        raw = csp.get("raw", "") if isinstance(csp, dict) else str(csp)
        directives: dict[str, list[str]] = {}
        flags: list[str] = []
        for d in raw.split(";"):
            d = d.strip()
            if not d:
                continue
            parts = d.split(None, 1)
            name = parts[0].lower()
            sources = parts[1].split() if len(parts) > 1 else []
            directives[name] = sources
            for s in sources:
                sl = s.lower()
                if "'unsafe-inline'" in sl or s == "*":
                    flags.append(f"{name} contains {s}")
                if "'unsafe-eval'" in sl:
                    flags.append(f"{name} allows eval()")
                if s == "data:":
                    flags.append(f"{name} allows data: URI")
        recommended = ["script-src", "default-src", "frame-ancestors", "base-uri", "form-action"]
        missing = [r for r in recommended if r not in directives]
        return {
            "page_url": page.url,
            "csp_raw": raw,
            "directives": directives,
            "flags": flags,
            "missing_recommended": missing,
        }
