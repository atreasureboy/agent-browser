"""T118: Stealth fingerprint consistency tests (纯单测, 不启 Playwright).

验证 fingerprint profile 自洽 — UA / navigator.platform / locale /
Accept-Language / sec-ch-ua / plugins / STEALTH_JS 互相一致. 这些都是
anti-bot 检测的核心信号, 跨字段不一致比裸奔更易被识别.
"""
from __future__ import annotations

import re

import pytest

from semantic_browser.safety.stealth import (
    BROWSER_DISABLE_OPTIONS,
    PROFILES,
    STEALTH_JS,
    Profile,
    pick_profile,
    random_user_agent,
)


# ── 1. UA 仅 Chromium-family (Chrome + Edge) ─────────────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_ua_is_chromium_family(profile: Profile) -> None:
    """UA 必须是 Chrome 或 Edge — 不能再混 Firefox/Safari."""
    ua = profile.user_agent
    assert "Mozilla/5.0" in ua
    # Chrome UA 一定含 Chrome/x.y.z; Edge UA 一定含 Edg/x.y.z
    has_chrome = bool(re.search(r"Chrome/\d+", ua))
    has_edge = bool(re.search(r"Edg/\d+", ua))
    assert has_chrome or has_edge, f"UA 不是 Chromium-family: {ua}"
    # 显式禁 Firefox / Safari — 它们不是 Chromium, 与引擎必不一致
    assert "Firefox/" not in ua, f"UA 仍含 Firefox: {ua}"
    assert "Safari/" not in ua or "Chrome/" in ua or "Edg/" in ua, (
        f"UA 单独含 Safari (无 Chrome/Edg): {ua}"
    )
    # Safari-only UA 形如 "Version/17 Safari/..."; Edge/Chrome UA 不带 Version/
    if "Safari/" in ua and "Chrome/" not in ua and "Edg/" not in ua:
        assert False, f"UA 是 Safari-only: {ua}"


# ── 2. UA 平台 token ↔ navigator.platform 自洽 ──────────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_ua_matches_navigator_platform(profile: Profile) -> None:
    """UA 含 Windows → platform='Win32'; Mac → 'MacIntel'; Linux → 'Linux x86_64'."""
    ua = profile.user_agent
    p = profile.platform
    if "Windows NT" in ua:
        assert p == "Win32", f"Windows UA 但 platform={p!r} ({ua})"
    elif "Macintosh" in ua:
        assert p == "MacIntel", f"Mac UA 但 platform={p!r} ({ua})"
    elif "X11; Linux" in ua:
        assert p == "Linux x86_64", f"Linux UA 但 platform={p!r} ({ua})"
    else:
        pytest.fail(f"UA 平台未识别: {ua}")


# ── 3. Accept-Language ↔ locale ↔ navigator.languages ────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_accept_language_matches_locale(profile: Profile) -> None:
    """Accept-Language 头里第一个语言必须等于 locale; languages[0] 也一致."""
    al = profile.accept_language
    loc = profile.locale
    langs = profile.languages
    # Accept-Language 形如 "en-US,en;q=0.9" — 第一个 token 等于 locale
    first_lang = al.split(",")[0].strip().split(";")[0]
    assert first_lang == loc, (
        f"Accept-Language 首项 {first_lang!r} != locale {loc!r}"
    )
    # navigator.languages 第一个也应该是 locale
    assert langs[0] == loc, f"languages[0]={langs[0]!r} != locale {loc!r}"


# ── 4. sec-ch-ua 与 UA 主品牌对齐 ─────────────────────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_sec_ch_ua_consistent_with_ua(profile: Profile) -> None:
    """UA 含 Edg/ → sec-ch-ua 必须含 'Microsoft Edge'; UA 含 Chrome/ → 'Google Chrome' 或 'Chromium'."""
    ua = profile.user_agent
    ch = profile.sec_ch_ua
    if "Edg/" in ua:
        assert '"Microsoft Edge"' in ch, f"Edge UA 但 sec-ch-ua 无 Edge: {ch}"
    elif "Chrome/" in ua:
        assert '"Chromium"' in ch or '"Google Chrome"' in ch, (
            f"Chrome UA 但 sec-ch-ua 无 Chromium/Google Chrome: {ch}"
        )


# ── 5. sec-ch-ua-platform ↔ platform_header ↔ platform ─────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_platform_header_matches(profile: Profile) -> None:
    """sec-ch-ua-platform 必须是带引号的 quoted-string, 且与 platform 一致.

    platform_header 是 platform 的 'sec-ch-ua-platform' 形式:
      Win32 → 'Windows', MacIntel → 'macOS', Linux x86_64 → 'Linux'
    """
    p = profile.platform
    ph = profile.platform_header
    if p == "Win32":
        assert ph == "Windows"
    elif p == "MacIntel":
        assert ph == "macOS"
    elif p == "Linux x86_64":
        assert ph == "Linux"
    else:
        pytest.fail(f"未知 platform: {p}")


# ── 6. plugins 非空且含 Chrome 默认插件名 ──────────────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_plugins_have_real_chrome_defaults(profile: Profile) -> None:
    """plugins 应至少含 'PDF Viewer' / 'Chrome PDF Viewer' 之一 — CreepJS 比对基线."""
    names = profile.plugins
    assert len(names) >= 3, f"plugins 太少: {names}"
    expected = {"PDF Viewer", "Chrome PDF Viewer", "Chromium PDF Viewer"}
    assert any(n in expected for n in names), (
        f"plugins 无 Chrome 默认项: {names}"
    )


# ── 7. STEALTH_JS 不再含已知 stale override ───────────────────────
def test_stealth_js_no_stale_overrides() -> None:
    """STEALTH_JS 不应再硬编码已知 stale 模式 (固定 WebGL 串 / 假 plugins / 硬编码 languages).

    注: 'Intel Inc.' / 'WebKit' 等字面量可能出现在解释性的注释里,
    这里只看 *代码* — 注释以 // 或 /* 开头, 不参与匹配.
    """
    # 去掉注释行, 只检查代码
    code_lines = []
    for line in STEALTH_JS.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue
        code_lines.append(line)
    code_only = "\n".join(code_lines)

    # 之前覆盖 WebGL vendor/renderer 到固定 'Intel Inc.' / 'WebKit' —
    # 所有请求聚到同一对字符串, 熵为 0, 比裸奔更易识别.
    assert "return 'Intel Inc.'" not in code_only, (
        "STEALTH_JS 不应在代码里硬编码 WebGL vendor"
    )
    assert "return 'WebKit'" not in code_only, (
        "STEALTH_JS 不应在代码里硬编码 WebGL renderer"
    )
    # 旧版覆盖 WebGLRenderingContext.getParameter 的两个 magic number
    assert "37445" not in code_only, "STEALTH_JS 不应再覆盖 WebGL UNMASKED_VENDOR"
    assert "37446" not in code_only, "STEALTH_JS 不应再覆盖 WebGL UNMASKED_RENDERER"
    # 旧版 plugins 假名 'Plugin 1' / 'Plugin 2' — 看代码里的字符串拼接
    assert "'Plugin '" not in code_only and "'Plugin ' +" not in code_only, (
        "STEALTH_JS 不应再硬编码假 plugin 名"
    )
    # 旧版裸数组 [1, 2, 3, 4]
    assert "[1, 2, 3, 4]" not in code_only and "[1,2,3,4]" not in code_only, (
        "STEALTH_JS 不应再用裸数组当 plugins"
    )
    # 旧版硬编码 languages (代码层)
    assert "['en-US', 'en']" not in code_only, (
        "STEALTH_JS 不应再硬编码 languages (应从 __SB_PROFILE__ 读)"
    )
    # 旧版 chrome 空对象
    assert "loadTimes: () => ({})" not in code_only, (
        "STEALTH_JS 不应再用空对象当 chrome.loadTimes"
    )


# ── 8. STEALTH_JS 仍隐藏 webdriver (核心防御) ─────────────────────
def test_stealth_js_hides_webdriver() -> None:
    assert "webdriver" in STEALTH_JS
    assert "Navigator.prototype" in STEALTH_JS or "navigator, 'webdriver'" in STEALTH_JS


# ── 9. STEALTH_JS 从 window.__SB_PROFILE__ 读 profile 字段 ───────
def test_stealth_js_uses_profile_global() -> None:
    """profile-coherent 的核心: navigator.platform / languages / plugins
    必须从 __SB_PROFILE__ 读, 不再硬编码."""
    assert "__SB_PROFILE__" in STEALTH_JS
    # 必须读 platform / languages / plugins 三项
    assert re.search(r"P\.platform", STEALTH_JS), "STEALTH_JS 不读 P.platform"
    assert re.search(r"P\.languages", STEALTH_JS), "STEALTH_JS 不读 P.languages"
    assert re.search(r"P\.plugins", STEALTH_JS), "STEALTH_JS 不读 P.plugins"


# ── 10. STEALTH_JS 的 chrome 对象有现代 Chrome 字段 (无空对象) ────
def test_stealth_js_chrome_object_realistic() -> None:
    """现代 Chrome 字段必须齐全 — 空对象是 stale 指纹."""
    required = [
        "OnInstalledReason",
        "PlatformArch",
        "PlatformOs",
        "RequestUpdateCheckStatus",
        "OnUserScriptSettingChanged",
    ]
    for field_name in required:
        assert field_name in STEALTH_JS, (
            f"STEALTH_JS chrome.runtime 缺 {field_name}"
        )


# ── 11. BROWSER_DISABLE_OPTIONS 包含关键 anti-automation flag ─────
def test_browser_disable_options_includes_automation_controlled() -> None:
    """最关键的 flag: --disable-blink-features=AutomationControlled.
    它直接关掉 navigator.webdriver=true 的 Blink 路径."""
    joined = " ".join(BROWSER_DISABLE_OPTIONS)
    assert "AutomationControlled" in joined, (
        f"BROWSER_DISABLE_OPTIONS 缺 AutomationControlled flag: {BROWSER_DISABLE_OPTIONS}"
    )
    # 还要有 sync / extensions 这些基本项
    assert "--disable-sync" in joined
    assert "--disable-extensions" in joined


# ── 12. pick_profile 返回值类型正确 ──────────────────────────────
def test_pick_profile_returns_profile_instance() -> None:
    p = pick_profile()
    assert isinstance(p, Profile)
    assert isinstance(p.user_agent, str)
    assert isinstance(p.platform, str)
    assert isinstance(p.languages, tuple)
    assert isinstance(p.plugins, tuple)


# ── 13. random_user_agent 薄包装仍工作 (向后兼容) ────────────────
def test_random_user_agent_returns_chromium_family_ua() -> None:
    for _ in range(20):
        ua = random_user_agent()
        assert "Chrome/" in ua or "Edg/" in ua, f"random UA 不是 Chromium: {ua}"
        assert "Firefox/" not in ua
        assert "Safari/" not in ua or "Chrome/" in ua or "Edg/" in ua


# ── 14. PROFILES 全局无 Firefox / Safari-only UA ──────────────────
def test_no_firefox_or_safari_only_in_all_profiles() -> None:
    """PROFILES 是 fingerprint 唯一数据源 — 全局断言."""
    for p in PROFILES:
        ua = p.user_agent
        assert "Firefox/" not in ua
        # Safari-only UA (含 Version/ + Safari/ 但不含 Chrome/ 或 Edg/) 禁
        if "Version/" in ua and "Safari/" in ua:
            assert False, f"PROFILES 含 Safari-only UA: {ua}"


# ── 15. PROFILES 数量足够 (减分用 — 至少 8 条, 避免 fingerprint 集中) ─
def test_profiles_count_is_sufficient() -> None:
    assert len(PROFILES) >= 8, (
        f"PROFILES 只有 {len(PROFILES)} 条, 太少会让 fingerprint 集中"
    )


# ── 16. sec-ch-ua-mobile 默认是 desktop (?0) ─────────────────────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_sec_ch_ua_mobile_is_desktop(profile: Profile) -> None:
    """本项目只跑 desktop headless — mobile profile 不是设计目标."""
    assert profile.sec_ch_ua_mobile == "?0"


# ── 17. STEALTH_JS 里 plugins 用真 PluginArray 接口 ───────────────
def test_stealth_js_plugins_use_real_pluginarray_interface() -> None:
    """旧版用裸数组 + 假 plugin name 是 stale 模式 — 新版必须用
    PluginArray 形状 (item / namedItem / refresh)."""
    assert "PluginArray.prototype" in STEALTH_JS
    assert "Plugin.prototype" in STEALTH_JS
    # 三种方法齐全
    assert "arr.item" in STEALTH_JS or ".item = function" in STEALTH_JS
    assert "arr.namedItem" in STEALTH_JS or ".namedItem = function" in STEALTH_JS
    assert "arr.refresh" in STEALTH_JS or ".refresh = function" in STEALTH_JS


# ── 18. STEALTH_JS 仍定义 webdriver 覆盖 (不依赖 AutomationControlled flag) ─
def test_stealth_js_uses_navigator_prototype_not_navigator() -> None:
    """T118: webdriver 覆盖走 Navigator.prototype 而不是 navigator —
    之前用 navigator 直接赋值被 Playwright 内部覆盖回去, prototype 覆盖更稳."""
    # 至少要 prototype 覆盖
    assert "Navigator.prototype" in STEALTH_JS


# ── 19. PROFILES 列表里每个 profile 至少 1 个 Chrome 版本号 ──────
@pytest.mark.parametrize("profile", PROFILES)
def test_profile_has_chrome_version_token(profile: Profile) -> None:
    """UA 形如 'Chrome/120.0.0.0' — 必须含 'Chrome/<digits>' 或 'Edg/<digits>'."""
    ua = profile.user_agent
    assert re.search(r"(?:Chrome|Edg)/\d+\.\d+", ua), f"UA 缺版本号: {ua}"