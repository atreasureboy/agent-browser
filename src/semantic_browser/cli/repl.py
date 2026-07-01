"""
Interactive REPL — `sb interactive <url>` 后的命令循环。

设计目标:
- 解析层和执行层分离。parse_command 是纯函数，可测。
- 会话复用 BrowserController + SnapshotEngine + ContentExtractor；不开新引擎。
- 富文本输出用 rich.console，但 console 实例可注入，便于测试时静音。
- 错误恢复：单条命令失败不退出 REPL。

支持的命令 (大小写不敏感):
    open <url>            打开 URL
    snapshot              输出当前页语义快照 (rich 表格)
    links                 列出可点击链接
    controls              列出可交互控件
    text [N]              列出文本块 (默认前 10 个)
    click <ref>           通过 ref 点击元素
    type <ref> <text...>  通过 ref 输入文本
    extract [--markdown]  提取并显示正文
    aria                  输出 Playwright aria snapshot
    url                   当前 URL
    title                 当前标题
    back / forward / reload
    scroll [up|down] [amount]
    inspect <ref>         看单个 ref 元素的属性
    note <text...>        给当前页加一条笔记
    refs                  列出当前 session 内见过的所有 ref (去重)
    help                  显示帮助
    quit / exit / q       退出 REPL
"""
from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from semantic_browser.browser.controller import BrowserController
from semantic_browser.classifier.heuristic import PageClassifier
from semantic_browser.extractor.content import ContentExtractor
from semantic_browser.snapshot.engine import (
    SnapshotEngine,
    PageSnapshot,
)


# ── 命令模型 ──────────────────────────────────────────────────

@dataclass
class Command:
    """解析后的一条 REPL 指令。"""
    name: str
    args: list[str] = field(default_factory=list)
    raw: str = ""

    def arg(self, idx: int, default: str = "") -> str:
        return self.args[idx] if idx < len(self.args) else default


class ParseError(Exception):
    """命令语法错误。"""


# ── 解析器 (纯函数) ───────────────────────────────────────────

HELP_TEXT = """\
[bold cyan]Semantic Browser REPL — 帮助[/bold cyan]

[bold]导航[/bold]
  open <url>              打开 URL
  back / forward / reload 浏览器导航
  url / title             显示当前 URL / 标题
  scroll [up|down] [n]    滚动 (默认 down 500)
  tabs / new [url] / switch <N> / close [N]
                          多 tab 管理 (人类浏览器的核心能力)
  frames / frame <name> / to-top
                          iframe 切换 (现代网页大量嵌入)
  wait text|ref|url X [ms]
                          智能等待 (默认 10s); 比 sleep 稳

[bold]查看[/bold]
  snapshot                语义快照表格
  links                   链接列表
  controls                控件列表
  text [N] [LEN]          文本块 (默认 10 个, 每块默认预览 120 字符; LEN 可改)
  aria                    Playwright aria 快照
  inspect <ref>           看某个 ref 的元素信息
  refs                    列出当前 session 内见过的所有 ref

[bold]操作[/bold]
  click <ref>             点击元素 (ref 接受 3 / e3 / @e3)
  type <ref> <text...>    在元素中输入文本 (用引号包含空格)
  extract [--markdown]    提取正文

[bold]记忆[/bold]
  note <text...>          给当前页加一条笔记 (持久化到 MemoryStore)
  notes                  查看当前页的所有笔记

[bold]其他[/bold]
  help                    显示本帮助
  quit / exit / q         退出
"""


def parse_command(line: str) -> Command:
    """把一行输入解析为 Command。空行返回 None-safe 哨兵 (Command('empty'))。

    引号内的空格保留为一个参数: `type e1 hello world` 会被 shlex 解析为 ['e1', 'hello world']。
    """
    line = line.strip()
    if not line:
        return Command(name="empty", raw=line)

    # shlex 保留引号
    try:
        parts = shlex.split(line)
    except ValueError as e:
        raise ParseError(f"引号未闭合: {e}") from e

    if not parts:
        return Command(name="empty", raw=line)

    name = parts[0].lower()
    if name in ("quit", "exit", "q"):
        name = "quit"
    elif name in ("h", "?"):
        name = "help"
    return Command(name=name, args=parts[1:], raw=line)


# ── 会话执行层 ───────────────────────────────────────────────

@dataclass
class CommandResult:
    """一条命令的输出结果。文本 / 表格 / 错误任一。"""
    text: str = ""
    table: Optional[Table] = None
    panel: Optional[Panel] = None
    clear: bool = False  # quit 用
    error: bool = False


class REPLSession:
    """维护一个浏览器 + snapshot engine + extractor 的会话，跨命令复用。"""

    def __init__(
        self,
        controller: BrowserController,
        console: Optional[Console] = None,
        classifier: Optional[PageClassifier] = None,
        memory_store: Optional[Any] = None,
    ) -> None:
        self.controller = controller
        self.console = console or Console()
        self.classifier = classifier or PageClassifier()
        self.memory_store = memory_store  # 笔记持久化用; 注入便于测试
        self._snapshot_engine: Optional[SnapshotEngine] = None
        self._extractor: Optional[ContentExtractor] = None
        self._snapshot: Optional[PageSnapshot] = None
        self._seen_refs: set[str] = set()
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return
        await self.controller.start()
        self._started = True

    async def close(self) -> None:
        if self._started:
            await self.controller.close()
            self._started = False

    # ── 主入口 ──────────────────────────────────────────────

    async def run_loop(self) -> None:
        """阻塞式读取 stdin 直到 quit / EOF。"""
        await self._ensure_started()
        self.console.print(
            "[dim]输入 help 查看命令；quit 退出[/dim]"
        )
        while True:
            try:
                line = await asyncio.to_thread(input, "sb> ")
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return
            line = line.strip()
            if not line:
                continue
            try:
                cmd = parse_command(line)
            except ParseError as e:
                self.console.print(f"[red]语法错误:[/red] {e}")
                continue
            try:
                result = await self.execute(cmd)
            except Exception as e:  # noqa: BLE001 - REPL 不应因单条命令崩
                self.console.print(f"[red]命令执行失败:[/red] {type(e).__name__}: {e}")
                continue
            self._render(result)
            if result.clear:
                return

    def _render(self, result: CommandResult) -> None:
        if result.table is not None:
            self.console.print(result.table)
        if result.panel is not None:
            self.console.print(result.panel)
        if result.text:
            style = "red" if result.error else None
            self.console.print(result.text, style=style)

    # ── 调度 ────────────────────────────────────────────────

    async def execute(self, cmd: Command) -> CommandResult:
        """执行一条命令，返回结果对象。便于测试时直接调用。"""
        handler = getattr(self, f"_cmd_{cmd.name}", None)
        if handler is None:
            return CommandResult(
                text=f"未知命令: {cmd.name} (输入 help 查看可用命令)",
                error=True,
            )
        return await handler(cmd)

    # ── 内置命令实现 ────────────────────────────────────────

    async def _cmd_help(self, _cmd: Command) -> CommandResult:
        return CommandResult(panel=Panel(HELP_TEXT, border_style="cyan", title="帮助"))

    async def _cmd_quit(self, _cmd: Command) -> CommandResult:
        return CommandResult(text="👋 Bye.", clear=True)

    async def _cmd_empty(self, _cmd: Command) -> CommandResult:
        return CommandResult()  # no-op

    async def _cmd_open(self, cmd: Command) -> CommandResult:
        url = cmd.arg(0)
        if not url:
            return CommandResult(text="用法: open <url>", error=True)
        await self._ensure_started()
        await self.controller.open(url)
        snap = await self._refresh_snapshot()
        self._seen_refs.update(l.ref for l in snap.links)
        self._seen_refs.update(c.ref for c in snap.controls)
        cls = self.classifier.classify(snap)
        # URL 太长会换行/截断难看；显示层压缩到 60 字符 (保留协议头和末尾 path)
        url_disp = snap.url if len(snap.url) <= 60 else snap.url[:30] + "…" + snap.url[-27:]
        return CommandResult(panel=Panel(
            f"[bold]{snap.title or '(无标题)'}[/bold]\n"
            f"URL: {url_disp}\n"
            f"Type: [cyan]{cls.page_type}[/cyan] ({cls.confidence:.0%})\n"
            f"Blocks: {len(snap.text_blocks)}  Links: {len(snap.links)}  "
            f"Controls: {len(snap.controls)}",
            title="📍 已打开",
            border_style="green",
        ))

    async def _cmd_snapshot(self, _cmd: Command) -> CommandResult:
        snap = await self._refresh_snapshot()
        cls = self.classifier.classify(snap)
        table = Table(title="📋 Snapshot", show_header=True, header_style="bold cyan")
        table.add_column("Metric", style="dim")
        table.add_column("Value", style="bold")
        table.add_row("URL", snap.url)
        table.add_row("Title", snap.title or "(无标题)")
        table.add_row("Type", f"{cls.page_type} ({cls.confidence:.0%})")
        table.add_row("Blocks", str(len(snap.text_blocks)))
        table.add_row("Links", str(len(snap.links)))
        table.add_row("Controls", str(len(snap.controls)))
        return CommandResult(table=table)

    async def _cmd_links(self, cmd: Command) -> CommandResult:
        snap = await self._refresh_snapshot()
        limit = int(cmd.arg(0, "20"))
        table = Table(title=f"🔗 Links (showing {min(limit, len(snap.links))}/{len(snap.links)})",
                      show_header=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Ref", style="cyan", width=6)
        table.add_column("Text", width=50)
        table.add_column("Href", style="dim")
        table.add_column("Int", justify="center", width=4)
        for i, link in enumerate(snap.links[:limit], 1):
            table.add_row(
                str(i), link.ref,
                (link.text or "(no text)")[:50],
                link.href[:60],
                "✓" if link.internal else "✗",
            )
        return CommandResult(table=table)

    async def _cmd_controls(self, cmd: Command) -> CommandResult:
        snap = await self._refresh_snapshot()
        limit = int(cmd.arg(0, "20"))
        table = Table(title=f"🎛️ Controls ({min(limit, len(snap.controls))}/{len(snap.controls)})",
                      show_header=True)
        table.add_column("Ref", style="cyan", width=6)
        table.add_column("Kind", style="yellow", width=12)
        table.add_column("Label", width=40)
        table.add_column("Placeholder", style="dim", width=20)
        for c in snap.controls[:limit]:
            table.add_row(c.ref, c.kind, c.label[:40], c.placeholder[:20])
        return CommandResult(table=table)

    async def _cmd_text(self, cmd: Command) -> CommandResult:
        snap = await self._refresh_snapshot()
        limit = int(cmd.arg(0, "10"))
        if len(cmd.args) >= 2:
            preview_len = int(cmd.args[1])
            if preview_len < 0:
                preview_len = 0  # 0 = 显示完整文本
            show_preview_in_title = True
        else:
            preview_len = 120
            show_preview_in_title = False
        lines = []
        for b in snap.text_blocks[:limit]:
            tag = f"[bold cyan]{b.tag}[/bold cyan]" if b.tag.startswith("h") else f"[dim]{b.tag}[/dim]"
            text = b.text
            if preview_len == 0 or len(text) <= preview_len:
                preview = text
            else:
                preview = text[:preview_len] + "…"
            lines.append(f"{tag}  {preview}")
        body = "\n".join(lines) if lines else "(无文本块)"
        suffix = f" (preview={preview_len})" if show_preview_in_title and preview_len > 0 else ""
        return CommandResult(panel=Panel(body, title=f"📝 Text Blocks ({min(limit, len(snap.text_blocks))}/{len(snap.text_blocks)}){suffix}",
                                          border_style="blue"))

    async def _cmd_click(self, cmd: Command) -> CommandResult:
        ref = cmd.arg(0)
        if not ref:
            return CommandResult(text="用法: click <ref> (例如 click e3 或 click 3)", error=True)
        await self._ensure_started()
        ok = await self.controller.click(ref)
        if not ok:
            return CommandResult(text=f"✗ 点击失败: ref={ref} (可能元素不可见或 ref 已失效)", error=True)
        await self.controller.wait(1.0)
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"✓ 已点击 {ref}\n现在: {snap.url}")

    async def _cmd_type(self, cmd: Command) -> CommandResult:
        if len(cmd.args) < 2:
            return CommandResult(text='用法: type <ref> <text...> (文本含空格请加引号，例如: type e1 "hello world")', error=True)
        ref, text = cmd.args[0], " ".join(cmd.args[1:])
        await self._ensure_started()
        ok = await self.controller.type_text(ref, text)
        if not ok:
            return CommandResult(text=f"✗ 输入失败: ref={ref}", error=True)
        return CommandResult(text=f"✓ 已在 {ref} 输入 {len(text)} 字符")

    async def _cmd_extract(self, cmd: Command) -> CommandResult:
        await self._ensure_started()
        page = self.controller.current_page
        if page is None:
            return CommandResult(text="没有当前页面，请先 open <url>", error=True)
        extractor = ContentExtractor(page)
        article = await extractor.extract_article()
        use_md = "--markdown" in cmd.args
        if use_md:
            return CommandResult(panel=Panel(article.to_markdown() or "(空)", title="📄 Article (Markdown)", border_style="green"))
        # 否则用紧凑格式
        lines = [f"[bold]{article.title or '(无标题)'}[/bold]"]
        if article.author:
            lines.append(f"Author: {article.author}")
        if article.publish_date:
            lines.append(f"Date: {article.publish_date}")
        lines.append(f"Sections: {len(article.sections)}  Chars: {article.word_count}  "
                     f"Confidence: {article.extraction_confidence:.0%}")
        for s in article.sections[:3]:
            if s.get("heading"):
                lines.append(f"\n## {s['heading']}")
            for p in s.get("paragraphs", [])[:2]:
                lines.append(f"  {p[:200]}{'…' if len(p) > 200 else ''}")
        return CommandResult(panel=Panel("\n".join(lines), title="📄 Article", border_style="green"))

    async def _cmd_aria(self, _cmd: Command) -> CommandResult:
        await self._ensure_started()
        aria = await self.controller.get_aria_snapshot()
        return CommandResult(panel=Panel(aria or "(无 aria snapshot)", title="🌳 Aria Tree", border_style="magenta"))

    async def _cmd_url(self, _cmd: Command) -> CommandResult:
        url = await self.controller.get_url()
        return CommandResult(text=url)

    async def _cmd_title(self, _cmd: Command) -> CommandResult:
        title = await self.controller.get_title()
        return CommandResult(text=title)

    async def _cmd_back(self, _cmd: Command) -> CommandResult:
        await self.controller.back()
        await self.controller.wait(0.8)
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"← {snap.url}")

    async def _cmd_forward(self, _cmd: Command) -> CommandResult:
        await self.controller.forward()
        await self.controller.wait(0.8)
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"→ {snap.url}")

    async def _cmd_reload(self, _cmd: Command) -> CommandResult:
        await self.controller.reload()
        await self.controller.wait(0.8)
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"🔄 {snap.url}")

    async def _cmd_scroll(self, cmd: Command) -> CommandResult:
        direction = cmd.arg(0, "down").lower()
        amount = int(cmd.arg(1, "500"))
        if direction not in ("up", "down"):
            return CommandResult(text="direction 必须是 up 或 down", error=True)
        await self.controller.scroll(direction, amount)
        return CommandResult(text=f"↕️  scroll {direction} {amount}px")

    async def _cmd_wait(self, cmd: Command) -> CommandResult:
        """`wait text|ref|url <target> [timeout_ms]` — 智能等待。"""
        if len(cmd.args) < 2:
            return CommandResult(text="用法: wait text|ref|url <target> [timeout_ms]", error=True)
        kind = cmd.args[0].lower()
        target = cmd.args[1]
        timeout_ms = int(cmd.args[2]) if len(cmd.args) >= 3 else 10000
        try:
            if kind == "text":
                ok = await self.controller.wait_for_text(target, timeout_ms=timeout_ms)
            elif kind == "ref":
                ok = await self.controller.wait_for_ref(target, timeout_ms=timeout_ms)
            elif kind == "url":
                ok = await self.controller.wait_for_url(target, timeout_ms=timeout_ms)
            else:
                return CommandResult(text=f"wait 类型必须是 text|ref|url, got {kind!r}", error=True)
        except ValueError as e:
            return CommandResult(text=f"✗ {e}", error=True)
        if ok:
            return CommandResult(text=f"✓ {kind} {target!r} 已出现 (≤ {timeout_ms}ms)")
        return CommandResult(text=f"⏱  {kind} {target!r} {timeout_ms}ms 内未出现", error=True)

    async def _cmd_inspect(self, cmd: Command) -> CommandResult:
        ref = cmd.arg(0)
        if not ref:
            return CommandResult(text="用法: inspect <ref>", error=True)
        snap = await self._refresh_snapshot()
        for c in snap.controls:
            if c.ref == ref:
                return CommandResult(panel=Panel(
                    f"ref:       {c.ref}\nkind:      {c.kind}\n"
                    f"label:     {c.label}\nplaceholder:{c.placeholder}\n"
                    f"role:      {c.role}",
                    title=f"🔍 Control {c.ref}", border_style="yellow"))
        for l in snap.links:
            if l.ref == ref:
                return CommandResult(panel=Panel(
                    f"ref:   {l.ref}\ntext:  {l.text}\nhref:  {l.href}\n"
                    f"internal: {l.internal}",
                    title=f"🔍 Link {l.ref}", border_style="yellow"))
        return CommandResult(text=f"ref {ref} 在当前快照中找不到 (可能页面已变)", error=True)

    async def _cmd_refs(self, _cmd: Command) -> CommandResult:
        snap = await self._refresh_snapshot()
        current = {l.ref for l in snap.links} | {c.ref for c in snap.controls}
        all_refs = sorted(self._seen_refs | current, key=lambda r: int(r[1:]))
        if not all_refs:
            return CommandResult(text="(session 内还没见过 ref，先 open 一个页面)")
        return CommandResult(panel=Panel(
            " ".join(all_refs),
            title=f"🏷️ Refs ({len(all_refs)})",
            border_style="cyan",
        ))

    async def _cmd_tabs(self, _cmd: Command) -> CommandResult:
        """列出所有 tab (active 加 * 号)。"""
        tabs = self.controller.list_tabs()
        if not tabs:
            return CommandResult(text="(no tabs)")
        lines = []
        for t in tabs:
            marker = "*" if t["active"] else " "
            url_disp = t["url"] if len(t["url"]) <= 60 else t["url"][:30] + "…" + t["url"][-27:]
            lines.append(f"{marker} [{t['index']}] {url_disp}")
        return CommandResult(panel=Panel("\n".join(lines),
                                          title=f"📑 Tabs ({len(tabs)})",
                                          border_style="cyan"))

    async def _cmd_new(self, cmd: Command) -> CommandResult:
        """打开新 tab。`new <url>` 或 `new` = blank tab."""
        url = cmd.arg(0, "")
        await self.controller.new_tab(url)
        # 新 tab 自动激活, 拉一次 snapshot 让用户立刻看到内容
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"✓ 新 tab: {snap.url}")

    async def _cmd_switch(self, cmd: Command) -> CommandResult:
        idx = int(cmd.arg(0, "-1"))
        try:
            await self.controller.switch_tab(idx)
        except ValueError as e:
            return CommandResult(text=f"✗ {e}", error=True)
        snap = await self._refresh_snapshot()
        return CommandResult(text=f"→ tab {idx}: {snap.url}")

    async def _cmd_close_tab(self, cmd: Command) -> CommandResult:
        idx = int(cmd.arg(0)) if cmd.arg(0) else None
        try:
            remaining = await self.controller.close_tab(idx)
        except ValueError as e:
            return CommandResult(text=f"✗ {e}", error=True)
        return CommandResult(text=f"✗ 关闭 tab {idx if idx is not None else 'current'}; 剩余 {remaining}")

    async def _cmd_frames(self, _cmd: Command) -> CommandResult:
        """列出当前页的所有 frame (顶层 + iframe)."""
        frames = await self.controller.list_frames()
        if not frames:
            return CommandResult(text="(no frames)")
        lines = []
        for f in frames:
            marker = "*" if f["is_main"] else " "
            url_disp = f["url"] if len(f["url"]) <= 60 else f["url"][:30] + "…" + f["url"][-27:]
            lines.append(f"{marker} {f['name']:24s} {url_disp}")
        return CommandResult(panel=Panel("\n".join(lines),
                                          title=f"🪟 Frames ({len(frames)})",
                                          border_style="cyan"))

    async def _cmd_frame(self, cmd: Command) -> CommandResult:
        """`frame <name_or_url>` — 切到该 frame (后续 click/type 走它)."""
        target = cmd.arg(0)
        if not target:
            return CommandResult(text="用法: frame <name_or_url>  (或 'main'/'top' 回顶层)", error=True)
        try:
            result = await self.controller.switch_frame(target)
        except ValueError as e:
            return CommandResult(text=f"✗ {e}", error=True)
        return CommandResult(text=f"→ frame {result['name']}: {result['url']}")

    async def _cmd_to_top(self, _cmd: Command) -> CommandResult:
        """回到顶层 frame (main)."""
        await self.controller.to_top_frame()
        return CommandResult(text="→ top frame")

    async def _cmd_note(self, cmd: Command) -> CommandResult:
        if not cmd.args:
            return CommandResult(text="用法: note <text...>", error=True)
        url = await self.controller.get_url()
        text = " ".join(cmd.args)
        if self.memory_store is None:
            # 没注入 store 就建一个默认的 (单 DB 在 ~/.semantic-browser/memory.db)
            try:
                from semantic_browser.memory.store import MemoryStore
                from pathlib import Path as _P
                self.memory_store = MemoryStore(_P.home() / ".semantic-browser" / "memory.db")
            except Exception as e:
                return CommandResult(text=f"⚠ 笔记持久化失败 (无法初始化 MemoryStore): {e}", error=True)
        try:
            self.memory_store.add_note(url, text)
        except Exception as e:
            return CommandResult(text=f"⚠ 笔记保存失败: {e}", error=True)
        return CommandResult(text=f"📝 已保存 [{url}] {text}")

    async def _cmd_notes(self, _cmd: Command) -> CommandResult:
        """列出当前 URL 的所有笔记 (倒序)。"""
        url = await self.controller.get_url()
        if self.memory_store is None:
            try:
                from semantic_browser.memory.store import MemoryStore
                from pathlib import Path as _P
                self.memory_store = MemoryStore(_P.home() / ".semantic-browser" / "memory.db")
            except Exception as e:
                return CommandResult(text=f"⚠ 无法读取笔记: {e}", error=True)
        notes = self.memory_store.get_notes(url)
        if not notes:
            return CommandResult(text=f"当前页面没有笔记: {url}")
        lines = [f"[bold]{url}[/bold]  ({len(notes)} 条)"]
        for n in notes[:10]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(n["created_at"]))
            lines.append(f"  [{ts}] {n['note']}")
        return CommandResult(panel=Panel("\n".join(lines), title="📝 Notes", border_style="yellow"))

    # ── 内部 ────────────────────────────────────────────────

    async def _refresh_snapshot(self) -> PageSnapshot:
        page = self.controller.current_page
        if page is None:
            raise RuntimeError("没有打开的页面，请先 `open <url>`")
        if self._snapshot_engine is None or self._snapshot_engine.page is not page:
            self._snapshot_engine = SnapshotEngine(page)
        self._snapshot = await self._snapshot_engine.capture(base_url=page.url)
        self._seen_refs.update(l.ref for l in self._snapshot.links)
        self._seen_refs.update(c.ref for c in self._snapshot.controls)
        return self._snapshot