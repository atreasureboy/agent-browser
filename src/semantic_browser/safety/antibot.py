"""T106: Anti-bot detection — 分层 pattern 匹配 (参考 Crawl4AI antibot_detector.py).

3-tier 检测: Tier 1 高置信 structural markers, Tier 2 中置信 generic terms, Tier 3 structural integrity.

检测哲学 (Crawl4AI 借鉴):
- false positive 廉价 (有 fallback 兜底), false negative 致命 (user 拿到垃圾)
- err on the side of detection

实际生产场景:
- Akamai/Cloudflare/PerimeterX/DataDome/Imperva/Sucuri 都有特征 pattern
- 不需要真去解决 challenge (那需要更复杂的 stealth)
- 检测到就 fast-fail, 返回空 answer + 说明原因
- 减少无意义的 LLM/M3 调用
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


# Tier 1: high-confidence structural markers
# (structural patterns unique to block pages, virtually never in real content)
_TIER1_PATTERNS = [
    # Akamai
    (re.compile(r"Reference\s*#\s*[\d]+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.IGNORECASE),
    # Cloudflare challenge form
    (re.compile(r'challenge-form.*?__cf_chl_f_tk=', re.IGNORECASE | re.DOTALL)),
    # Cloudflare error code spans
    (re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.IGNORECASE)),
    # PerimeterX
    (re.compile(r"window\._pxAppId\s*=", re.IGNORECASE)),
    # DataDome captcha delivery
    (re.compile(r"captcha-delivery\.com", re.IGNORECASE)),
    # Imperva/Incapsula
    (re.compile(r"_Incapsula_Resource", re.IGNORECASE)),
    # Sucuri firewall
    (re.compile(r"Sucuri\s+WebSite\s+Firewall", re.IGNORECASE)),
    # Distil Networks
    (re.compile(r"distil_captcha\.js", re.IGNORECASE)),
    # hCaptcha challenge script
    (re.compile(r"hcaptcha\.com/1/api\.js", re.IGNORECASE)),
    # reCAPTCHA script
    (re.compile(r"google\.com/recaptcha/api\.js", re.IGNORECASE)),
    # Generic "blocked" markers
    (re.compile(r"<title>\s*(?:Access\s+Denied|Forbidden|Just\s+a\s+Moment)", re.IGNORECASE)),
]

# Tier 2: medium-confidence generic terms
# (only trigger on short pages to avoid matching legit content)
_TIER2_PATTERNS = [
    (re.compile(r"Access\s+Denied", re.IGNORECASE)),
    (re.compile(r"Checking\s+your\s+browser", re.IGNORECASE)),
    (re.compile(r"<title>\s*Just\s+a\s+moment", re.IGNORECASE)),
    (re.compile(r"Cloudflare\s+Ray\s+ID", re.IGNORECASE)),
    (re.compile(r"Access\s+to\s+This\s+Page\s+Has\s+Been\s+Blocked", re.IGNORECASE)),
    (re.compile(r"You\s+have\s+been\s+blocked", re.IGNORECASE)),
    (re.compile(r"Request\s+Rejected", re.IGNORECASE)),
    (re.compile(r"Sorry,\s+you\s+have\s+been\s+blocked", re.IGNORECASE)),
]

# Tier 3: structural integrity (silent blocks)
# (page is mostly empty or all noise — likely a block)
_TIER3_INDICATORS = [
    # Very short page with no semantic content
    # (evaluated separately, not a pattern)
]

_SHORT_PAGE_THRESHOLD = 1500  # bytes; under = probably a block page


def detect_antibot(
    body: str,
    status: int = 200,
) -> Tuple[bool, Optional[str]]:
    """T106: 分层 pattern + 状态码检测.

    Returns:
        (blocked, reason) — blocked=True 时 reason 解释哪个 anti-bot 系统.

    Algorithm:
    1. status 403/503 with HTML body → always blocked
    2. Tier 1 patterns → blocked (high confidence)
    3. Tier 2 patterns on short body → blocked (medium)
    4. Tier 3: body too short + no semantic content → silent block
    """
    # Tier 0: status code check
    if status in (403, 503) and body:
        return True, f"HTTP {status} with body (likely block page)"

    # Tier 1: structural markers (any one is decisive)
    for pat in _TIER1_PATTERNS:
        if pat.search(body):
            return True, f"anti-bot pattern: {pat.pattern[:60]}"

    # Tier 2: generic terms on short pages
    if len(body) < _SHORT_PAGE_THRESHOLD:
        for pat in _TIER2_PATTERNS:
            if pat.search(body):
                return True, f"anti-bot short-page pattern: {pat.pattern[:60]}"

    # Tier 3: structural integrity — body 极短 (< 500 bytes) 且无 h1/h2/article
    if len(body) < 500 and not re.search(r"<h[12]|<article", body, re.IGNORECASE):
        return True, f"anti-bot silent block: body too short ({len(body)} bytes)"

    return False, None
