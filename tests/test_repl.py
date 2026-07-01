"""
REPL 单元测试 — 解析层 (纯函数) + 命令调度 (无浏览器)。

parse_command 是纯函数，完全可测。
execute() 通过 mocked controller + SnapshotEngine 测试各 handler。
"""
from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from semantic_browser.cli.repl import (
    Command,
    CommandResult,
    ParseError,
    parse_command,
)


# ── parse_command ─────────────────────────────────────────────

class TestParseCommand:
    @pytest.mark.parametrize("line,expected", [
        ("open https://example.com", ("open", ["https://example.com"])),
        ("click e3", ("click", ["e3"])),
        ("click 3", ("click", ["3"])),
        ("type e1 hello world", ("type", ["e1", "hello", "world"])),
        ('type e1 "hello world"', ("type", ["e1", "hello world"])),
        ("type e1 'hello world'", ("type", ["e1", "hello world"])),
        ("snapshot", ("snapshot", [])),
        ("SNAPSHOT", ("snapshot", [])),
        ("links 5", ("links", ["5"])),
        ("controls 10", ("controls", ["10"])),
        ("text", ("text", [])),
        ("text 30", ("text", ["30"])),
        ("url", ("url", [])),
        ("title", ("title", [])),
        ("back", ("back", [])),
        ("forward", ("forward", [])),
        ("reload", ("reload", [])),
        ("scroll down 800", ("scroll", ["down", "800"])),
        ("scroll up", ("scroll", ["up"])),
        ("aria", ("aria", [])),
        ("inspect e5", ("inspect", ["e5"])),
        ("refs", ("refs", [])),
        ("note remember this", ("note", ["remember", "this"])),
        ("extract", ("extract", [])),
        ("extract --markdown", ("extract", ["--markdown"])),
        ("help", ("help", [])),
        ("HELP", ("help", [])),
        ("h", ("help", [])),
        ("?", ("help", [])),
        ("quit", ("quit", [])),
        ("QUIT", ("quit", [])),
        ("exit", ("quit", [])),
        ("q", ("quit", [])),
        ("   ", ("empty", [])),
        ("", ("empty", [])),
    ])
    def test_parses_well(self, line, expected):
        cmd = parse_command(line)
        assert (cmd.name, cmd.args) == expected

    def test_raw_preserved(self):
        cmd = parse_command("  click  e3  ")
        assert cmd.raw.strip() == "click  e3"
        assert cmd.name == "click"
        assert cmd.args == ["e3"]

    def test_unclosed_quote_raises(self):
        with pytest.raises(ParseError):
            parse_command('type e1 "unclosed')

    def test_unknown_command_name(self):
        cmd = parse_command("foobar")
        assert cmd.name == "foobar"
        assert cmd.args == []

    def test_arg_helper_defaults(self):
        cmd = parse_command("click")
        assert cmd.arg(0, "default") == "default"
        assert cmd.arg(99, "fallback") == "fallback"

    def test_arg_helper_returns_value(self):
        cmd = parse_command("click e3")
        assert cmd.arg(0) == "e3"


# ── Fakes ─────────────────────────────────────────────────────

class _FakePage:
    """REPL 通过 controller.current_page 拿到 Page，需要 url 属性。"""
    url = "https://x.com/"


class _FakeController:
    """满足 REPL handler 调用面的最小替身。"""
    def __init__(self):
        self.url = "https://x.com/"
        self.title = "T"
        self.clicked = []
        self.typed = []
        self.scrolled = []
        self._page = _FakePage()
        self._started = True

    @property
    def current_page(self):
        return self._page

    async def start(self):
        self._started = True

    async def close(self):
        self._started = False

    async def open(self, url):
        self.url = url

    async def back(self):
        self.url = "about:blank"

    async def forward(self):
        self.url = "https://x.com/fwd"

    async def reload(self):
        pass

    async def scroll(self, direction, amount):
        self.scrolled.append((direction, amount))

    async def wait(self, seconds):
        pass

    async def click(self, ref):
        self.clicked.append(ref)
        return ref != "BAD"

    async def type_text(self, ref, text):
        self.typed.append((ref, text))
        return True

    async def get_url(self):
        return self.url

    async def get_title(self):
        return self.title

    async def get_aria_snapshot(self):
        return "- heading [ref=e1]"


def _make_fake_snapshot():
    from semantic_browser.snapshot.engine import (
        PageSnapshot, LinkInfo, ControlInfo, TextBlock,
    )
    return PageSnapshot(
        url="https://x.com/",
        title="Hello",
        domain="x.com",
        text_blocks=[TextBlock(tag="h1", text="Hello"), TextBlock(tag="p", text="World")],
        links=[LinkInfo(ref="e1", text="L1", href="https://x.com/a", internal=True),
               LinkInfo(ref="e2", text="L2", href="https://y.com/b", internal=False)],
        controls=[ControlInfo(ref="e3", kind="button", label="Go"),
                  ControlInfo(ref="e4", kind="textbox", label="name", placeholder="Your name")],
        meta={"description": "test"},
    )


@pytest.fixture
def fake_session(monkeypatch):
    """一个装好 fake controller + snapshot engine 的 REPLSession。"""
    from semantic_browser.cli.repl import REPLSession
    from semantic_browser.snapshot.engine import SnapshotEngine

    def fake_init(self, page):
        self.page = page
        self._calls = 0

    async def fake_capture(self, base_url=""):
        self._calls += 1
        return _make_fake_snapshot()

    monkeypatch.setattr(SnapshotEngine, "__init__", fake_init)
    monkeypatch.setattr(SnapshotEngine, "capture", fake_capture)

    ctl = _FakeController()
    console = Console(file=StringIO(), force_terminal=False, width=120)
    return REPLSession(ctl, console=console)


# ── execute() 调度 ────────────────────────────────────────────

@pytest.mark.asyncio
class TestREPLExecute:
    async def test_unknown_command_returns_error(self, fake_session):
        r = await fake_session.execute(parse_command("foobar"))
        assert r.error
        assert "未知命令" in r.text

    async def test_quit_sets_clear(self, fake_session):
        r = await fake_session.execute(parse_command("q"))
        assert r.clear
        assert "Bye" in r.text

    async def test_help_returns_panel(self, fake_session):
        r = await fake_session.execute(parse_command("help"))
        assert r.panel is not None
        assert "open" in r.panel.renderable

    async def test_empty_returns_noop(self, fake_session):
        r = await fake_session.execute(parse_command(""))
        assert r.error is False
        assert r.text == ""
        assert r.table is None and r.panel is None

    async def test_url_returns_current(self, fake_session):
        r = await fake_session.execute(parse_command("url"))
        assert r.text == "https://x.com/"

    async def test_title_returns_title(self, fake_session):
        r = await fake_session.execute(parse_command("title"))
        assert r.text == "T"

    async def test_snapshot_returns_table(self, fake_session):
        r = await fake_session.execute(parse_command("snapshot"))
        assert r.table is not None
        assert r.table.row_count == 6

    async def test_links_returns_table(self, fake_session):
        r = await fake_session.execute(parse_command("links"))
        assert r.table is not None
        assert r.table.row_count == 2

    async def test_links_with_limit(self, fake_session):
        r = await fake_session.execute(parse_command("links 1"))
        assert r.table is not None
        assert r.table.row_count == 1

    async def test_controls_returns_table(self, fake_session):
        r = await fake_session.execute(parse_command("controls"))
        assert r.table is not None
        assert r.table.row_count == 2

    async def test_text_returns_panel(self, fake_session):
        r = await fake_session.execute(parse_command("text"))
        assert r.panel is not None
        assert "Hello" in r.panel.renderable

    async def test_text_preview_length_parameter(self, fake_session):
        """`text N LEN` 控制每块预览长度 (默认 120)。"""
        # 默认 120 (短文本直接完整显示, 不在 title 标 preview=N)
        r = await fake_session.execute(parse_command("text"))
        assert "preview=" not in r.panel.title
        # 显式传 LEN, title 显示 preview=N
        r2 = await fake_session.execute(parse_command("text 10 200"))
        assert "preview=200" in r2.panel.title
        # 0 = 不截断 (完整文本), 不在 title 标 preview=
        r3 = await fake_session.execute(parse_command("text 10 0"))
        assert "preview=" not in r3.panel.title

    async def test_click_dispatches_to_controller(self, fake_session):
        r = await fake_session.execute(parse_command("click e3"))
        assert r.error is False
        assert "e3" in fake_session.controller.clicked
        assert "已点击" in r.text

    async def test_click_missing_ref_errors(self, fake_session):
        r = await fake_session.execute(parse_command("click"))
        assert r.error
        assert "用法" in r.text

    async def test_click_bad_ref_returns_error(self, fake_session):
        r = await fake_session.execute(parse_command("click BAD"))
        assert r.error
        assert "失败" in r.text

    async def test_type_dispatches_to_controller(self, fake_session):
        r = await fake_session.execute(parse_command('type e3 "hello world"'))
        assert r.error is False
        assert ("e3", "hello world") in fake_session.controller.typed

    async def test_type_requires_two_args(self, fake_session):
        r = await fake_session.execute(parse_command("type e3"))
        assert r.error

    async def test_scroll_dispatches(self, fake_session):
        r = await fake_session.execute(parse_command("scroll down 300"))
        assert r.error is False
        assert ("down", 300) in fake_session.controller.scrolled

    async def test_scroll_invalid_direction(self, fake_session):
        r = await fake_session.execute(parse_command("scroll left"))
        assert r.error

    async def test_back_updates_url(self, fake_session):
        r = await fake_session.execute(parse_command("back"))
        assert r.error is False
        assert fake_session.controller.url == "about:blank"
        assert r.text.startswith("←")

    async def test_forward_updates_url(self, fake_session):
        r = await fake_session.execute(parse_command("forward"))
        assert r.error is False
        assert fake_session.controller.url == "https://x.com/fwd"
        assert r.text.startswith("→")

    async def test_reload(self, fake_session):
        r = await fake_session.execute(parse_command("reload"))
        assert r.error is False

    async def test_inspect_finds_control(self, fake_session):
        r = await fake_session.execute(parse_command("inspect e3"))
        assert r.error is False
        assert r.panel is not None
        assert "button" in r.panel.renderable

    async def test_inspect_finds_link(self, fake_session):
        r = await fake_session.execute(parse_command("inspect e1"))
        assert r.error is False
        assert "https://x.com/a" in r.panel.renderable

    async def test_inspect_unknown_ref(self, fake_session):
        r = await fake_session.execute(parse_command("inspect e9999"))
        assert r.error
        assert "找不到" in r.text

    async def test_inspect_missing_arg(self, fake_session):
        r = await fake_session.execute(parse_command("inspect"))
        assert r.error

    async def test_refs_lists_seen_refs(self, fake_session):
        await fake_session.execute(parse_command("snapshot"))
        r = await fake_session.execute(parse_command("refs"))
        assert r.panel is not None
        assert "e3" in r.panel.renderable

    async def test_note_echo(self, fake_session):
        r = await fake_session.execute(parse_command("note remember this"))
        assert r.error is False
        assert "remember this" in r.text

    async def test_note_requires_text(self, fake_session):
        r = await fake_session.execute(parse_command("note"))
        assert r.error

    async def test_aria_returns_panel(self, fake_session):
        r = await fake_session.execute(parse_command("aria"))
        assert r.panel is not None


# ── 错误隔离 ──────────────────────────────────────────────────

@pytest.mark.asyncio
class TestREPLResilience:
    async def test_handler_exception_propagates_from_execute(self, monkeypatch):
        """execute 本身不吞异常，run_loop 才吞。验证 contract。"""
        from semantic_browser.cli.repl import REPLSession
        from semantic_browser.snapshot.engine import SnapshotEngine

        def boom_init(self, page):
            self.page = page

        async def boom_capture(self, base_url=""):
            raise RuntimeError("boom")

        monkeypatch.setattr(SnapshotEngine, "__init__", boom_init)
        monkeypatch.setattr(SnapshotEngine, "capture", boom_capture)

        ctl = _FakeController()
        console = Console(file=StringIO(), force_terminal=False)
        session = REPLSession(ctl, console=console)
        with pytest.raises(RuntimeError, match="boom"):
            await session.execute(parse_command("snapshot"))

    async def test_run_loop_survives_command_errors(self, monkeypatch):
        """run_loop 应该吞掉 handler 抛出的异常，不退出。"""
        from semantic_browser.cli.repl import REPLSession
        from semantic_browser.snapshot.engine import SnapshotEngine

        def boom_init(self, page):
            self.page = page

        async def boom_capture(self, base_url=""):
            raise RuntimeError("boom")

        monkeypatch.setattr(SnapshotEngine, "__init__", boom_init)
        monkeypatch.setattr(SnapshotEngine, "capture", boom_capture)

        ctl = _FakeController()
        console = Console(file=StringIO(), force_terminal=False)
        session = REPLSession(ctl, console=console)

        inputs = iter(["snapshot", "quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        await session.run_loop()  # 跑到这里 = 没崩