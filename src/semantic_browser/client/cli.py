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


@tb.command("vision-snapshot")
@click.option("--goal", default="", help="可选 — 用户当前目标 (让 LLM 重点突出相关元素)")
@click.option("--provider", default="",
              type=click.Choice(["", "anthropic", "gemini"]),
              help="强制选 vision provider (默认 auto-detect)")
@click.option("--model", default="", help="强制选 vision 模型")
@click.option("--full-page/--viewport-only", default=True,
              help="是否整页截图 (默认是)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def vision_snapshot(ctx, goal, provider, model, full_page, json_out):
    """T38: 截图 + vision LLM 描述页面 (canvas/SPA/shadow DOM fallback)."""
    args: dict = {"goal": goal, "full_page": full_page}
    if provider:
        args["provider"] = provider
    if model:
        args["model"] = model
    _print(_request("GET", "/snapshot-vision", args, base=ctx.obj["base"]),
           json_out=json_out)


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


@tb.command("heal-click")
@click.argument("ref")
@click.pass_context
def heal_click(ctx, ref):
    """T22: Self-healing click — 失败自动 force / JS click."""
    data = _request("POST", "/click/healed", {"ref": ref}, base=ctx.obj["base"])
    if data.get("ok"):
        if data.get("tried") and len(data["tried"]) > 1:
            click.echo(f"✓ healed: tried {data['tried']}")
        else:
            click.echo("✓")
    else:
        click.echo(f"✗ {data.get('error')} (tried: {data.get('tried')})", err=True)
        ctx.exit(1)


@tb.command("type")
@click.argument("ref")
@click.argument("text")
@click.pass_context
def type_cmd(ctx, ref, text):
    """Fill text into a ref."""
    _print(_request("POST", "/type", {"ref": ref, "text": text}, base=ctx.obj["base"]))


@tb.command("heal-type")
@click.argument("ref")
@click.argument("text")
@click.pass_context
def heal_type(ctx, ref, text):
    """T22: Self-healing type — 失败自动 force / JS set."""
    data = _request("POST", "/type/healed", {"ref": ref, "text": text}, base=ctx.obj["base"])
    if data.get("ok"):
        if data.get("tried") and len(data["tried"]) > 1:
            click.echo(f"✓ healed: tried {data['tried']}")
        else:
            click.echo("✓")
    else:
        click.echo(f"✗ {data.get('error')} (tried: {data.get('tried')})", err=True)
        ctx.exit(1)


@tb.command("hover")
@click.argument("ref")
@click.pass_context
def hover(ctx, ref):
    """T19: 鼠标悬停在 ref 上 (触发 tooltip / 下拉菜单 / hover 状态)."""
    _print(_request("POST", "/hover", {"ref": ref}, base=ctx.obj["base"]))


@tb.command("dblclick")
@click.argument("ref")
@click.pass_context
def dblclick(ctx, ref):
    """T19: 双击元素."""
    _print(_request("POST", "/dblclick", {"ref": ref}, base=ctx.obj["base"]))


@tb.command("rightclick")
@click.argument("ref")
@click.pass_context
def rightclick(ctx, ref):
    """T19: 右键点击 (打开 context menu)."""
    _print(_request("POST", "/rightclick", {"ref": ref}, base=ctx.obj["base"]))


@tb.command("drag")
@click.argument("from_ref")
@click.argument("to_ref")
@click.pass_context
def drag(ctx, from_ref, to_ref):
    """T19+T28: 拖拽 from_ref -> to_ref (鼠标手势, 失败自动 HTML5 fallback)."""
    _print(_request("POST", "/drag",
                    {"from_ref": from_ref, "to_ref": to_ref},
                    base=ctx.obj["base"]))


@tb.command("drag-html5")
@click.argument("from_ref")
@click.argument("to_ref")
@click.pass_context
def drag_html5(ctx, from_ref, to_ref):
    """T28: 强制 HTML5 drag-and-drop (DataTransfer + dispatchEvent)."""
    _print(_request("POST", "/drag/html5",
                    {"from_ref": from_ref, "to_ref": to_ref},
                    base=ctx.obj["base"]))


@tb.command("select-option")
@click.argument("ref")
@click.argument("value")
@click.pass_context
def select_option(ctx, ref, value):
    """T19: 在 <select> ref 上选 value (value / label / index 都可)."""
    _print(_request("POST", "/select-option",
                    {"ref": ref, "value": value},
                    base=ctx.obj["base"]))


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


@tb.command("set-files")
@click.argument("ref")
@click.argument("paths", nargs=-1, required=True,
                metavar="PATHS...")
@click.pass_context
def set_files(ctx, ref, paths):
    """T13: 通过 ref 给 file input 设置文件 (人类上传附件动作)."""
    _print(_request("POST", "/set-files",
                    {"ref": ref, "paths": list(paths)},
                    base=ctx.obj["base"]))


@tb.command("download")
@click.option("--ref", "trigger_ref",
              help="触发下载的 ref (例如 'Download' 按钮). 省略 = 监听下一次下载事件.")
@click.option("--save-to", type=click.Path(),
              help="保存路径 (省略则用 /tmp/<suggested_filename>)")
@click.option("--timeout-ms", default=30000, show_default=True)
@click.pass_context
def download(ctx, trigger_ref, save_to, timeout_ms):
    """T14: 触发下载并保存文件。返回 path/size/suggested_filename。"""
    body = {"timeout_ms": timeout_ms}
    if trigger_ref:
        body["trigger_ref"] = trigger_ref
    if save_to:
        body["save_to"] = save_to
    _print(_request("POST", "/download", body, base=ctx.obj["base"]))


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


@tb.group()
def debug():
    """T18: Console / Network / PageError 调试接口."""


@debug.command("console")
@click.option("--type", "msg_type",
              type=click.Choice(["log", "warn", "error", "info", "debug"]),
              help="按 console 类型过滤")
@click.option("--limit", default=100, show_default=True)
@click.pass_context
def debug_console(ctx, msg_type, limit):
    """最近 console.log/warn/error (默认 100 条)."""
    data = _request("GET", "/console", {"type": msg_type, "limit": limit}, base=ctx.obj["base"])
    if not data:
        click.echo("(no console messages)")
        return
    for m in data:
        loc = f"  @{m['location']}" if m.get("location") else ""
        click.echo(f"[{m['type']:5s}] {m['text']}{loc}")


@debug.command("network")
@click.option("--only-failed", is_flag=True, help="只看 4xx/5xx/网络失败")
@click.option("--method", help="按 HTTP method 过滤 (GET/POST/...)")
@click.option("--limit", default=100, show_default=True)
@click.pass_context
def debug_network(ctx, only_failed, method, limit):
    """最近 network 请求."""
    q = {"limit": limit}
    if only_failed:
        q["only_failed"] = "true"
    if method:
        q["method"] = method
    data = _request("GET", "/network", q, base=ctx.obj["base"])
    # 默认打印关键列; 加 --json-out 拿全量 (含 response_headers)
    if ctx.obj.get("json_out"):
        _print(data, json_out=True)
        return
    for r in data:
        status = r.get("status", "?")
        m = r.get("method", "?")
        u = r.get("url", "")
        click.echo(f"  {status:>4} {m:6s} {u[:120]}")


@debug.command("headers")
@click.argument("url")
@click.pass_context
def debug_headers(ctx, url):
    """T39: 按 URL 拿最近一次响应的 HTTP headers (CSP / HSTS / Set-Cookie 等)."""
    data = _request("GET", "/response-headers",
                    {"url": url}, base=ctx.obj["base"])
    if data is None:
        click.echo("(no matching response)")
        return
    for k, v in data.items():
        click.echo(f"  {k}: {v}")


@debug.command("dom-diff")
@click.option("--before-refs", required=True,
              help="逗号分隔的 ref 集合 (之前 snapshot 看到的)")
@click.pass_context
def debug_dom_diff(ctx, before_refs):
    """T39: DOM diff — 当前页面 ref 与 before_refs 比的 appeared/disappeared."""
    data = _request("GET", "/dom-diff",
                    {"before_refs": before_refs}, base=ctx.obj["base"])
    appeared = data.get("appeared", [])
    disappeared = data.get("disappeared", [])
    click.echo(f"current_url: {data.get('current_url','')}")
    if appeared:
        click.echo(f"appeared ({len(appeared)}):")
        for r in appeared[:20]:
            click.echo(f"  + {r}")
    if disappeared:
        click.echo(f"disappeared ({len(disappeared)}):")
        for r in disappeared[:20]:
            click.echo(f"  - {r}")
    if not appeared and not disappeared:
        click.echo("(no diff — page unchanged)")


@debug.command("script-source")
@click.argument("url")
@click.pass_context
def debug_script_source(ctx, url):
    """T39: 按 URL 抓 JS 源码 (deep 模式审计用)."""
    data = _request("GET", "/script-source",
                    {"url": url}, base=ctx.obj["base"])
    if not data:
        click.echo("(no response from daemon)")
        return
    src = data.get("source", "")
    click.echo(src)


@debug.command("errors")
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def debug_errors(ctx, limit):
    """未捕获 JS 异常 (page.on('pageerror'))."""
    data = _request("GET", "/errors", {"limit": limit}, base=ctx.obj["base"])
    if not data:
        click.echo("(no JS errors)")
        return
    for e in data:
        click.echo(f"[{e.get('name','Error')}] {e.get('message','')}")
        if e.get("page"):
            click.echo(f"  page: {e['page']}")


@debug.command("clear")
@click.pass_context
def debug_clear(ctx):
    """清空 console/network/error 缓冲 (通常在导航后调用)."""
    _print(_request("POST", "/debug/clear", base=ctx.obj["base"]))


@tb.command("dump-storage")
@click.option("--json-out", is_flag=True)
@click.pass_context
def dump_storage(ctx, json_out):
    """T40a: 客户端存储探针 — localStorage/sessionStorage 全文 + cookies 字段."""
    _print(_request("GET", "/storage", base=ctx.obj["base"]), json_out=json_out)


@tb.command("security-headers")
@click.argument("url")
@click.option("--json-out", is_flag=True)
@click.pass_context
def security_headers(ctx, url, json_out):
    """T40f: 按 URL 解析 CSP/HSTS/XFO/Referrer-Policy/COOP/COEP/Set-Cookie 等安全头."""
    _print(_request("GET", "/security-headers", {"url": url},
                    base=ctx.obj["base"]), json_out=json_out)


@tb.command("probe-paths")
@click.argument("url")
@click.option("--categories", default="",
              help="well_known,discovery,admin,debug (逗号分隔; 空=全部)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def probe_paths(ctx, url, categories, json_out):
    """T40b: 探测常见隐藏路径 (robots/sitemap/.well-known/admin/api/debug/actuator)."""
    q = {"url": url}
    if categories:
        q["categories"] = categories
    _print(_request("GET", "/probe-paths", q, base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("list-frames")
@click.option("--json-out", is_flag=True)
@click.pass_context
def list_frames(ctx, json_out):
    """T40e: 列出所有 frame 含结构 (depth/cross-origin/child_count)."""
    _print(_request("GET", "/frame/list", base=ctx.obj["base"]), json_out=json_out)


@tb.command("switch-frame")
@click.argument("name_or_url")
@click.pass_context
def switch_frame(ctx, name_or_url):
    """T40e: 切换活跃 frame (按 name substring 或 url substring; 'main'/'top' 切回顶层)."""
    _print(_request("POST", "/frame/switch", {"name_or_url": name_or_url},
                    base=ctx.obj["base"]))


@tb.command("extract-api-endpoints")
@click.option("--json-out", is_flag=True)
@click.pass_context
def extract_api_endpoints(ctx, json_out):
    """T40g: 从当前页面 JS 中提取 API endpoints (fetch/axios/XHR 模式)."""
    _print(_request("GET", "/extract-api-endpoints", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("extract-js-libraries")
@click.option("--json-out", is_flag=True)
@click.pass_context
def extract_js_libraries(ctx, json_out):
    """T42b: 识别页面 JS 库 (jQuery/React/Vue/...) + 版本 + 已知 CVE."""
    _print(_request("GET", "/extract-js-libraries", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("detect-graphql")
@click.argument("endpoint")
@click.option("--json-out", is_flag=True)
@click.pass_context
def detect_graphql(ctx, endpoint, json_out):
    """T42g: 跑 GraphQL introspection query dump schema (types/queries/mutations)."""
    _print(_request("GET", "/detect-graphql", {"endpoint": endpoint},
                    base=ctx.obj["base"]), json_out=json_out)


@tb.command("websockets")
@click.option("--limit", default=100, type=int)
@click.option("--json-out", is_flag=True)
@click.pass_context
def websockets(ctx, limit, json_out):
    """T40i: 返回累积的 WebSocket 连接列表 (wss://)."""
    _print(_request("GET", "/websockets", {"limit": limit}, base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("enumerate-subdomains")
@click.argument("host")
@click.option("--no-tls-san", is_flag=True, help="跳过 TLS cert SAN 检查 (只跑 crt.sh)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def enumerate_subdomains(ctx, host, no_tls_san, json_out):
    """T43a: 子域名枚举 — crt.sh + (默认) TLS cert SAN."""
    _print(_request("GET", "/enumerate-subdomains",
                    {"host": host, "include_tls_san": not no_tls_san},
                    base=ctx.obj["base"]), json_out=json_out)


@tb.command("extract-secrets-from-js")
@click.option("--json-out", is_flag=True)
@click.pass_context
def extract_secrets_from_js(ctx, json_out):
    """T43b: 扫当前页所有 <script src> 找硬编码 secret (AWS / GitHub / Bearer / api_key / 私钥)."""
    _print(_request("GET", "/extract-secrets-from-js", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("detect-waf")
@click.option("--json-out", is_flag=True)
@click.pass_context
def detect_waf(ctx, json_out):
    """T43c: WAF 指纹 — Cloudflare / Akamai / Imperva / AWS WAF / Fastly / Vercel / Netlify..."""
    _print(_request("GET", "/detect-waf", base=ctx.obj["base"]), json_out=json_out)


@tb.command("find-open-redirect-sinks")
@click.option("--json-out", is_flag=True)
@click.pass_context
def find_open_redirect_sinks(ctx, json_out):
    """T43d: 扫链接/form action 找开放重定向/SSRF sink (returnUrl, redirect, next, ...)."""
    _print(_request("GET", "/find-open-redirect-sinks", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("find-disclosure")
@click.option("--json-out", is_flag=True)
@click.pass_context
def find_disclosure(ctx, json_out):
    """T43e: 扫页面找敏感泄露 (email / 内网 IP / AWS key / GitHub token / 私钥 / 调试堆栈)."""
    _print(_request("GET", "/find-disclosure", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("analyze-exposed-files")
@click.option("--base-url", help="以另一 URL 为基准 (默认当前页 origin)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def analyze_exposed_files(ctx, base_url, json_out):
    """T43f: 探常见备份/源码/配置文件 (.git/HEAD, .env, phpinfo, .DS_Store...)."""
    _print(_request("GET", "/analyze-exposed-files",
                    {"base_url": base_url} if base_url else None,
                    base=ctx.obj["base"]), json_out=json_out)


@tb.command("discover-api-specs")
@click.option("--base-url", help="以另一 URL 为基准 (默认当前页 origin)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def discover_api_specs(ctx, base_url, json_out):
    """T43g: 探 OpenAPI / Swagger 端点 (swagger.json, openapi.json, v3/api-docs...)."""
    _print(_request("GET", "/discover-api-specs",
                    {"base_url": base_url} if base_url else None,
                    base=ctx.obj["base"]), json_out=json_out)


@tb.command("tls-subdomains")
@click.argument("host")
@click.option("--port", default=443, type=int)
@click.option("--json-out", is_flag=True)
@click.pass_context
def tls_subdomains(ctx, host, port, json_out):
    """T43h: TLS 证书解析 — issuer / 有效期 / SAN → 子域."""
    _print(_request("GET", "/tls-subdomains",
                    {"host": host, "port": port}, base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("fingerprint-tech")
@click.option("--json-out", is_flag=True)
@click.pass_context
def fingerprint_tech(ctx, json_out):
    """T43i: 技术栈指纹 — Server / X-Powered-By / meta generator / 框架 cookie."""
    _print(_request("GET", "/fingerprint-tech", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("decode-jwts")
@click.option("--json-out", is_flag=True)
@click.pass_context
def decode_jwts(ctx, json_out):
    """T43j: 在 storage/cookie/页面里找 JWT, 解码 header + payload (不验签)."""
    _print(_request("GET", "/decode-jwts", base=ctx.obj["base"]), json_out=json_out)


@tb.command("dns-records")
@click.argument("host")
@click.option("--json-out", is_flag=True)
@click.pass_context
def dns_records(ctx, host, json_out):
    """T44a: DNS 记录查询 (A/AAAA/MX/NS/TXT-SPF/DMARC) — DoH 避开 dig 依赖."""
    _print(_request("GET", "/dns-records", {"host": host}, base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("wayback-urls")
@click.argument("url")
@click.option("--limit", default=200, type=int)
@click.option("--json-out", is_flag=True)
@click.pass_context
def wayback_urls(ctx, url, limit, json_out):
    """T44b: Wayback Machine 历史 URL (旧端点/旧 secret)."""
    _print(_request("GET", "/wayback-urls",
                    {"url": url, "limit": limit}, base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("find-xss-sinks")
@click.option("--json-out", is_flag=True)
@click.pass_context
def find_xss_sinks(ctx, json_out):
    """T44c: 扫 <script> 找 DOM XSS sinks (eval/innerHTML/document.write)."""
    _print(_request("GET", "/find-xss-sinks", base=ctx.obj["base"]), json_out=json_out)


@tb.command("detect-auth-methods")
@click.option("--json-out", is_flag=True)
@click.pass_context
def detect_auth_methods(ctx, json_out):
    """T44d: CAPTCHA / OAuth provider / WebAuthn / MFA 检测."""
    _print(_request("GET", "/detect-auth-methods", base=ctx.obj["base"]), json_out=json_out)


@tb.command("check-csrf-coverage")
@click.option("--json-out", is_flag=True)
@click.pass_context
def check_csrf_coverage(ctx, json_out):
    """T44e: 对当前页每个 form 检查 CSRF token 是否存在."""
    _print(_request("GET", "/check-csrf-coverage", base=ctx.obj["base"]), json_out=json_out)


@tb.command("find-idor-urls")
@click.option("--json-out", is_flag=True)
@click.pass_context
def find_idor_urls(ctx, json_out):
    """T44f: 扫链接找 IDOR-prone URLs (/user/N, /order/N ...)."""
    _print(_request("GET", "/find-idor-urls", base=ctx.obj["base"]), json_out=json_out)


@tb.command("find-cloud-resources")
@click.option("--json-out", is_flag=True)
@click.pass_context
def find_cloud_resources(ctx, json_out):
    """T44g: 扫 page source 找 S3 / Azure Blob / GCP / Heroku / Firebase URL 泄露."""
    _print(_request("GET", "/find-cloud-resources", base=ctx.obj["base"]), json_out=json_out)


@tb.command("probe-http-methods")
@click.option("--base-url", help="以另一 URL 为基准 (默认当前页 origin)")
@click.option("--paths", help="逗号分隔的 path 列表 (默认 /, /api, /api/v1, ...)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def probe_http_methods(ctx, base_url, paths, json_out):
    """T44h: OPTIONS 探测每个 path 的 Allow header (PUT/DELETE/PATCH = 危险)."""
    q: dict | None = None
    if base_url or paths:
        q = {}
        if base_url:
            q["base_url"] = base_url
        if paths:
            q["paths"] = [p.strip() for p in paths.split(",") if p.strip()]
    _print(_request("GET", "/probe-http-methods", q, base=ctx.obj["base"]), json_out=json_out)


@tb.command("detect-2fa")
@click.option("--json-out", is_flag=True)
@click.pass_context
def detect_2fa(ctx, json_out):
    """T44i: 2FA / MFA 检测 (WebAuthn / TOTP / SMS / backup code / Duo)."""
    _print(_request("GET", "/detect-2fa", base=ctx.obj["base"]), json_out=json_out)


@tb.command("inventory-external-resources")
@click.option("--json-out", is_flag=True)
@click.pass_context
def inventory_external_resources(ctx, json_out):
    """T44j: 外部资源清单 (外链域名/跨域脚本/iframe/cross-origin form)."""
    _print(_request("GET", "/inventory-external-resources", base=ctx.obj["base"]),
           json_out=json_out)


@tb.command("parse-csp")
@click.option("--json-out", is_flag=True)
@click.pass_context
def parse_csp(ctx, json_out):
    """T44k: CSP 头解析 — 拆 directive + 标危险配置 (unsafe-inline / unsafe-eval / *)."""
    _print(_request("GET", "/parse-csp", base=ctx.obj["base"]), json_out=json_out)


@tb.command("check-subdomain-takeover")
@click.argument("host", required=False)
@click.option("--subdomains", help="逗号分隔的子域列表 (默认 14 个常见子域)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def check_subdomain_takeover(ctx, host, subdomains, json_out):
    """T44l: 子域接管信号 — 查 CNAME 跟易被接管服务签名比对."""
    if not host:
        # 默认用当前页 origin
        from urllib.parse import urlparse
        try:
            import httpx
            r = httpx.get(f"{ctx.obj['base']}/url", timeout=3)
            host = urlparse(r.json().get("url", "")).hostname or ""
        except Exception:
            host = ""
    if not host:
        click.echo("error: host required (or run after opening a page)")
        return
    q: dict | None = None
    if subdomains:
        q = {"subdomains": [s.strip() for s in subdomains.split(",") if s.strip()]}
    _print(_request("GET", "/check-subdomain-takeover",
                    {"host": host, "subdomains": q.get("subdomains")} if q else {"host": host},
                    base=ctx.obj["base"]), json_out=json_out)


@tb.group()
def cookies():
    """T17: Cookie 管理 (调试登录态)."""


@cookies.command("list")
@click.option("--url", help="过滤特定 URL 的 cookies")
@click.pass_context
def cookies_list(ctx, url):
    """列出所有 cookies (或某 URL 的)."""
    q = {"url": url} if url else None
    data = _request("GET", "/cookies", q, base=ctx.obj["base"])
    if not data:
        click.echo("(no cookies)")
        return
    for c in data:
        flags = []
        if c.get("httpOnly"):
            flags.append("httpOnly")
        if c.get("secure"):
            flags.append("secure")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        # 不打印 value (可能很长 / 含敏感)
        click.echo(f"{c['name']:30s} = {c['value'][:40]}{'...' if len(c['value']) > 40 else ''}  ({c['domain']}){flag_str}")


@cookies.command("set")
@click.argument("name")
@click.argument("value")
@click.option("--url", help="限定 cookie 作用的 URL")
@click.option("--domain", help="限定 cookie 作用的 domain (与 --url 二选一)")
@click.pass_context
def cookies_set(ctx, name, value, url, domain):
    """设置一个 cookie."""
    body = {"name": name, "value": value}
    if url:
        body["url"] = url
    if domain:
        body["domain"] = domain
    _print(_request("POST", "/cookies/set", body, base=ctx.obj["base"]))


@cookies.command("delete")
@click.argument("name")
@click.option("--url", help="限定删除的 URL")
@click.pass_context
def cookies_delete(ctx, name, url):
    """删一个 cookie."""
    body = {"name": name}
    if url:
        body["url"] = url
    _print(_request("POST", "/cookies/delete", body, base=ctx.obj["base"]))


@cookies.command("clear")
@click.pass_context
def cookies_clear(ctx):
    """清空所有 cookies."""
    _print(_request("POST", "/cookies/clear", base=ctx.obj["base"]))


@tb.group()
def storage():
    """T17: localStorage / sessionStorage 管理."""


@storage.command("list")
@click.option("--kind", default="local", type=click.Choice(["local", "session"]),
              show_default=True)
@click.pass_context
def storage_list(ctx, kind):
    """列出 storage 条目."""
    data = _request("GET", "/storage", {"kind": kind}, base=ctx.obj["base"])
    if not data:
        click.echo(f"(empty {kind}Storage)")
        return
    for k, v in data.items():
        v_disp = v[:60] + "..." if len(v) > 60 else v
        click.echo(f"{k:30s} = {v_disp}")


@storage.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--kind", default="local", type=click.Choice(["local", "session"]),
              show_default=True)
@click.pass_context
def storage_set(ctx, key, value, kind):
    """写一个 storage 条目."""
    _print(_request("POST", "/storage/set",
                    {"key": key, "value": value, "kind": kind},
                    base=ctx.obj["base"]))


@storage.command("clear")
@click.option("--kind", default="local", type=click.Choice(["local", "session", "all"]),
              show_default=True)
@click.pass_context
def storage_clear(ctx, kind):
    """清空 localStorage / sessionStorage."""
    _print(_request("POST", "/storage/clear", {"kind": kind}, base=ctx.obj["base"]))


@tb.group()
def keyboard():
    """T16: 键盘导航 / 焦点 / 快捷键."""


@keyboard.command("focus")
@click.argument("ref", required=False)
@click.pass_context
def keyboard_focus(ctx, ref):
    """查询当前焦点元素; 给 ref 时则把焦点设到该 ref."""
    if ref:
        _print(_request("POST", "/focus", {"ref": ref}, base=ctx.obj["base"]))
    else:
        data = _request("GET", "/focus", base=ctx.obj["base"])
        if not data:
            click.echo("(no element focused)")
        else:
            click.echo(f"[{data.get('tag','?')}] ref={data.get('ref') or '-'}  "
                       f"text={data.get('text','')[:40]}")


@keyboard.command("tab")
@click.option("--shift", is_flag=True, help="Shift+Tab (反方向)")
@click.option("--count", default=1, show_default=True, help="按几次")
@click.pass_context
def keyboard_tab(ctx, shift, count):
    """按 Tab / Shift+Tab N 次."""
    data = _request("POST", "/tab",
                    {"shift": "true" if shift else "false", "count": count},
                    base=ctx.obj["base"])
    if isinstance(data, dict) and data.get("ref"):
        click.echo(f"focused ref: {data['ref']}")
    else:
        click.echo(f"pressed Tab x{count}")


@keyboard.command("shortcut")
@click.argument("keys", nargs=-1, required=True, metavar="KEY...")
@click.pass_context
def keyboard_shortcut(ctx, keys):
    """键盘组合键. 用法: tb keyboard shortcut F5  或  Control a."""
    _print(_request("POST", "/keyboard/shortcut",
                    {"keys": list(keys)},
                    base=ctx.obj["base"]))


@keyboard.command("type")
@click.argument("text")
@click.option("--delay-ms", default=0, show_default=True,
              help="每键间隔 (ms). >0 模拟真实键入.")
@click.pass_context
def keyboard_type(ctx, text, delay_ms):
    """往当前焦点元素打字 (无需 ref)."""
    _print(_request("POST", "/keyboard/type",
                    {"text": text, "delay_ms": delay_ms},
                    base=ctx.obj["base"]))


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


@tb.group()
def frame():
    """iframe / frame management."""


@frame.command("list")
@click.option("--json-out", is_flag=True)
@click.pass_context
def frame_list(ctx, json_out):
    """列出所有 frame (顶层 + 所有 iframe)."""
    frames = _request("GET", "/frame/list", base=ctx.obj["base"])
    if json_out:
        _print(frames, json_out=True)
        return
    if not frames:
        click.echo("(no frames)")
        return
    for f in frames:
        marker = "*" if f["is_main"] else " "
        click.echo(f"{marker} {f['name']:30s} {f['url']}")


@frame.command("switch")
@click.argument("name_or_url")
@click.pass_context
def frame_switch(ctx, name_or_url):
    """Switch active frame by name substring or URL substring."""
    _print(_request("POST", "/frame/switch",
                    {"name_or_url": name_or_url},
                    base=ctx.obj["base"]))


@frame.command("to-top")
@click.pass_context
def frame_to_top(ctx):
    """回到顶层 frame (main)."""
    _print(_request("POST", "/frame/to-top", base=ctx.obj["base"]))


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


@tb.command("agent")
@click.argument("goal")
@click.option("--start-url", help="先打开这个 URL 再开始")
@click.option("--max-steps", default=20, show_default=True,
              help="最多步数 (LLM 决策循环)")
@click.option("--tier", default="smart",
              type=click.Choice(["cheap", "medium", "smart"]),
              show_default=True,
              help="决策模型层 (smart=复杂决策, cheap=便宜快速)")
@click.option("--no-slicing", is_flag=True,
              help="禁用 smart snapshot 切片 (默认开, 用 cheap 模型按 goal 过滤 ref)")
@click.option("--no-diagnostics", is_flag=True,
              help="禁用失败时自动 dump diagnostics")
@click.option("--allow-destructive", is_flag=True,
              help="T32: 关闭危险动作守卫 (默认 type/click 含 delete 等关键词会被拦截)")
@click.option("--dry-run", is_flag=True,
              help="T29: 只生成 plan 不执行 (用户先看再决定)")
@click.option("--stream", is_flag=True,
              help="T31: 实时流式输出每步 (需要在同一台机器跑 daemon)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def agent_cmd(ctx, goal, start_url, max_steps, tier, no_slicing, no_diagnostics, allow_destructive, dry_run, stream, json_out):
    """T21 + T26: LLM-driven autonomous loop — 给个目标, agent 自主完成.

    \b
    需环境变量 (任一组):
      LLM_API_KEY + LLM_BASE_URL + LLM_MODEL_<TIER>
      或 OPENAI_API_KEY + OPENAI_BASE_URL + OPENAI_MODEL (fallback)

    \b
    Examples:
      tb agent "find contact email" --start-url https://example.com
      tb agent "搜 'deepseek' 并提取第一条结果的标题" --start-url https://www.google.com
      tb agent "..." --tier cheap  (便宜模型跑, 适合简单任务)
      tb agent "..." --dry-run  (只看 plan, 不执行)
    """
    if dry_run:
        body = {"goal": goal, "tier": tier}
        if start_url:
            body["start_url"] = start_url
        data = _request("POST", "/agent/plan", body, base=ctx.obj["base"])
        if data.get("error"):
            click.echo(f"✗ plan failed: {data['error']}")
            return
        click.echo(f"Strategy: {data.get('thought','')}")
        click.echo("Plan:")
        for s in data.get("plan", []):
            arg_str = json.dumps(s.get("args", {}), ensure_ascii=False)
            why = s.get("why", "")
            click.echo(f"  [{s.get('step','?')}] {s.get('action','?'):12s} {arg_str}  — {why}")
        return

    body = {
        "goal": goal,
        "max_steps": max_steps,
        "tier": tier,
        "use_smart_slicing": not no_slicing,
        "use_failure_diagnostics": not no_diagnostics,
        "allow_destructive": allow_destructive,
    }
    if start_url:
        body["start_url"] = start_url
    # T31: --stream 实时打印每步 (回调通过 SSE-like 增量在 daemon 端)
    if stream:
        body["stream"] = True
    data = _request("POST", "/agent/run", body, base=ctx.obj["base"])
    if json_out:
        _print(data, json_out=True)
    else:
        if data.get("success"):
            click.echo(f"✓ 达成目标 (steps={data['total_steps']})")
            if data.get("answer"):
                click.echo(f"  答案: {data['answer']}")
        else:
            click.echo(f"✗ 未达成 (steps={data['total_steps']}): {data.get('reason','')}")
        for s in data.get("steps", []):
            mark = "✓" if s["success"] else "✗"
            arg_str = json.dumps(s["args"], ensure_ascii=False)
            click.echo(f"  [{s['step']}] {mark} {s['action']:12s} {arg_str}")
            if s.get("thought"):
                click.echo(f"      thought: {s['thought'][:80]}")


@tb.group()
def llm():
    """T23/T24: LLM 智能辅助 (snapshot 切片 / 摘要 / 字段抽取 / ref 查找)."""


@llm.command("stats")
@click.pass_context
def llm_stats(ctx):
    """显示当前 LLM 配置和调用统计 (cheap/medium/smart 各调几次)."""
    # 走 daemon 内部 stats 端点
    try:
        data = _request("GET", "/llm/stats", base=ctx.obj["base"])
    except click.ClickException:
        # daemon 没启 — fallback 本地
        from semantic_browser.llm import get_default_service
        svc = get_default_service()
        data = svc.stats()
    click.echo(f"available: {data['available']}")
    click.echo(f"models: {data['models']}")
    click.echo(f"call_counts: {data['call_counts']}")


@llm.command("slice")
@click.argument("goal")
@click.option("--max-refs", default=15, show_default=True)
@click.pass_context
def llm_slice(ctx, goal, max_refs):
    """T24: 给 goal → 当前页 top-K 最有用的 ref (cheap 模型)."""
    data = _request("POST", "/llm/slice",
                    {"goal": goal, "max_refs": max_refs},
                    base=ctx.obj["base"])
    useful = data.get("useful_refs", [])
    reason = data.get("reason", "")
    click.echo(f"reason: {reason}")
    click.echo(f"useful refs ({len(useful)}): {useful}")


@llm.command("summarize")
@click.argument("text")
@click.option("--max-chars", default=500, show_default=True)
@click.pass_context
def llm_summarize(ctx, text, max_chars):
    """T24: 长文摘要 (cheap 模型)."""
    data = _request("POST", "/llm/summarize",
                    {"text": text, "max_chars": max_chars},
                    base=ctx.obj["base"])
    click.echo(data.get("summary", ""))


@llm.command("extract")
@click.argument("schema_text")  # 格式: "name=str, price=float"  (k=v,k=v)
@click.option("--from-json", "text_source", type=click.Path(exists=True),
              help="从文件读源文本")
@click.option("--text", "text_inline", help="直接给源文本 (与 --from-json 互斥)")
@click.pass_context
def llm_extract(ctx, schema_text, text_source, text_inline):
    """T24: 结构化字段抽取 (cheap 模型).

    \b
    Examples:
      tb llm extract "name=str,price=float" --text "Apple iPhone costs $999"
    """
    # 解析 schema
    schema: dict[str, str] = {}
    for pair in schema_text.split(","):
        if "=" not in pair:
            raise click.ClickException(f"--schema 格式必须是 k=type, got {pair!r}")
        k, t = pair.split("=", 1)
        schema[k.strip()] = t.strip()
    if text_source:
        text = Path(text_source).read_text(encoding="utf-8")
    elif text_inline:
        text = text_inline
    else:
        raise click.ClickException("需要 --text 或 --from-json")
    data = _request("POST", "/llm/extract",
                    {"text": text, "schema": schema},
                    base=ctx.obj["base"])
    click.echo(json.dumps(data.get("fields", {}), ensure_ascii=False, indent=2))


@llm.command("find-ref")
@click.argument("description")
@click.pass_context
def llm_find_ref(ctx, description):
    """T24: 用语义描述找 ref (e.g. "登录按钮").

    解决 refresh 后 ref 重新编号的问题."""
    data = _request("POST", "/llm/find-ref",
                    {"description": description},
                    base=ctx.obj["base"])
    ref = data.get("ref")
    if ref:
        click.echo(f"ref: {ref}")
    else:
        click.echo("(no match)")


@tb.group()
def memory():
    """T27: 跨 session goal memory (跨次跑的 goal 答案缓存)."""


@memory.command("stats")
@click.pass_context
def memory_stats(ctx):
    """显示 goal memory 统计."""
    try:
        data = _request("GET", "/memory/stats", base=ctx.obj["base"])
    except click.ClickException:
        from semantic_browser.memory.goal_memory import GoalMemory
        data = GoalMemory().stats()
    click.echo(f"path: {data['path']}")
    click.echo(f"total: {data['total']} (success={data['success']}, failure={data['failure']})")
    click.echo(f"total_hits: {data['total_hits']}")


@memory.command("list")
@click.option("--limit", default=10, show_default=True)
@click.pass_context
def memory_list(ctx, limit):
    """列出最近 N 条 goal 记录."""
    try:
        data = _request("GET", f"/memory/list?limit={limit}", base=ctx.obj["base"])
    except click.ClickException:
        from semantic_browser.memory.goal_memory import GoalMemory
        data = {"entries": GoalMemory().list_recent(limit)}
    for e in data.get("entries", []):
        mark = "✓" if e.get("success") else "✗"
        hits = e.get("hit_count", 0)
        ans = (e.get("answer") or e.get("reason") or "")[:60]
        click.echo(f"  {mark} [hits={hits}] {e.get('goal','')[:50]} → {ans}")


@memory.command("clear")
@click.pass_context
def memory_clear(ctx):
    """清空 goal memory."""
    try:
        data = _request("POST", "/memory/clear", base=ctx.obj["base"])
        click.echo("cleared" if data.get("cleared") else "failed")
    except click.ClickException:
        from semantic_browser.memory.goal_memory import GoalMemory
        GoalMemory().clear()
        click.echo("cleared (local)")


@tb.command("discover")
@click.argument("start_url")
@click.option("--max-pages", default=15, show_default=True,
              help="最多爬多少页 (防失控)")
@click.option("--max-depth", default=2, show_default=True,
              help="BFS 深度 (从 start_url = 0)")
@click.option("--delay-ms", default=100, show_default=True,
              help="每页之间延迟 (礼貌爬取)")
@click.option("--json-out", is_flag=True)
@click.pass_context
def discover_cmd(ctx, start_url, max_pages, max_depth, delay_ms, json_out):
    """T30: 现场爬站点, 生成导航图给 agent 当参考.

    \b
    Examples:
      tb discover https://example.com
      tb discover https://example.com --max-pages 30 --max-depth 3
    """
    body = {
        "start_url": start_url,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "delay_ms": delay_ms,
    }
    data = _request("POST", "/discover", body, base=ctx.obj["base"])
    if json_out:
        _print(data, json_out=True)
        return
    click.echo(f"Pages visited: {len(data.get('pages_visited', []))}")
    click.echo(f"Pages failed:  {len(data.get('pages_failed', []))}")
    click.echo("")
    click.echo(data.get("tree_text", "(empty)"))
    if data.get("llm_summary"):
        click.echo("")
        click.echo("--- LLM summary (for agent) ---")
        click.echo(data["llm_summary"])


@tb.command("bench")
@click.argument("tasks_file", type=click.Path(exists=True))
@click.option("--tier", default="smart",
              type=click.Choice(["cheap", "medium", "smart"]),
              show_default=True)
@click.option("--max-steps", default=20, show_default=True)
@click.option("--json-out", is_flag=True)
@click.pass_context
def bench_cmd(ctx, tasks_file, tier, max_steps, json_out):
    """T35: 跑一组 golden task 评测 agent.

    \b
    tasks_file 是 JSON 列表, schema:
      [{"name": "...", "goal": "...", "start_url": "...",
        "expected": {"answer_contains": "...", "max_steps": N},
        "tags": [...]}]
    """
    import asyncio
    from semantic_browser.bench import load_tasks, run_benchmark
    from semantic_browser.llm import LLMService
    from semantic_browser.browser.controller import BrowserController, BrowserConfig

    tasks = load_tasks(tasks_file)
    click.echo(f"Loaded {len(tasks)} tasks from {tasks_file}")

    async def run():
        ctrl = BrowserController(BrowserConfig())
        try:
            await ctrl.start()
            report = await run_benchmark(
                tasks, llm_service=LLMService(),
                controller=ctrl, tier=tier, max_steps=max_steps,
                use_memory=False,
            )
        finally:
            await ctrl.close()
        return report

    report = asyncio.run(run())
    if json_out:
        _print(report.to_dict(), json_out=True)
        return
    click.echo("")
    click.echo(f"=== Results: {report.succeeded}/{report.total} "
               f"({report.success_rate*100:.0f}%) ===")
    click.echo(f"Avg steps: {report.avg_steps:.1f}")
    click.echo(f"Avg duration: {report.avg_duration_sec:.2f}s")
    if report.failure_reasons:
        click.echo("Failure reasons:")
        for reason, count in report.failure_reasons.items():
            click.echo(f"  ×{count}  {reason[:80]}")
    click.echo("")
    for r in report.results:
        mark = "✓" if r.success else "✗"
        click.echo(f"  {mark} {r.task.name} (steps={r.actual_steps}, {r.duration_sec:.1f}s)")
        if not r.success:
            click.echo(f"      reason: {r.failure_reason[:80]}")
    click.echo(data.get("tree_text", "(empty)"))
    if data.get("llm_summary"):
        click.echo("")
        click.echo("--- LLM summary (for agent) ---")
        click.echo(data["llm_summary"])


def main():
    tb()


if __name__ == "__main__":
    main()
