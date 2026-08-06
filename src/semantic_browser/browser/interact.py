"""Interactive browser operations: click, type, drag, hover, frames, and healing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from playwright.async_api import Page

from semantic_browser.browser._utils import _redact_url_secrets

logger = logging.getLogger(__name__)


class _InteractMixin:
    """Click/type/drag/hover/heal-click and frame operations — mixed into BrowserController."""

    async def get_ref_label(self, ref: str) -> str:
        """Best-effort accessible label of a data-sb-ref element.

        Used by the safety guard to judge whether a click target is
        destructive (delete/remove/submit...). Returns "" when the ref
        does not exist or the page is gone — callers must treat "" as
        "unknown", never as "safe by definition".
        """
        try:
            target = await self._active_page_or_frame()
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            label = await locator.evaluate(
                """el => {
                    return el.getAttribute('aria-label')
                        || el.getAttribute('title')
                        || el.getAttribute('alt')
                        || el.getAttribute('placeholder')
                        || el.getAttribute('value')
                        || (el.innerText || '').trim().slice(0, 80)
                        || '';
                }""",
                timeout=3000,
            )
            return (label or "").strip()
        except Exception:
            return ""

    async def click(self, ref: str, human_like: bool = True) -> bool:
        """通过 @ref 点击元素。"""
        import random
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            if human_like:
                try:
                    await locator.hover(timeout=1500)
                    await asyncio.sleep(random.uniform(0.08, 0.22))
                except Exception:
                    pass
            await locator.click(timeout=5000)
            logger.info("Clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Click failed ref=%s: %s", ref, e)
            return False

    async def click_with_healing(self, ref: str, *, heal_attempts: int = 2) -> dict[str, Any]:
        """T22: 带 self-healing 的 click — 失败时自动 retry with:
        1. force=True (绕过遮挡检查)
        2. JS click (绕过 Playwright actionability 检查)
        Returns {"ok": bool, "ref": str, "tried": [str], "error": Optional[str]}.
        """
        target = await self._active_page_or_frame()
        selector = self._ref_to_selector(ref)
        tried: list[str] = []
        last_err = None

        # 第一次: 标准 click
        tried.append("normal")
        try:
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=5000)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        if heal_attempts <= 0:
            return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

        # 第二次: force=True (跳过遮挡检查)
        tried.append("force")
        try:
            locator = target.locator(selector).first
            await locator.click(force=True, timeout=5000)
            logger.info("Healed click with force=True ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        # 第三次: JS click (绕过所有 actionability)
        tried.append("js")
        try:
            ok = await target.evaluate(
                "(sel) => { const el = document.querySelector(sel); "
                "if (el) { el.click(); return true; } return false; }",
                selector,
            )
            if ok:
                logger.info("Healed click via JS ref=%s", ref)
                return {"ok": True, "ref": ref, "tried": tried, "error": None}
            last_err = "JS click: element not found"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

    async def type_text(self, ref: str, text: str, human_like: bool = True) -> bool:
        """通过 @ref 拟人化输入文本。"""
        import random
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            if human_like:
                try:
                    await locator.focus(timeout=2000)
                    await locator.press_sequentially(text, delay=random.randint(40, 90))
                except Exception:
                    await locator.fill(text, timeout=5000)
            else:
                await locator.fill(text, timeout=5000)
            logger.info("Typed into ref=%s (human_like=%s)", ref, human_like)
            return True
        except Exception as e:
            logger.warning("Type failed ref=%s: %s", ref, e)
            return False

    async def humanlike_scroll(self, steps: int = 3, distance: int = 350) -> None:
        """拟人化非等速视口滑动，模仿人类浏览行为，触发懒加载与风控轨迹校验。"""
        import random
        page = await self._ensure_page()
        for _ in range(steps):
            step_dist = distance + random.randint(-40, 60)
            await page.mouse.wheel(0, step_dist)
            await asyncio.sleep(random.uniform(0.2, 0.45))

    async def type_with_healing(self, ref: str, text: str, *, heal_attempts: int = 2) -> dict[str, Any]:
        """T22: 带 self-healing 的 type_text — 失败时自动:
        1. force=True fill
        2. JS set value + dispatch input event (绕过 React 受控组件检查)
        Returns {"ok", "ref", "tried", "error"}.
        """
        target = await self._active_page_or_frame()
        selector = self._ref_to_selector(ref)
        tried: list[str] = []
        last_err = None

        # 第一次: 标准 fill
        tried.append("normal")
        try:
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.fill(text, timeout=5000)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        if heal_attempts <= 0:
            return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

        # 第二次: force=True fill
        tried.append("force")
        try:
            locator = target.locator(selector).first
            await locator.fill(text, force=True, timeout=5000)
            logger.info("Healed fill with force=True ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        # 第三次: JS dispatch input event (绕过 React 受控组件 / 框架拦截)
        tried.append("js")
        try:
            await target.evaluate(
                "([sel, v]) => { const el = document.querySelector(sel); "
                "if (!el) return false; "
                "const setter = Object.getOwnPropertyDescriptor("
                "  window.HTMLInputElement.prototype, 'value').set; "
                "setter.call(el, v); "
                "el.dispatchEvent(new Event('input', { bubbles: true })); "
                "el.dispatchEvent(new Event('change', { bubbles: true })); "
                "return true; }",
                [selector, text],
            )
            logger.info("Healed fill via JS ref=%s", ref)
            return {"ok": True, "ref": ref, "tried": tried, "error": None}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"[:200]

        return {"ok": False, "ref": ref, "tried": tried, "error": last_err}

    async def fill_form(self, fields: dict[str, str]) -> dict[str, bool]:
        """T11: 一次性填多个字段 (人类填表的"批量"动作)。

        Returns {ref: ok} — agent 能立即看出哪些字段没填上, 再针对性 retry。
        """
        out: dict[str, bool] = {}
        for ref, text in fields.items():
            out[ref] = await self.type_text(ref, text)
        return out

    async def hover(self, ref: str) -> bool:
        """T19: 鼠标悬停在 ref 元素上 (触发 hover 状态 / tooltip / 下拉菜单等)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.hover(timeout=5000)
            logger.info("Hovered ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Hover failed ref=%s: %s", ref, e)
            return False

    async def dblclick(self, ref: str) -> bool:
        """T19: 双击元素 (人类编辑文件 / 打开项目的动作)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.dblclick(timeout=5000)
            logger.info("Double-clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Dblclick failed ref=%s: %s", ref, e)
            return False

    async def rightclick(self, ref: str) -> bool:
        """T19: 右键点击元素 (打开 context menu)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.click(button="right", timeout=5000)
            logger.info("Right-clicked ref=%s", ref)
            return True
        except Exception as e:
            logger.warning("Rightclick failed ref=%s: %s", ref, e)
            return False

    async def drag(self, from_ref: str, to_ref: str) -> bool:
        """T19 + T28: 拖拽 from_ref 到 to_ref. 鼠标手势 + HTML5 双策略.

        优先 mouse gesture (兼容 jQuery UI draggable, Sortable.js 老版本);
        失败 fallback 到 HTML5 DataTransfer dispatch (React-dnd / 现代 dnd 库).
        返回 True 表示任意一种方式触发了 drop event.
        """
        target = await self._active_page_or_frame()
        try:
            from_sel = self._ref_to_selector(from_ref)
            to_sel = self._ref_to_selector(to_ref)
            from_loc = target.locator(from_sel).first
            to_loc = target.locator(to_sel).first
            await from_loc.scroll_into_view_if_needed(timeout=5000)
            await to_loc.scroll_into_view_if_needed(timeout=5000)
            from_box = await from_loc.bounding_box()
            to_box = await to_loc.bounding_box()
            if from_box is None or to_box is None:
                raise RuntimeError("element not visible (no bounding box)")
            # 鼠标手势拖 (兼容性最好 — 不依赖 HTML5 drag API)
            sx = from_box["x"] + from_box["width"] / 2
            sy = from_box["y"] + from_box["height"] / 2
            tx = to_box["x"] + to_box["width"] / 2
            ty = to_box["y"] + to_box["height"] / 2
            await target.mouse.move(sx, sy)
            await target.mouse.down()
            # 多步移动 (某些 framework 需要中间步骤才触发 dragenter/dragover)
            await target.mouse.move((sx + tx) / 2, (sy + ty) / 2, steps=10)
            await target.mouse.move(tx, ty, steps=10)
            await target.mouse.up()
            logger.info("Dragged (mouse) ref=%s -> ref=%s", from_ref, to_ref)
            return True
        except Exception as e:
            logger.warning("Mouse drag failed ref=%s->%s: %s; trying HTML5", from_ref, to_ref, e)
            return await self.drag_html5(from_ref, to_ref)

    async def drag_html5(self, from_ref: str, to_ref: str) -> bool:
        """T28: HTML5 drag-and-drop via DataTransfer + dispatchEvent.

        解决 React-dnd / 现代 dnd 库对 mouse gesture 无响应的问题.
        通过共享 DataTransfer 对象构造 dragstart → dragover → drop 序列.
        """
        target = await self._active_page_or_frame()
        try:
            from_sel = self._ref_to_selector(from_ref)
            to_sel = self._ref_to_selector(to_ref)
            # 在 page 上跑一段脚本: 用共享 DataTransfer 派发 dragstart/dragenter/dragover/drop
            ok = await target.evaluate(
                """([fromSel, toSel]) => {
                    const from = document.querySelector(fromSel);
                    const to = document.querySelector(toSel);
                    if (!from || !to) return {ok: false, error: 'element not found'};
                    const dt = new DataTransfer();
                    const fire = (el, type) => {
                        const r = el.getBoundingClientRect();
                        const ev = new DragEvent(type, {
                            bubbles: true, cancelable: true,
                            dataTransfer: dt,
                            clientX: r.left + r.width / 2,
                            clientY: r.top + r.height / 2,
                        });
                        el.dispatchEvent(ev);
                        return ev;
                    };
                    fire(from, 'dragstart');
                    fire(to, 'dragenter');
                    fire(to, 'dragover');
                    fire(to, 'drop');
                    fire(from, 'dragend');
                    return {ok: true};
                }""",
                [from_sel, to_sel],
            )
            if isinstance(ok, dict) and ok.get("ok"):
                logger.info("Dragged (html5) ref=%s -> ref=%s", from_ref, to_ref)
                return True
            err = ok.get("error") if isinstance(ok, dict) else "unknown"
            logger.warning("HTML5 drag failed ref=%s->%s: %s", from_ref, to_ref, err)
            return False
        except Exception as e:
            logger.warning("HTML5 drag exception ref=%s->%s: %s", from_ref, to_ref, e)
            return False

    async def select_option(self, ref: str, value: str | list[str]) -> bool:
        """T19: 在 <select> ref 上选 value. 接受单值或 list (multi-select).

        value 可以是 option 的 value / label / index (Playwright 支持).
        """
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.select_option(value, timeout=5000)
            n = len(value) if isinstance(value, list) else 1
            logger.info("Selected %d option(s) on ref=%s", n, ref)
            return True
        except Exception as e:
            logger.warning("Select failed ref=%s: %s", ref, e)
            return False

    async def press_key(self, key: str) -> None:
        """按键，如 Enter, Tab, Escape。"""
        page = await self._ensure_page()
        await page.keyboard.press(key)

    async def set_files(self, ref: str, paths: list[str]) -> dict[str, Any]:
        """T13: 通过 ref 给 file input 设置文件路径 (人类"上传附件"动作).

        Returns {"ok": bool, "ref": str, "file_count": int, "error": Optional[str]}.
        """
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            locator = target.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.set_input_files(paths, timeout=10000)
            logger.info("Set files ref=%s: %d files", ref, len(paths))
            return {"ok": True, "ref": ref, "file_count": len(paths), "error": None}
        except Exception as e:
            logger.warning("set_files failed ref=%s: %s", ref, e)
            return {"ok": False, "ref": ref, "file_count": 0, "error": str(e)[:200]}

    async def download_file(
        self,
        trigger_ref: str | None = None,
        *,
        save_to: str | None = None,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """T14: 触发下载并保存文件。

        用法 1 — 知道 ref: `download_file(trigger_ref='e5', save_to='/tmp/file.zip')`
        用法 2 — 已点击外部触发器 (e.g. agent 已 click): `download_file(save_to='/tmp/x')`
                等下一个下载事件 (适用罕见场景).

        Returns {"ok", "path", "size", "suggested_filename", "url"}.
        """
        page = await self._ensure_page()
        import os as _os

        async def _do_download():
            if trigger_ref:
                # 边 click 边捕获 download 事件
                async with page.expect_download(timeout=timeout_ms) as dl_info:
                    ok = await self.click(trigger_ref)
                    if not ok:
                        raise RuntimeError(f"click {trigger_ref} failed")
                download = await dl_info.value
            else:
                # 等待下一个 download 事件 (调用前已 click 过了)
                download = await page.expect_download(timeout=timeout_ms).__aenter__()
            suggested = download.suggested_filename
            target = save_to or _os.path.join("/tmp", suggested or "download.bin")
            await download.save_as(target)
            return download, target, suggested

        try:
            download, target, suggested = await _do_download()
            size = _os.path.getsize(target) if _os.path.exists(target) else 0
            return {
                "ok": True,
                "path": target,
                "size": size,
                "suggested_filename": suggested,
                "url": download.url,
            }
        except Exception as e:
            return {
                "ok": False,
                "path": None,
                "size": 0,
                "suggested_filename": None,
                "url": None,
                "error": f"{type(e).__name__}: {e}"[:200],
            }

    async def get_focused_element(self) -> dict[str, Any]:
        """T16: 返回当前 active element 的描述.

        Returns {"tag", "type", "ref", "text", "value", "href"} 或 {} 若无焦点.
        用 :focus + [data-sb-ref] 查 ref.
        """
        target = await self._active_page_or_frame()
        info = await target.evaluate("""
            () => {
                const el = document.activeElement;
                if (!el || el === document.body) return null;
                const out = {
                    tag: el.tagName.toLowerCase(),
                    type: (el.getAttribute('type') || '').toLowerCase(),
                    ref: el.getAttribute('data-sb-ref') || null,
                    text: (el.textContent || '').trim().slice(0, 80),
                    value: el.value !== undefined ? String(el.value).slice(0, 200) : null,
                    href: el.href || null,
                    placeholder: el.placeholder || null,
                    aria_label: el.getAttribute('aria-label') || null,
                };
                return out;
            }
        """)
        return info or {}

    async def focus(self, ref: str) -> bool:
        """T16: 把焦点设到 ref 元素上 (无需 click)."""
        target = await self._active_page_or_frame()
        try:
            selector = self._ref_to_selector(ref)
            await target.locator(selector).first.focus(timeout=5000)
            return True
        except Exception as e:
            logger.warning("Focus failed ref=%s: %s", ref, e)
            return False

    async def tab(self, shift: bool = False, count: int = 1) -> str | None:
        """T16: 按 Tab N 次. shift=True = Shift+Tab (反方向).

        返回最后焦点元素的 ref (若有), 便于 agent 接着操作.
        """
        target = await self._active_page_or_frame()
        for _ in range(count):
            key = "Shift+Tab" if shift else "Tab"
            await target.keyboard.press(key)
            await asyncio.sleep(0.05)
        # 看现在焦点在哪儿
        info = await self.get_focused_element()
        return info.get("ref") if isinstance(info, dict) else None

    async def keyboard_shortcut(self, *keys: str) -> None:
        """T16: 键盘组合键. 用法: keyboard_shortcut('Control', 'a') (全选).
        或者 keyboard_shortcut('F5') (单键也支持).
        """
        target = await self._active_page_or_frame()
        if len(keys) == 1:
            await target.keyboard.press(keys[0])
        else:
            await target.keyboard.press("+".join(keys))

    async def type_into_active(self, text: str, delay_ms: int = 0) -> bool:
        """T16: 直接往当前焦点元素打字 (不需要 ref). 模拟人类"键入"动作.

        delay_ms > 0 时模拟真实键入速度 (避免某些 framework 拦截过快键入).
        """
        target = await self._active_page_or_frame()
        try:
            if delay_ms > 0:
                await target.keyboard.type(text, delay=delay_ms)
            else:
                await target.keyboard.type(text)
            return True
        except Exception as e:
            logger.warning("type_into_active failed: %s", e)
            return False

    # ── T15: Frame (iframe) 支持 ─────────────────────────────

    @property
    def active_frame(self) -> Optional[Page]:
        """当前活跃的 frame (page 或 frame_page); 默认 = current_page."""
        return self._page  # 初始 = 顶层 page

    async def list_frames(self) -> list[dict[str, Any]]:
        """T15/T40e: 列出所有 frame (顶层 + 所有 iframe) — 含结构信息.

        每个 frame 报告:
          - name, url, is_main
          - depth: 嵌套深度 (顶层 = 0)
          - parent: 父 frame 的 name (顶层 = None)
          - is_cross_origin: 与顶层不同源 (可能受 CORS 限制, agent 拿不到内部 DOM)
          - child_count: 直接子 frame 数

        Returns [
          {"name": "main", "url": "...", "is_main": True, "depth": 0,
           "parent": None, "is_cross_origin": False, "child_count": N},
          {"name": "frame[foo]", "url": "...", "is_main": False, "depth": 1,
           "parent": "main", "is_cross_origin": bool, "child_count": M},
          ...
        ]
        """
        from urllib.parse import urlparse
        page = await self._ensure_page()
        origin_top = urlparse(page.url).netloc
        # 先建一个 name → frame 的索引, 同时递归算 child_count + depth
        frames = [f for f in page.frames]

        def _parent_of(f):
            return f.parent_frame if f.parent_frame in frames else None

        def _children_of(f):
            return [c for c in frames if _parent_of(c) is f]

        out: list[dict[str, Any]] = []
        # 主 frame
        out.append({
            "name": "main",
            "url": page.url,
            "is_main": True,
            "depth": 0,
            "parent": None,
            "is_cross_origin": False,
            "child_count": len(_children_of(page.main_frame)),
        })
        # BFS 算 depth
        visited: set[int] = {id(page.main_frame)}
        queue: list[tuple[Any, int]] = [(page.main_frame, 0)]
        # index by id, 用于 child lookup
        id_to_frame = {id(f): f for f in frames}
        id_to_frame[id(page.main_frame)] = page.main_frame
        while queue:
            cur, depth = queue.pop(0)
            for child in _children_of(cur):
                if id(child) in visited:
                    continue
                visited.add(id(child))
                queue.append((child, depth + 1))
                try:
                    child_origin = urlparse(child.url).netloc
                    is_cross = child_origin != origin_top
                except Exception:
                    is_cross = True
                out.append({
                    "name": f"frame[{child.name or '(unnamed)'}]",
                    "url": child.url,
                    "is_main": False,
                    "depth": depth + 1,
                    "parent": "main" if cur is page.main_frame else f"frame[{cur.name or '(unnamed)'}]",
                    "is_cross_origin": is_cross,
                    "child_count": len(_children_of(child)),
                })
        return out

    async def switch_frame(self, name_or_url: str) -> dict[str, Any]:
        """T15: 切换活跃 frame (按 name substring 或 url substring 匹配).

        设置 _frame 后, 所有 click/type/snapshot/wait 都作用在该 frame 上。
        Returns {"name", "url"} or raises ValueError if not found.
        """
        page = await self._ensure_page()
        # 主 frame 用特殊 key
        if name_or_url in ("main", "top"):
            self._frame = None
            return {"name": "main", "url": page.url}
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if (frame.name and name_or_url in frame.name) or name_or_url in frame.url:
                self._frame = frame
                logger.info("Switched to frame: %s (%s)", frame.name, _redact_url_secrets(frame.url))
                return {"name": frame.name, "url": frame.url}
        raise ValueError(f"frame not found: {name_or_url!r}; try one of {[f['name'] for f in await self.list_frames()]}")

    async def to_top_frame(self) -> None:
        """T15: 回到顶层 frame."""
        self._frame = None

    async def _active_page_or_frame(self) -> Any:
        """返回当前活跃 page (或 frame 替身). Frame 也实现了 page-like 接口
        (locator, click, fill, set_input_files, screenshot 等),
        所以 click/type/snapshot/wait 等操作都可以路由到 frame.

        若 frame 已设, 直接返回 frame (避免无谓 page 初始化).
        """
        if self._frame is not None:
            return self._frame
        return await self._ensure_page()
