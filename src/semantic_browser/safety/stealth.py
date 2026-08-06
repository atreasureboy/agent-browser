"""T106 + T118: Stealth mode for Playwright — 反 anti-bot 检测.

profile-coherent 设计 (T118 修订):

底层永远是 Chromium (`chromium.launch()`). 风控检测不是看 UA 是不是"真",
而是看 UA / navigator.platform / navigator.languages / sec-ch-ua-* / plugins /
window.chrome 是否**自洽**. 之前混排 Firefox/Safari/Edge UA 反而更易被识别:
JS 引擎是 V8, window.chrome 存在, 但 UA 说自己是 Firefox — 三方矛盾.

每个 Profile 是自洽的 fingerprint bundle. pick_profile() 一次选中, 浏览器
的所有可观察字段 (UA, platform, locale, Accept-Language, Client Hints,
plugins) 跟着对齐.

参考 Crawl4AI browser_manager.py BROWSER_DISABLE_OPTIONS 保留 anti-automation flags.

注: 真要攻 Incapsula/Cloudflare 需 playwright-stealth 之类. 这里是
最小修改 + 减分用. 大部分还是靠 prompt LLM 协商/或 detection 后 fast-fail.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Tuple


# ── Browser launch flags ────────────────────────────────────────────────
# T106: 参考 Crawl4AI BROWSER_DISABLE_OPTIONS, 去掉暴露 headless/auto 的 features
# T118: 新增 --disable-blink-features=AutomationControlled — 隐藏 navigator.webdriver
#       的最直接手段, 配合 init script 兜底
BROWSER_DISABLE_OPTIONS: List[str] = [
    # 去 background-networking (暴露 automation)
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    # 去 breakpad (crash reporter 暴露真实 client)
    "--disable-breakpad",
    # 去 client-side-phishing-detection (暴露 enterprise policy)
    "--disable-client-side-phishing-detection",
    # 去 component-extensions-with-background-pages
    "--disable-component-extensions-with-background-pages",
    # 去 default apps / extensions
    "--disable-default-apps",
    "--disable-extensions",
    # 去 TranslateUI (暴露 Google)
    "--disable-features=TranslateUI",
    # 去 hang monitor
    "--disable-hang-monitor",
    # 去 ipc-flooding-protection
    "--disable-ipc-flooding-protection",
    # 去 popup blocking (Cloudflare 检查)
    "--disable-popup-blocking",
    # 去 prompt-on-repost
    "--disable-prompt-on-repost",
    # 去 sync (暴露 Google account)
    "--disable-sync",
    # T118: 去 blink AutomationControlled feature — 直接关掉 navigator.webdriver
    # 暴露的最常见路径 (即使 init script 没跑, 这条也会让 navigator.webdriver=false)
    "--disable-blink-features=AutomationControlled",
    # force sRGB color profile
    "--force-color-profile=srgb",
    # metrics recording only
    "--metrics-recording-only",
    # 不 first run
    "--no-first-run",
    # 不存密码
    "--password-store=basic",
    # 不 use mock keychain
    "--use-mock-keychain",
]


# ── Profile 数据结构 ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Profile:
    """一个 fingerprint profile — 所有观察字段必须自洽.

    字段语义:
      - user_agent:        navigator.userAgent 的值
      - platform:          navigator.platform 的值 (与 UA 平台 token 一致)
      - platform_header:   sec-ch-ua-platform 头的值 (与 platform 一致)
      - locale:            Playwright context locale (影响 navigator.language)
      - accept_language:   HTTP Accept-Language 头 (与 navigator.languages 一致)
      - languages:         navigator.languages 的值
      - sec_ch_ua:         sec-ch-ua 头的值 (与 UA 主版本号一致)
      - sec_ch_ua_mobile:  sec-ch-ua-mobile 头的值 (?0=desktop, ?1=mobile)
      - plugins:           navigator.plugins 应该暴露的插件名集合
                           (用 Chrome 真实默认插件名 — CreepJS 比对基线)
    """

    user_agent: str
    platform: str
    platform_header: str
    locale: str
    accept_language: str
    languages: Tuple[str, ...]
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    plugins: Tuple[str, ...] = field(default_factory=tuple)


# Chrome 120 真实默认 plugins (Linux Chrome 报告的基线 — 与 platform 无关, 这
# 5 个名字几乎所有 Chrome 版本都会带, 是 CreepJS 重点比对项)
_CHROME_DEFAULT_PLUGINS: Tuple[str, ...] = (
    "PDF Viewer",
    "Chrome PDF Viewer",
    "Chromium PDF Viewer",
    "Microsoft Edge PDF Viewer",
    "WebKit built-in PDF",
)


# ── Profile 列表 (T118: 全部是 Chromium-family; 删 Firefox / Safari) ─────
PROFILES: List[Profile] = [
    # ── Chrome Windows (5 条, 最常见) ──
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="Win32",
        platform_header="Windows",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        platform="Win32",
        platform_header="Windows",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Google Chrome";v="119", "Chromium";v="119", "Not_A Brand";v="24"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        platform="Win32",
        platform_header="Windows",
        locale="en-GB",
        accept_language="en-GB,en;q=0.9",
        languages=("en-GB", "en"),
        sec_ch_ua='"Chromium";v="118", "Not_A Brand";v="24", "Google Chrome";v="118"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="Win32",
        platform_header="Windows",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        platform="Win32",
        platform_header="Windows",
        locale="de-DE",
        accept_language="de-DE,de;q=0.9,en;q=0.8",
        languages=("de-DE", "de", "en"),
        sec_ch_ua='"Google Chrome";v="121", "Not_A Brand";v="8", "Chromium";v="121"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),

    # ── Chrome macOS (3 条) ──
    Profile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="MacIntel",
        platform_header="macOS",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        platform="MacIntel",
        platform_header="macOS",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Google Chrome";v="119", "Chromium";v="119", "Not_A Brand";v="24"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        platform="MacIntel",
        platform_header="macOS",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Google Chrome";v="121", "Not_A Brand";v="8", "Chromium";v="121"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),

    # ── Edge Windows (2 条, Edge UA 不带 Chrome 字串, 必须带 Edg/) ──
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
        platform="Win32",
        platform_header="Windows",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Microsoft Edge";v="120"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
        platform="Win32",
        platform_header="Windows",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Microsoft Edge";v="119", "Chromium";v="119", "Not_A Brand";v="24"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),

    # ── Linux Chrome (2 条, 服务器 headless 环境专用) ──
    Profile(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="Linux x86_64",
        platform_header="Linux",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
    Profile(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        platform="Linux x86_64",
        platform_header="Linux",
        locale="en-US",
        accept_language="en-US,en;q=0.9",
        languages=("en-US", "en"),
        sec_ch_ua='"Google Chrome";v="119", "Chromium";v="119", "Not_A Brand";v="24"',
        sec_ch_ua_mobile="?0",
        plugins=_CHROME_DEFAULT_PLUGINS,
    ),
]


def pick_profile() -> Profile:
    """T118: 随机选一个 profile. 浏览器所有 fingerprint 字段都从这个 profile 派生."""
    return random.choice(PROFILES)


def random_user_agent() -> str:
    """T106 兼容: 返回随机 UA. 实际现在只返回 Chromium-family UA.

    保留这个名字是因为 controller.py:240 还在引用, 不破坏向后兼容.
    """
    return pick_profile().user_agent


# ── Init script ─────────────────────────────────────────────────────────
# T118: 改为 profile-coherent — 读 window.__SB_PROFILE__ (由 controller 在
# STEALTH_JS 之前注入), 然后按 profile 字段覆盖 navigator.* 字段.
#
# 设计要点:
#   1. webdriver: 所有 profile 一致, 直接覆盖
#   2. platform / languages / plugins: 跟 profile 走 (从 __SB_PROFILE__ 读)
#   3. plugins: 用真 PluginArray 形状 (item/namedItem/refresh + 真实 Chrome
#      默认插件名), 不是裸数组
#   4. chrome: 给完整的现代 Chrome 形状 (OnInstalledReason / PlatformArch /
#      PlatformOs / RequestUpdateCheckStatus), 不留空对象
#   5. WebGL vendor/renderer: 不再覆盖 — 让 Chromium 自己 swiftshader 报
#      真值, 固定串 'Intel Inc.'/'WebKit' 是熵为 0 的 stale 模式, 比裸奔
#      更容易被识别
#
# 注入顺序由 controller 控制: __SB_PROFILE__ 先注入, STEALTH_JS 后注入,
# 后者在同一 microtask 内执行, window 全局可见.
STEALTH_JS = r"""
(function () {
  'use strict';

  // 读 profile (由 controller 提前注入). 缺字段时静默跳过, 不抛.
  var P = (typeof window !== 'undefined' && window.__SB_PROFILE__) || {};

  // ── 1. webdriver ──
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: function () { return undefined; },
      configurable: true,
    });
  } catch (_) {}

  // ── 1b. hardwareConcurrency & deviceMemory ──
  try {
    Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
      get: function () { return 8; },
      configurable: true,
    });
    Object.defineProperty(Navigator.prototype, 'deviceMemory', {
      get: function () { return 8; },
      configurable: true,
    });
  } catch (_) {}

  // ── 1c. window outer dimensions alignment ──
  try {
    if (window.outerWidth === 0) {
      Object.defineProperty(window, 'outerWidth', { get: function() { return window.innerWidth || 1280; }, configurable: true });
    }
    if (window.outerHeight === 0) {
      Object.defineProperty(window, 'outerHeight', { get: function() { return window.innerHeight || 800; }, configurable: true });
    }
  } catch (_) {}

  // ── 1d. permissions.query mock ──
  try {
    if (navigator.permissions && navigator.permissions.query) {
      var origQuery = navigator.permissions.query;
      navigator.permissions.query = function (parameters) {
        if (parameters && parameters.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission, onchange: null });
        }
        return origQuery.apply(this, arguments);
      };
    }
  } catch (_) {}

  // ── 2. platform ──
  if (P.platform) {
    try {
      Object.defineProperty(Navigator.prototype, 'platform', {
        get: function () { return P.platform; },
        configurable: true,
      });
    } catch (_) {}
  }

  // ── 3. languages ──
  if (P.languages && P.languages.length) {
    try {
      var langs = P.languages;
      Object.defineProperty(Navigator.prototype, 'languages', {
        get: function () { return langs; },
        configurable: true,
      });
    } catch (_) {}
  }

  // ── 4. plugins — 真 PluginArray 形状 + 真实 Chrome 默认插件名 ──
  if (P.plugins && P.plugins.length) {
    try {
      var names = P.plugins;
      var arr = Object.create(PluginArray.prototype);
      names.forEach(function (name, i) {
        var p = Object.create(Plugin.prototype, {
          name:        { value: name, enumerable: true },
          filename:    { value: name.replace(/ /g, '') + '.pdf' },
          description: { value: 'Portable Document Format', enumerable: true },
          length:      { value: 1, enumerable: true },
        });
        // index access
        arr[i] = p;
      });
      arr.length = names.length;
      arr.item = function (i) { return arr[i] || null; };
      arr.namedItem = function (n) {
        for (var i = 0; i < arr.length; i++) {
          if (arr[i] && arr[i].name === n) return arr[i];
        }
        return null;
      };
      arr.refresh = function () {};
      Object.defineProperty(Navigator.prototype, 'plugins', {
        get: function () { return arr; },
        configurable: true,
      });
    } catch (_) {}
  }

  // ── 5. chrome — 真实 Chrome 120+ 形状, 不留空对象 ──
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.app) {
      window.chrome.app = {
        isInstalled: false,
        InstallState: {
          DISABLED: 'disabled',
          INSTALLED: 'installed',
          NOT_INSTALLED: 'not_installed',
        },
        RunningState: {
          CANNOT_RUN: 'cannot_run',
          READY_TO_RUN: 'ready_to_run',
          RUNNING: 'running',
        },
        getDetails: function () { return null; },
        getIsInstalled: function () { return false; },
      };
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        OnInstalledReason: {
          CHROME_UPDATE: 'chrome_update',
          INSTALL: 'install',
          SHARED_MODULE_UPDATE: 'shared_module_update',
          UPDATE: 'update',
        },
        OnRestartRequiredReason: {
          APP_UPDATE: 'app_update',
          OS_UPDATE: 'os_update',
          PERIODIC: 'periodic',
        },
        PlatformArch: {
          ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64',
          X86_32: 'x86-32', X86_64: 'x86-64',
        },
        PlatformOs: {
          ANDROID: 'android', CROS: 'cros', FUCHSIA: 'fuchsia',
          LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win',
        },
        RequestUpdateCheckStatus: {
          NO_UPDATE: 'no_update',
          THROTTLED: 'throttled',
          UPDATE_AVAILABLE: 'update_available',
        },
        OnUserScriptSettingChanged: {
          CONFIRM: 'confirm',
          ALWAYS_ALLOW: 'always_allow',
        },
        connect: function () {},
        sendMessage: function () {},
      };
    }
    if (typeof window.chrome.csi !== 'function') {
      window.chrome.csi = function () { return { startE: Date.now(), onloadT: Date.now() }; };
    }
    if (typeof window.chrome.loadTimes !== 'function') {
      window.chrome.loadTimes = function () { return { requestTime: Date.now() / 1000 }; };
    }
  } catch (_) {}

  // ── 6. WebGL: 不覆盖 vendor/renderer. 让 Chromium 自己的 swiftshader / GPU
  //      串直出. 固定串 'Intel Inc.' / 'WebKit' 是熵为 0 的 stale 模式,
  //      比裸奔更容易被识别 (CreepJS entropy check).
})();
"""
