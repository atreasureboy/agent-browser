"""
T32: Destructive action guard — agent 不能在用户不知情时执行危险动作.

危险模式:
- type 包含 "delete", "drop", "remove", "truncate", "rm -rf" 等 → 需 confirm
- click button label 含 "delete"/"remove"/"reset"/"submit" 等 → 需 confirm
- drag 到 ref 含 "trash"/"recycle" → 需 confirm

设计: guard 是 pure function (action + args → needs_confirm: bool, reason: str).
不直接拦截 — 留给上层决定怎么 confirm (CLI prompt, daemon 返回 needs_confirm,
agent 看到 reason 后问 LLM 重选 action).

为啥 separate module: 安全策略可能演化 (新关键词, 风险分级), 独立文件易测试.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 危险关键词 (type/click/drag)
_DESTRUCTIVE_KEYWORDS = (
    "delete", "del ", "remove", "drop", "truncate", "destroy",
    "wipe", "purge", "reset", "clear all", "rm -rf",
    "uninstall", "revoke", "terminate",
    "submit", "confirm purchase", "checkout", "pay",
)

# drag 目标危险 ref 模式
_DANGEROUS_REF_PATTERNS = (
    re.compile(r"trash", re.I),
    re.compile(r"recycle", re.I),
    re.compile(r"bin", re.I),
    re.compile(r"danger", re.I),
)


@dataclass
class SafetyCheck:
    """Guard 返回的结果."""
    needs_confirm: bool
    reason: str = ""
    risk_level: str = "safe"  # safe / warning / dangerous


def check_action(action: str, args: dict[str, Any],
                  ref_label: Optional[str] = None) -> SafetyCheck:
    """检查单个 action 是否需要人类 confirm.

    Args:
        action: open/click/type/drag/done/extract_text
        args: action 的 args (e.g. {"ref": "e5", "text": "delete"})
        ref_label: 可选 — ref 对应的 label (从 snapshot 取), 用于更精确判断

    Returns:
        SafetyCheck(needs_confirm, reason, risk_level)
    """
    if action not in ("type", "click", "drag"):
        return SafetyCheck(needs_confirm=False, risk_level="safe")

    # type: 检查 text 内容
    if action == "type":
        text = (args.get("text") or "").lower()
        for kw in _DESTRUCTIVE_KEYWORDS:
            if kw in text:
                # T116 audit fix: 之前静默返 SafetyCheck, 0 audit log.
                # 安全敏感事件必须有 trail — 后面 ops 才审计 "谁何时想
                # 做什么". 注意: 不打 args.get("text") 原文 (可能含
                # password/secret) — 只打 reason + kw 关键词.
                logger.warning(
                    "safety/guard: BLOCKED %s: %s (action=%s, kw=%r, ref=%s)",
                    "type", f"text contains destructive keyword: {kw!r}",
                    action, kw, ref_label,
                )
                return SafetyCheck(
                    needs_confirm=True,
                    reason=f"type() text contains destructive keyword: {kw!r}",
                    risk_level="dangerous",
                )
        return SafetyCheck(needs_confirm=False, risk_level="safe")

    # click: 检查 ref_label (button/control text)
    if action == "click":
        if ref_label is None:
            return SafetyCheck(needs_confirm=False, risk_level="safe")
        label_lower = ref_label.lower()
        for kw in _DESTRUCTIVE_KEYWORDS:
            if kw in label_lower:
                # T116 audit fix: 同 type 块 — 安全 BLOCKED 事件记 log.
                logger.warning(
                    "safety/guard: BLOCKED %s: target label %r contains %r",
                    "click", ref_label, kw,
                )
                return SafetyCheck(
                    needs_confirm=True,
                    reason=f"click() target label {ref_label!r} contains: {kw!r}",
                    risk_level="dangerous",
                )
        return SafetyCheck(needs_confirm=False, risk_level="safe")

    # drag: 检查 to_ref 名称
    if action == "drag":
        to_ref = args.get("to_ref", "")
        for pat in _DANGEROUS_REF_PATTERNS:
            if pat.search(to_ref):
                # T116 audit fix: 同 type/click — 安全 BLOCKED 事件记 log.
                logger.warning(
                    "safety/guard: BLOCKED %s: target ref %r matches %r",
                    "drag", to_ref, pat.pattern,
                )
                return SafetyCheck(
                    needs_confirm=True,
                    reason=f"drag() target ref {to_ref!r} matches dangerous pattern: {pat.pattern}",
                    risk_level="dangerous",
                )
        return SafetyCheck(needs_confirm=False, risk_level="safe")

    return SafetyCheck(needs_confirm=False, risk_level="safe")


def is_destructive(action: str, args: dict[str, Any],
                   ref_label: Optional[str] = None) -> bool:
    """便捷方法: 直接返回 bool."""
    return check_action(action, args, ref_label).needs_confirm