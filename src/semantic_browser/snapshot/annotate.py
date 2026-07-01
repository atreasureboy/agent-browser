"""
T10: Annotated screenshot — 在原图上叠加 ref 标签框。

LLM 视觉模型能直接看页面; 但仍要知道每个 ref 对应哪个元素。
本模块接受 PNG 截图 + refs 信息, 在原图上画矩形框 + ref 标签,
返回带标注的 PNG bytes + 元素位置 sidecar JSON (供 LLM 引用)。

颜色编码:
  - 蓝色: link
  - 橙色: button / submit
  - 绿色: textbox / input
  - 紫色: select / dropdown
"""
from __future__ import annotations

import io
from dataclasses import dataclass, asdict
from typing import Any

from PIL import Image, ImageDraw, ImageFont


# 颜色 (RGBA) - 选对比度高的; alpha 0.55 让底层仍可见
KIND_COLORS = {
    "link":      (59, 130, 246, 220),    # 蓝
    "button":    (249, 115, 22, 220),    # 橙
    "submit":    (249, 115, 22, 220),
    "textbox":   (34, 197, 94, 220),     # 绿
    "input":     (34, 197, 94, 220),
    "search":    (34, 197, 94, 220),
    "select":    (168, 85, 247, 220),    # 紫
    "checkbox":  (236, 72, 153, 220),    # 粉
    "radio":     (236, 72, 153, 220),
    "textarea":  (34, 197, 94, 200),     # 绿 (比 textbox 略透明)
    "_default":  (107, 114, 128, 200),   # 灰
}

LABEL_BG = (17, 24, 39, 240)            # 近黑 (RGBA)
LABEL_FG = (255, 255, 255, 255)


@dataclass
class RefBox:
    """单个 ref 元素在截图中的位置 + 标注。"""
    ref: str
    kind: str
    label: str
    bbox: tuple[int, int, int, int]  # left, top, right, bottom
    visible: bool


def _get_font(size: int) -> ImageFont.ImageFont:
    """Load a TrueType font or fall back to default bitmap font."""
    # 优先 DejaVuSans (Linux 通常有)
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate_screenshot(
    png_bytes: bytes,
    refs: list[RefBox],
    *,
    label_offset_y: int = -2,
    min_box_size: int = 4,
) -> tuple[bytes, dict[str, Any]]:
    """在 png_bytes 上叠加 ref 标签, 返回 (annotated_png, sidecar_dict).

    sidecar_dict 含每个 ref 的 bbox, 让 LLM 能精确定位 (e.g. "右上角第 3 个按钮")。
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _get_font(13)

    sidecar: list[dict[str, Any]] = []
    visible_count = 0

    for r in refs:
        l, t, right, bottom = r.bbox
        w = right - l
        h = bottom - t
        if w < min_box_size or h < min_box_size:
            # 跳过 0 像素元素 (off-screen / hidden)
            continue
        # 截断到画布内
        l = max(0, l); t = max(0, t)
        right = min(img.size[0], right)
        bottom = min(img.size[1], bottom)
        color = KIND_COLORS.get(r.kind, KIND_COLORS["_default"])
        # 矩形 (3px 边)
        for offset in range(3):
            draw.rectangle(
                [l - offset, t - offset, right + offset, bottom + offset],
                outline=color,
            )
        # ref 标签: 紧贴左上角 (label_offset_y 让其上移避免遮挡元素)
        tag = r.ref
        text_bbox = draw.textbbox((0, 0), tag, font=font)
        tw, th = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
        pad = 2
        tag_x = l
        tag_y = max(0, t + label_offset_y - th - pad * 2)
        draw.rectangle(
            [tag_x - pad, tag_y - pad, tag_x + tw + pad, tag_y + th + pad],
            fill=LABEL_BG,
        )
        draw.text((tag_x, tag_y), tag, fill=LABEL_FG, font=font)
        sidecar.append({
            "ref": r.ref,
            "kind": r.kind,
            "label": r.label,
            "bbox": [l, t, right, bottom],
        })
        visible_count += 1

    combined = Image.alpha_composite(img, overlay)
    out = io.BytesIO()
    combined.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue(), {
        "image_size": list(img.size),
        "ref_count": len(refs),
        "visible_count": visible_count,
        "refs": sidecar,
    }


async def collect_refs_from_page(
    page,
    snapshot_data: dict | None = None,
) -> list[RefBox]:
    """从 Playwright page 收集所有 ref 元素的 bbox + 类别信息。

    通过 JS 在 DOM 上查 [data-sb-ref], 用 getBoundingClientRect() 取坐标。
    kind 推断: tagName + role + type.
    """
    # 在 page 上跑 JS 拿所有 ref 元素的 rect (async)
    js_result = await page.evaluate("""
() => {
    const out = [];
    document.querySelectorAll('[data-sb-ref]').forEach(el => {
        const r = el.getBoundingClientRect();
        out.push({
            ref: el.getAttribute('data-sb-ref'),
            tag: el.tagName.toLowerCase(),
            type: (el.getAttribute('type') || '').toLowerCase(),
            role: (el.getAttribute('role') || '').toLowerCase(),
            label: (el.textContent || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 80),
            left: Math.round(r.left),
            top: Math.round(r.top),
            right: Math.round(r.right),
            bottom: Math.round(r.bottom),
        });
    });
    return out;
}
""")
    if not js_result:
        return []
    out: list[RefBox] = []
    for entry in js_result:
        kind = _infer_kind(entry["tag"], entry["type"], entry["role"])
        out.append(RefBox(
            ref=entry["ref"],
            kind=kind,
            label=entry["label"],
            bbox=(entry["left"], entry["top"], entry["right"], entry["bottom"]),
            visible=True,
        ))
    return out


def _infer_kind(tag: str, input_type: str, role: str) -> str:
    """HTML 标签 → 我们的 kind 类别 (与 SnapshotEngine 一致)."""
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag == "input":
        if input_type in ("submit", "button"):
            return "submit"
        if input_type == "checkbox":
            return "checkbox"
        if input_type == "radio":
            return "radio"
        if input_type == "search":
            return "search"
        return "textbox"
    if role == "button":
        return "button"
    if role == "link":
        return "link"
    return "_default"


async def annotate_current_page(page, *, viewport_only: bool = True) -> tuple[bytes, dict[str, Any]]:
    """便捷: 截当前页 + 标注。一次调用拿两样。"""
    import asyncio
    png = await page.screenshot(full_page=not viewport_only)
    refs = await collect_refs_from_page(page)
    return annotate_screenshot(png, refs)