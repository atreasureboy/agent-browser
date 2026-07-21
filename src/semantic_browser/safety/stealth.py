"""T106: Stealth mode for Playwright — 反 anti-bot 检测.

参考 Crawl4AI browser_manager.py BROWSER_DISABLE_OPTIONS:
- 禁用暴露自动化身份的 Chromium features
- 隐藏 navigator.webdriver 标志
- 配合 antibot.py 检测

注: 真要攻 Incapsula/Cloudflare 需 playwright-stealth 之类. 这里是
最小修改 + 减分用. 大部分还是靠 prompt LLM 协商/或 detection 后 fast-fail.
"""
from __future__ import annotations

import random
from typing import List


# T106: 参考 Crawl4AI BROWSER_DISABLE_OPTIONS, 去掉暴露 headless/auto 的 features
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


# T106: 减分用 stealth JS — 在 page load 前 inject
# 关键: 隐藏 navigator.webdriver (Playwright 默认 true), 模拟真人 plugins/languages
STEALTH_JS = """
// 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// 模拟 plugins
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4].map(i => ({ name: 'Plugin ' + i }))
});
// 模拟 languages
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en']
});
// Chrome runtime 模拟
window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
// 隐藏 webgl 暴露
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'WebKit';
  return getParameter.call(this, p);
};
"""


# T106: 减分用 UA list — 真 UA 不暴露 automation
USER_AGENTS: List[str] = [
    # Chrome Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Chrome Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Firefox Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    # Firefox Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Safari Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    # Edge Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]


def random_user_agent() -> str:
    """T106: 减分用 — 随机真 UA, 避免 fingerprint 集中."""
    return random.choice(USER_AGENTS)
