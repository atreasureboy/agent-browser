"""
T25: 失败自动 dump — agent 失败时自动收集诊断信息.

解决之前我作为 agent 提的 #3: "等不到的时候直接说'超时', 没有任何诊断".

提供:
  collect_diagnostics(controller, action, error) → dict
    自动收集:
    - 最近 console.error / console.warn (T18 已有 buffer)
    - 最近 network 失败 (T18 已有 buffer)
    - 最近 JS 异常 (T18 已有 buffer)
    - 当前 URL / title
    - 出错时的 snapshot 摘要
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from semantic_browser.browser.controller import BrowserController
from semantic_browser.snapshot.engine import SnapshotEngine

logger = logging.getLogger(__name__)


async def collect_diagnostics(
    controller: BrowserController,
    *,
    failed_action: str,
    failed_args: dict[str, Any],
    error: str,
    console_limit: int = 10,
    network_limit: int = 10,
    errors_limit: int = 5,
) -> dict[str, Any]:
    """失败时收集诊断信息 — 给 LLM 当 context 看到底发生了什么.

    Args:
        controller: BrowserController
        failed_action: 失败的动作名 (e.g. "click", "type")
        failed_args: 失败动作的参数
        error: 错误消息
        console_limit / network_limit / errors_limit: 各类事件最多取几条

    Returns:
        {
          "failed_action": "click",
          "failed_args": {"ref": "e5"},
          "error": "element not found",
          "page": {"url": "...", "title": "..."},
          "console_errors": [...],     # 仅 error 类型
          "console_warnings": [...],   # 仅 warn 类型
          "network_failures": [...],   # 4xx/5xx/网络失败
          "js_errors": [...],          # 未捕获 JS 异常
          "snapshot_excerpt": "URL:..\nRef e3: ...",  # 失败时的 snapshot
        }
    """
    page = controller.current_page
    page_info: dict[str, Any] = {"url": None, "title": None}
    snapshot_excerpt = ""
    if page is not None:
        try:
            page_info["url"] = page.url
            page_info["title"] = await page.title()
        except Exception as e:
            page_info["error"] = f"could not read page info: {e}"[:200]
        # 抓 snapshot 摘要 (refs 列表, 给 LLM 知道当前能点什么)
        try:
            engine = SnapshotEngine(page)
            snap = await engine.capture(base_url=page.url)
            refs_lines = []
            for c in list(snap.links)[:20]:
                refs_lines.append(f"  - {c.ref} link: {(c.text or c.href or '')[:60]}")
            for c in list(snap.controls)[:20]:
                refs_lines.append(f"  - {c.ref} {c.kind}: {(c.label or c.placeholder or '')[:60]}")
            snapshot_excerpt = (
                f"URL: {snap.url}\nTitle: {snap.title}\n\n"
                f"Current refs ({len(refs_lines)}):\n" + "\n".join(refs_lines)
            )
        except Exception as e:
            snapshot_excerpt = f"(snapshot failed: {e})"

    # console error / warn (T18 buffer)
    console_errors = controller.get_console_messages(type_filter="error", limit=console_limit)
    console_warnings = controller.get_console_messages(type_filter="warn", limit=console_limit)

    # network 失败
    network_failures = controller.get_network_requests(
        only_failed=True, limit=network_limit,
    )

    # JS 异常
    js_errors = controller.get_page_errors(limit=errors_limit)

    return {
        "failed_action": failed_action,
        "failed_args": failed_args,
        "error": error[:500] if error else "",
        "page": page_info,
        "console_errors": console_errors,
        "console_warnings": console_warnings,
        "network_failures": network_failures,
        "js_errors": js_errors,
        "snapshot_excerpt": snapshot_excerpt[:1500],
    }


def format_diagnostics_for_llm(diag: dict[str, Any]) -> str:
    """把 diagnostics dict 序列化成 LLM-friendly 的文本块.

    用于: GoalAgent 失败时, 把这段塞进 next prompt 让 LLM 知道为什么挂.
    """
    lines = [
        f"Last action FAILED: {diag['failed_action']}({diag.get('failed_args', {})})",
        f"Error: {diag['error']}",
        f"Page: {diag['page'].get('url', '?')} — {diag['page'].get('title', '?')}",
        "",
    ]
    if diag.get("console_errors"):
        lines.append(f"Console errors ({len(diag['console_errors'])}):")
        for m in diag["console_errors"][:5]:
            lines.append(f"  - {m.get('text', '')[:200]}")
        lines.append("")
    if diag.get("js_errors"):
        lines.append(f"JS exceptions ({len(diag['js_errors'])}):")
        for e in diag["js_errors"][:5]:
            lines.append(f"  - [{e.get('name', 'Error')}] {e.get('message', '')[:200]}")
        lines.append("")
    if diag.get("network_failures"):
        lines.append(f"Network failures ({len(diag['network_failures'])}):")
        for r in diag["network_failures"][:5]:
            lines.append(
                f"  - {r.get('method', '?')} {r.get('status', '?')} {r.get('url', '')[:100]}"
            )
        lines.append("")
    if diag.get("snapshot_excerpt"):
        lines.append("Current page refs:")
        lines.append(diag["snapshot_excerpt"])
    return "\n".join(lines)