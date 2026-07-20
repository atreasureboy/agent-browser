# T103: Agent 真测多个 e-commerce 站

user 让 "试试风控没那么严的网站". 真测 7 个 e-commerce + 4 个 blog + Wikipedia.

## 真测结果表

| 站 | URL | 结果 |
|---|---|---|
| Amazon | amazon.com/s?k=iphone+15 | anti-bot 挡, 0 sources |
| eBay | ebay.com/sch?iphone+15 | anti-bot 挡, 0 sources (SORRY 页面) |
| Best Buy | bestbuy.com | ERR |
| Walmart | walmart.com | ERR |
| Newegg | newegg.com | ERR |
| Target | target.com | ERR |
| Etsy | etsy.com | ERR |
| AliExpress | aliexpress.com | ERR |
| Wikipedia | en.wikipedia.org/wiki/Python | 813 text blocks, 0 sections |
| Rust blog | blog.rust-lang.org | 1 block (CSP 抗 bot) |
| LWN | lwn.net | 0 sections (识别为 login page) |
| InfoQ | infoq.com | 46 blocks, 3 sections |

## 关键发现: 3 个独立问题

### 1. Anti-bot (大站都封)
Amazon/eBay/Etsy: Playwright 默认 fingerprint 被检测. **不是工具 bug** — 真要攻需 `playwright-stealth` (ToS 也禁).

### 2. Readability 对现代 layout 失败 (ContentExtractor gap)
Wikipedia 813 text blocks 但 0 sections — Readability 算法对 wiki/forum 失败.
InfoQ 46 blocks + 3 sections — article layout 才能提取.

### 3. **Playwright state leak 跨 query (生产 bug)**
daemon browse_failed 错误: `'Page.goto: Page crashed\n  - navigating to "https://www.infoq.com/news/2024/01/python-3.13-released/", waiting until "networkidle"'`

之前 Amazon/eBay 失败后, **共享 Playwright 浏览器卡在坏状态**. 后续所有 query 返 0 sources.
daemon **不隔离失败** — 1 个坏 site 污染所有 caller.

真修法 (T104 待做): browse 失败时 reset context 到 about:blank, 或 pool 重 acquire 新 controller.

## 工具能力真实评估

| 场景 | 评估 |
|---|---|
| 现代 e-commerce (Amazon/eBay) | ❌ anti-bot, 抓不到 |
| 老式 article blog (InfoQ) | ✅ work |
| Wiki 资料 | ⚠ fetch OK, 抽取差 |
| 通用 web | ⚠ Readability 是限制 |
