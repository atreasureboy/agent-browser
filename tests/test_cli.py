"""
CLI 注册测试 — 不真正调用浏览器，只验证 Click 命令树结构、参数、help 输出。

用 Click 的 CliRunner，不需要 asyncio / playwright。
"""
from __future__ import annotations

from click.testing import CliRunner

import pytest

from semantic_browser.cli.main import cli


# ── 命令注册 ──────────────────────────────────────────────────

class TestCLIRegistration:
    def setup_method(self):
        self.runner = CliRunner()

    def test_help_lists_all_commands(self):
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for cmd in ("browse", "snapshot", "article", "graph", "history",
                    "stats", "login", "crawl", "interactive"):
            assert cmd in result.output, f"missing command: {cmd}"

    def test_version_option(self):
        result = self.runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    @pytest.mark.parametrize("cmd,help_contains", [
        ("browse", ["URL", "--no-headless", "--json-out"]),
        ("snapshot", ["URL"]),
        ("article", ["--markdown"]),
        ("graph", ["URL"]),
        ("crawl", ["--max-pages", "--max-depth", "--no-same-domain"]),
        ("interactive", ["URL", "--no-headless"]),
        ("login", ["--state"]),
        # T70.1: query 命令的 cache 相关 flag
        ("query", ["--cache-persist-path", "--clear-cache", "--verbose"]),
    ])
    def test_command_help(self, cmd, help_contains):
        result = self.runner.invoke(cli, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"
        for token in help_contains:
            assert token in result.output, f"{cmd} help missing: {token}"

    def test_unknown_command_errors(self):
        result = self.runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0

    def test_browse_requires_url_arg(self):
        result = self.runner.invoke(cli, ["browse"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "URL" in result.output

    def test_snapshot_requires_url_arg(self):
        result = self.runner.invoke(cli, ["snapshot"])
        assert result.exit_code != 0

    def test_interactive_url_optional(self):
        """interactive 不带 url 应该正常显示 help，不报错。"""
        result = self.runner.invoke(cli, ["interactive", "--help"])
        assert result.exit_code == 0
        assert "URL" in result.output