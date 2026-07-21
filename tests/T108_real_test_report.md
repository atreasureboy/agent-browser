# T108 E2E 验证报告

## Test A: Amazon (主路径成功)

```
URL: https://www.amazon.com/s?k=iphone+15
RESULT: page_type=list, text_blocks=155, links=316
fallback_source: <primary> (没走 fallback, 主路径已成功)
products:
  - "Apple iPhone 15, 128GB, Pink - Unlocked (Renewed)"
  - "Apple iPhone 15 Pro, 128GB, Blue Titanium - Unlocked (Renewed)"
```

**结论**: T108 之前 Amazon 30s 超时 (networkidle). 改成 domcontentloaded 后直接主路径成功, 拿到 155 个文本块.

## Test B: Nokia (antibot 触发 → 自动 wayback 兜底)

```
URL: https://www.nokia.com/

Step 1 主路径:
  - antibot 触发:  Akamai bot manager reference id pattern
  - nokia.com 后面挂 Akamai, 返回的 HTML 里含 "Reference #18.a1b2..." 之类
  - 触发后 page reset to about:blank (T104 fix)
  - raise RuntimeError("antibot block: ..."), browse() 主路径失败

Step 2 fallback:
  - archive.org 查 latest snapshot → 找到 20260720161440 (2 天前)
  - wayback URL: https://web.archive.org/web/20260720161440id_/https://www.nokia.com/
  - Playwright navigate 到 wayback URL
  - Snapshot: 50 blocks, 69 links, 12 controls
  - Classified: article (44%)
  - fallback_source=archive.org wayback  (标记在 snapshot.meta)
```

**结论**: 这是 T108 设计的完整流程. antibot → raise → fallback → 兜底拿内容 → caller 收到 BrowseResult + fallback 标记.

## Test C: 自动化回归 (tests/test_t108_fallback.py)

```
4 passed in 8.32s:
  - test_archive_returns_snapshot_for_known_site     PASS
  - test_archive_returns_none_for_unknown_site       PASS
  - test_browse_falls_back_to_wayback_on_primary_failure  PASS
  - test_browse_no_fallback_when_disabled            PASS
```

## 总结

T108 解决的实际问题:

| 之前 | 之后 |
|---|---|
| Amazon 30s networkidle 超时 | domcontentloaded 直接拿 |
| Akamai / Cloudflare / PerimeterX 抗 bot → daemon 返 raise 给 caller | 自动 fallback 到 wayback, caller 拿到内容 + fallback 标记 |
| wayback 流量始终 0 (功能没接) | 真上线, 命中 Nokia 这种被 archive 的站 |

剩下的边界:
- **Amazon 主动 opt-out Wayback** (robots.txt exclusion). 这是 Amazon 自己的选择, 我们也没办法. fallback 会试 → 找不到 → 直接 raise 给 caller
- **DDG HTML / Google cache** 还没接. 后续可加 (但现在这两个 source 质量都不如 wayback, 有空再说)
- **pricing data 拿不到** — Amazon 商品页里有 product 但 price 普遍被 "Sign in to see price" 拦, 即便 wayback 拿到 HTML 也看不到. 这不是 T108 范围, 需要后续 agent 化方案
