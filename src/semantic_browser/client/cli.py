"""CLI adapter for the Transparent Browser daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import click

DEFAULT_BASE = "http://127.0.0.1:8765"


def _request(method: str, path: str, data: dict | None = None, *, base: str = DEFAULT_BASE) -> dict:
    url = base.rstrip("/") + path
    body = None
    headers = {}
    if method == "GET" and data:
        url += "?" + urlencode(data)
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["content-type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        payload = json.loads(e.read().decode("utf-8"))
    except URLError as e:
        raise click.ClickException(f"daemon unavailable at {base}: {e.reason}") from e
    if not payload.get("ok"):
        raise click.ClickException(payload.get("error", "unknown daemon error"))
    return payload["data"]


def _print(data, json_out: bool = False):
    if json_out:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, str):
        click.echo(data)
    else:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@click.group()
@click.option("--base", default=DEFAULT_BASE, show_default=True, help="daemon base URL")
@click.pass_context
def tb(ctx, base):
    """Transparent Browser CLI adapter."""
    ctx.obj = {"base": base}


@tb.group()
def daemon():
    """Manage local daemon."""


@daemon.command("start")
@click.option("--port", default=8765, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--headed", is_flag=True)
@click.option("--state", help="storage_state JSON path")
@click.option("--background", is_flag=True, help="start in background")
def daemon_start(port, host, headed, state, background):
    cmd = [sys.executable, "-m", "semantic_browser.daemon.server", "--host", host, "--port", str(port)]
    if headed:
        cmd.append("--headed")
    if state:
        cmd.extend(["--state", state])
    if background:
        log = Path.home() / ".semantic-browser" / "daemon.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as f:
            subprocess.Popen(cmd, stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.5)
        click.echo(f"started: http://{host}:{port} (log: {log})")
    else:
        subprocess.call(cmd)


@daemon.command("status")
@click.option("--port", type=int, default=None,
              help="Override base URL with http://127.0.0.1:<port> (alternative to global --base)")
@click.option("--quiet", "-q", is_flag=True,
              help="Exit 0 if daemon alive, 1 otherwise (for shell scripts)")
@click.pass_context
def daemon_status(ctx, port, quiet):
    """Check daemon health. With --quiet, exit code 0/1 instead of raising.

    \b
    Examples:
      tb daemon status                  # default base (8765), verbose
      tb daemon status --port 18770     # custom port
      tb daemon status --quiet && echo "daemon up"  # script-friendly
    """
    base = f"http://127.0.0.1:{port}" if port else ctx.obj["base"]
    if quiet:
        try:
            data = _request("GET", "/health", base=base)
            click.echo(json.dumps(data, ensure_ascii=False))
        except click.ClickException as e:
            click.echo(f"daemon unreachable at {base}: {e.message}", err=True)
            ctx.exit(1)
    else:
        _print(_request("GET", "/health", base=base))


@daemon.command("stop")
@click.option("--port", type=int, default=8765, show_default=True)
def daemon_stop(port):
    """Stop a background daemon by port (uses PID file)."""
    pid_file = Path.home() / ".semantic-browser" / f"daemon-{port}.pid"
    if not pid_file.exists():
        raise click.ClickException(f"no PID file at {pid_file}; is the daemon running on port {port}?")
    pid = int(pid_file.read_text().splitlines()[0].strip())
    try:
        os.kill(pid, 15)  # SIGTERM
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        raise click.ClickException(f"pid {pid} not running (stale PID file removed)")
    except PermissionError:
        raise click.ClickException(f"pid {pid} not owned by current user")
    # 等进程退出 (SIGTERM 不一定触发 server finally; 我们自己清理 PID 文件)
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            click.echo(f"stopped: daemon on port {port} (pid {pid})")
            return
    click.echo(f"sent SIGTERM to pid {pid}; process still alive, may need kill -9")
    # 进程还活着; 强制清理 PID 文件, 让用户能重新 start
    pid_file.unlink(missing_ok=True)
    # B25: SIGTERM 没生效时必须返回非零退出码, 脚本 (`tb stop && start`) 不能
    # 误以为成功 — 让脚本显式处理失败。stderr 提示用户 kill -9 兜底。
    click.echo(
        f"⚠️  SIGTERM sent to pid {pid} but process still alive after 3s; "
        f"manually run `kill -9 {pid}`",
        err=True,
    )
    sys.exit(1)


@tb.command()
@click.argument("url")
@click.pass_context
def open(ctx, url):
    """Open URL in the persistent browser."""
    _print(_request("POST", "/open", {"url": url}, base=ctx.obj["base"]))


@tb.command()
@click.option("--json-out", is_flag=True)
@click.pass_context
def snapshot(ctx, json_out):
    """Return semantic snapshot for current page."""
    _print(_request("GET", "/snapshot", base=ctx.obj["base"]), json_out=json_out)


@tb.command()
@click.option("--format", "fmt", default="markdown", type=click.Choice(["markdown", "json"]))
@click.pass_context
def read(ctx, fmt):
    """Read current page article/main content."""
    data = _request("GET", "/read", {"format": fmt}, base=ctx.obj["base"])
    _print(data["content"], json_out=fmt == "json")


@tb.command("click")
@click.argument("ref")
@click.pass_context
def click_cmd(ctx, ref):
    """Click a ref from snapshot."""
    _print(_request("POST", "/click", {"ref": ref}, base=ctx.obj["base"]))


@tb.command("type")
@click.argument("ref")
@click.argument("text")
@click.pass_context
def type_cmd(ctx, ref, text):
    """Fill text into a ref."""
    _print(_request("POST", "/type", {"ref": ref, "text": text}, base=ctx.obj["base"]))


@tb.command("fill-form")
@click.option("--field", "fields", multiple=True, metavar="REF=TEXT",
              help="填一个字段, 格式 e1=hello (可多次使用)")
@click.option("--from-json", "json_path", type=click.Path(exists=True),
              help="从 JSON 文件读 {ref: text} 映射")
@click.pass_context
def fill_form(ctx, fields, json_path):
    """T11: 一次性填多个字段。

    \b
    Examples:
      tb fill-form --field e1=alice --field e2=alice@x.com
      tb fill-form --from-json form.json
    """
    field_map: dict[str, str] = {}
    if fields:
        for f in fields:
            if "=" not in f:
                raise click.ClickException(f"--field 格式必须是 REF=TEXT, got {f!r}")
            ref, text = f.split("=", 1)
            field_map[ref.strip()] = text
    if json_path:
        import json as _json
        data = _json.loads(Path(json_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise click.ClickException(f"--from-json 必须是 {{ref: text}} 对象, got {type(data).__name__}")
        field_map.update(data)
    if not field_map:
        raise click.ClickException("至少需要一个 --field 或 --from-json")
    _print(_request("POST", "/fill-form", {"fields": field_map}, base=ctx.obj["base"]))


@tb.command("retry")
@click.option("--action", required=True, type=click.Choice(["open", "click", "type"]),
              help="要 retry 的动作类型")
@click.option("--url", help="open 动作的 URL")
@click.option("--ref", help="click/type 动作的 ref")
@click.option("--text", help="type 动作的文本")
@click.option("--max-retries", default=2, show_default=True,
              help="最大 retry 次数 (额外调用)")
@click.pass_context
def retry_cmd(ctx, action, url, ref, text, max_retries):
    """T12: 带 retry 包装的一个动作 (短错误自动重试, 指数 backoff).

    \b
    Examples:
      tb retry --action open --url https://x.com
      tb retry --action click --ref e3 --max-retries 3
    """
    if action == "open":
        if not url:
            raise click.ClickException("--action open 需要 --url")
        args = {"url": url}
    elif action == "click":
        if not ref:
            raise click.ClickException("--action click 需要 --ref")
        args = {"ref": ref}
    else:  # type
        if not ref or text is None:
            raise click.ClickException("--action type 需要 --ref 和 --text")
        args = {"ref": ref, "text": text}
    _print(_request("POST", "/with-retry",
                    {"action": action, "args": args, "max_retries": max_retries},
                    base=ctx.obj["base"]))


@tb.command()
@click.argument("direction", default="down")
@click.argument("amount", type=int, default=500)
@click.pass_context
def scroll(ctx, direction, amount):
    _print(_request("POST", "/scroll", {"direction": direction, "amount": amount}, base=ctx.obj["base"]))


@tb.command()
@click.argument("key")
@click.pass_context
def press(ctx, key):
    _print(_request("POST", "/press", {"key": key}, base=ctx.obj["base"]))


@tb.command()
@click.argument("path", required=False)
@click.pass_context
def screenshot(ctx, path):
    _print(_request("POST", "/screenshot", {"path": path}, base=ctx.obj["base"]))


@tb.command("annotated-screenshot")
@click.argument("path", required=False)
@click.option("--json-out", is_flag=True,
              help="输出完整 JSON (含 png_base64 + sidecar). 默认只显示 sidecar + path.")
@click.pass_context
def annotated_screenshot(ctx, path, json_out):
    """截图 + 在原图上叠加 ref 标签框 (T10). LLM 能直接看图 + 知道每个 ref 在哪。"""
    data = _request("POST", "/screenshot/annotated", {"path": path}, base=ctx.obj["base"])
    if json_out:
        # full output, 包括 base64 PNG
        import json as _json
        click.echo(_json.dumps(data, ensure_ascii=False))
    else:
        # 不打印 base64 (太大), 只显示 sidecar 摘要
        sidecar = data.get("sidecar", {})
        click.echo(f"saved: {data.get('path')} ({data['bytes']} bytes)")
        click.echo(f"  refs: {sidecar.get('visible_count', 0)}/{sidecar.get('ref_count', 0)} visible")
        for r in sidecar.get("refs", [])[:8]:
            click.echo(f"  [{r['ref']}] {r['kind']:8s} bbox={r['bbox']}  {r['label'][:40]}")
        if sidecar.get("visible_count", 0) > 8:
            click.echo(f"  ...and {sidecar['visible_count'] - 8} more")


@tb.command("screenshot-sidecar")
@click.pass_context
def screenshot_sidecar(ctx):
    """只要 ref 元素位置 JSON (不要 PNG), 供 LLM plan 行动。"""
    _print(_request("POST", "/screenshot/sidecar", base=ctx.obj["base"]))


@tb.group("state")
def state_group():
    """Browser state commands."""


@state_group.command("show")
@click.pass_context
def state_show(ctx):
    _print(_request("GET", "/state", base=ctx.obj["base"]))


@state_group.command("save")
@click.argument("path", required=False)
@click.pass_context
def state_save(ctx, path):
    _print(_request("POST", "/state/save", {"path": path}, base=ctx.obj["base"]))


@tb.command()
@click.option("--domain", default="")
@click.pass_context
def history(ctx, domain):
    _print(_request("GET", "/history", {"domain": domain}, base=ctx.obj["base"]))


@tb.command()
@click.option("--url", default="")
@click.pass_context
def graph(ctx, url):
    data = {"url": url} if url else None
    _print(_request("GET", "/graph", data, base=ctx.obj["base"]))


@tb.command()
@click.argument("url")
@click.argument("keyword")
@click.option("--max-results", default=10, show_default=True)
@click.pass_context
def find(ctx, url, keyword, max_results):
    """Browse a URL and find sections containing KEYWORD (server-side, no daemon open needed)."""
    _print(_request("POST", "/find",
                    {"url": url, "keyword": keyword, "max_results": max_results},
                    base=ctx.obj["base"]))


@tb.command("extract-topic")
@click.argument("url")
@click.argument("keyword")
@click.option("--max-chars", default=4000, show_default=True)
@click.pass_context
def extract_topic(ctx, url, keyword, max_chars):
    """Browse a URL and extract a focused topic summary around KEYWORD."""
    _print(_request("POST", "/extract-topic",
                    {"url": url, "keyword": keyword, "max_chars": max_chars},
                    base=ctx.obj["base"]))


@tb.command()
@click.argument("url")
@click.argument("note")
@click.pass_context
def note(ctx, url, note):
    """Attach a NOTE to URL (persisted to MemoryStore)."""
    _print(_request("POST", "/note", {"url": url, "note": note}, base=ctx.obj["base"]))


@tb.command()
@click.option("--json-out", is_flag=True)
@click.pass_context
def stats(ctx, json_out):
    """Memory store stats (via daemon). Mirror of `sb stats`."""
    data = _request("GET", "/stats", base=ctx.obj["base"])
    _print(data, json_out=json_out)


@tb.command()
@click.argument("url", required=False)
@click.option("--limit", default=50, show_default=True)
@click.option("--json-out", is_flag=True)
@click.pass_context
def notes(ctx, url, limit, json_out):
    """Notes for URL, or all recent notes. Mirror of `sb notes`."""
    if json_out:
        q = {"limit": limit}
        if url:
            q["url"] = url
        data = _request("GET", "/notes", q, base=ctx.obj["base"])
        _print(data, json_out=True)
    else:
        q = {"limit": limit}
        if url:
            q["url"] = url
        data = _request("GET", "/notes", q, base=ctx.obj["base"])
        import time as _t
        if not data["notes"]:
            click.echo("(no notes)")
            return
        for n in data["notes"]:
            ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(n["created_at"]))
            url_disp = n.get("url", "") or ""
            click.echo(f"[{ts}] {url_disp}: {n['note']}")


@tb.group()
def tab():
    """Multi-tab management."""


@tab.command("list")
@click.option("--json-out", is_flag=True)
@click.pass_context
def tab_list(ctx, json_out):
    """列出所有 tab (with active marker)."""
    tabs = _request("GET", "/tab/list", base=ctx.obj["base"])
    if json_out:
        _print(tabs, json_out=True)
        return
    if not tabs:
        click.echo("(no tabs)")
        return
    for t in tabs:
        marker = "*" if t["active"] else " "
        click.echo(f"{marker} [{t['index']}] {t['url']}")


@tab.command("new")
@click.argument("url", required=False)
@click.pass_context
def tab_new(ctx, url):
    """Open a new tab (URL optional, blank if missing)."""
    _print(_request("POST", "/tab/new", {"url": url or ""}, base=ctx.obj["base"]))


@tab.command("switch")
@click.argument("index", type=int)
@click.pass_context
def tab_switch(ctx, index):
    """Switch to tab INDEX."""
    _print(_request("POST", "/tab/switch", {"index": index}, base=ctx.obj["base"]))


@tab.command("close")
@click.argument("index", type=int, required=False)
@click.pass_context
def tab_close(ctx, index):
    """Close tab INDEX (or current if INDEX omitted)."""
    body = {"index": index} if index is not None else {}
    _print(_request("POST", "/tab/close", body, base=ctx.obj["base"]))


@tb.group("wait-for")
def wait_for_group():
    """智能等待 — 比 sleep 更可靠。"""


@wait_for_group.command("text")
@click.argument("text")
@click.option("--timeout-ms", default=10000, show_default=True)
@click.option("--in", "in_selector", default="body", show_default=True,
              help="CSS selector to scope search (default: whole page)")
@click.option("--quiet", "-q", is_flag=True,
              help="exit 0 if found, 1 if timeout (for scripts)")
@click.pass_context
def wait_text(ctx, text, timeout_ms, in_selector, quiet):
    """Wait until TEXT appears on the page."""
    try:
        data = _request("POST", "/wait-for/text",
                        {"text": text, "timeout_ms": timeout_ms, "in_selector": in_selector},
                        base=ctx.obj["base"])
    except click.ClickException as e:
        if quiet:
            click.echo(str(e), err=True)
            ctx.exit(1)
        raise
    if quiet:
        ctx.exit(0 if data["found"] else 1)
    _print(data)


@wait_for_group.command("ref")
@click.argument("ref")
@click.option("--timeout-ms", default=10000, show_default=True)
@click.option("--quiet", "-q", is_flag=True)
@click.pass_context
def wait_ref(ctx, ref, timeout_ms, quiet):
    """Wait until REF appears in DOM."""
    try:
        data = _request("POST", "/wait-for/ref",
                        {"ref": ref, "timeout_ms": timeout_ms}, base=ctx.obj["base"])
    except click.ClickException as e:
        if quiet:
            click.echo(str(e), err=True)
            ctx.exit(1)
        raise
    if quiet:
        ctx.exit(0 if data["found"] else 1)
    _print(data)


@wait_for_group.command("url")
@click.argument("pattern")
@click.option("--timeout-ms", default=10000, show_default=True)
@click.option("--quiet", "-q", is_flag=True)
@click.pass_context
def wait_url(ctx, pattern, timeout_ms, quiet):
    """Wait until current URL contains PATTERN (substring)."""
    try:
        data = _request("POST", "/wait-for/url",
                        {"pattern": pattern, "timeout_ms": timeout_ms}, base=ctx.obj["base"])
    except click.ClickException as e:
        if quiet:
            click.echo(str(e), err=True)
            ctx.exit(1)
        raise
    if quiet:
        ctx.exit(0 if data["found"] else 1)
    _print(data)


@tb.command("run-workflow")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.option("--json-out", is_flag=True)
@click.pass_context
def run_workflow(ctx, workflow_file, json_out):
    """Run a JSON workflow file (multi-step action sequence).

    Workflow schema: {"name": "...", "on_error": "stop|continue", "steps": [{"action": ..., ...}]}
    See src/semantic_browser/workflow/runner.py for full action list.
    """
    _print(_request("POST", "/run-workflow",
                    {"workflow_file": str(workflow_file)},
                    base=ctx.obj["base"]),
           json_out=json_out)


def main():
    tb()


if __name__ == "__main__":
    main()
