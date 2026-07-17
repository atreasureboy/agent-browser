#!/usr/bin/env python3
"""
Semantic Browser CLI。

用法:
    sb browse <url>           # 浏览一个页面，输出语义快照
    sb snapshot <url>         # 只输出快照 JSON
    sb article <url>          # 提取文章内容
    sb graph <url>            # 查看站点拓扑图
    sb history [domain]       # 查看访问历史
    sb stats                  # 查看记忆统计
    sb interactive <url>      # 交互式浏览（打开后可输入指令）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

console = Console()


def _emit_json(data: dict | list, *, indent: int = 2) -> None:
    """输出 valid JSON 到 stdout。

    故意不用 rich.console.print_json: 它对含 '\\n' / 控制字符的字符串处理有 bug,
    会破坏 output 解析。直接 json.dumps + print, 保证下游 (jq, python -c json.load) 能消费。
    """
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=indent) + "\n")
    sys.stdout.flush()


def _resolve_llm_flag(explicit: bool | None) -> bool:
    """决定是否启用 LLM 增强分类。

    显式传 True/False 用 explicit; 否则根据 OPENAI_API_KEY 是否存在自动判断:
    设了 key 就启用, 否则只用启发式 (避免网络/付费意外)。
    """
    if explicit is not None:
        return explicit
    return bool(os.getenv("OPENAI_API_KEY"))


def _install_silent_unraisable_hook():
    """静默 `Event loop is closed` 错误 (来自 Playwright transport GC race)。

    asyncio.run() 关闭 loop 后, 残留的 BaseSubprocessTransport 在 __del__ 里
    调 loop.call_soon 触发 RuntimeError, 写到 stderr 污染 agent 输出。
    sys.unraisablehook 让 Python GC 在 __del__ 抛错时回调这里, 我们吃掉这个
    特定错误。CLI 进程一次性, 钩子常驻即可。
    """
    def _hook(unraisable):
        msg = str(unraisable.exc_value) if unraisable.exc_value else ""
        if "Event loop is closed" in msg or "loop is closed" in msg.lower():
            return  # 静默 Playwright transport cleanup race
        # 其它错误按默认行为
        try:
            sys.__unraisablehook__(unraisable)
        except AttributeError:
            pass

    sys.unraisablehook = _hook


# 模块加载时即安装 (一次性 CLI 进程, 不需要恢复)
_install_silent_unraisable_hook()


def _run_async(coro):
    """同步运行异步函数。

    把内部异常 (Playwright / 网络 / 参数错) 转成单行 stderr + 非零退出码,
    而不是 30 行 Python traceback。调试细节仍可看 --verbose / PYTHONASYNCIODEBUG=1。
    """
    try:
        return asyncio.run(coro)
    except click.exceptions.Exit:
        raise  # click 自带 exit (--quiet 等), 不要吞
    except click.ClickException:
        raise  # click 自带 ClickException (参数错误), 不要吞
    except KeyboardInterrupt:
        click.echo("\n[中断]", err=True)
        sys.exit(130)
    except Exception as e:
        msg = _humanize_error(e)
        click.echo(f"Error: {msg}", err=True)
        if os.getenv("SB_DEBUG"):
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def _humanize_error(e: Exception) -> str:
    """把常见异常转成单行 + 修复提示, 而不是堆栈。"""
    name = type(e).__name__
    msg = str(e).strip()
    first_line = msg.splitlines()[0] if msg else ""
    # 网络错误 (优先于 "无效 URL" 判断, 因为 DNS 失败也走 Page.goto)
    if "ERR_NAME_NOT_RESOLVED" in msg:
        return f"域名不存在或 DNS 解析失败: {first_line[:200]}"
    if "ERR_CONNECTION_REFUSED" in msg or "ERR_CONNECTION_RESET" in msg:
        return f"连接被拒/重置: {first_line[:200]}"
    if "net::" in msg:
        return f"网络错误: {first_line[:200]}"
    # Playwright 协议错误
    if "Cannot navigate to invalid URL" in msg:
        return f"无效 URL (must include scheme, e.g. https://...): {first_line[:200]}"
    if "Timeout" in name or "timeout" in msg.lower()[:50]:
        return f"超时: {first_line[:200]}"
    # 401/403/429
    if "401" in msg or "Unauthorized" in msg:
        return f"认证失败 (检查 OPENAI_API_KEY): {first_line[:200]}"
    if "429" in msg:
        return f"API rate limit: {first_line[:200]}"
    # 参数错
    if isinstance(e, (ValueError, KeyError, TypeError)):
        return f"{name}: {first_line[:200]}"
    return f"{name}: {first_line[:200] or '(no message)'}"


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """Semantic Browser — Agent-readable semantic browser layer."""
    pass


@cli.command()
@click.argument("url")
@click.option("--no-headless", is_flag=True, help="显示浏览器窗口")
@click.option("--json-out", is_flag=True, help="输出 JSON 格式 (含完整正文)")
@click.option("--llm/--no-llm", default=None,
              help="启用/禁用 LLM 增强分类。默认: OPENAI_API_KEY 存在时启用, 否则仅启发式。")
def browse(url, no_headless, json_out, llm):
    """浏览一个 URL，输出语义快照。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser(
            headless=not no_headless,
            use_llm_classifier=_resolve_llm_flag(llm),
        )
        await sb.start()
        try:
            result = await sb.browse(url)
            if json_out:
                # --json-out 总是带全文, 避免 agent 多次调用
                _emit_json(result.to_dict(full=True))
            else:
                _print_browse_result(result)
        finally:
            await sb.close()

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.option("--json-out", is_flag=True, default=True,
              help="输出 JSON 格式 (默认即 JSON, flag 仅为 README/脚本兼容)")
def snapshot(url, json_out):
    """只输出页面语义快照 JSON。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser()
        await sb.start()
        try:
            result = await sb.browse(url, extract_content=False)
            if json_out:
                # 直接 stdout 写 valid JSON, 绕过 rich.print_json (它对含换行的字符串处理有 bug)
                _emit_json(result.snapshot.to_dict())
            else:
                from rich.console import Console
                Console().print(result.snapshot.to_dict())
        finally:
            await sb.close()

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.option("--markdown", is_flag=True, help="输出 Markdown 格式")
def article(url, markdown):
    """[DEPRECATED] 提取文章内容。请用 `sb browse <url> --json-out` 替代。

    原因: `browse --json-out` 现在一次性返回完整 snapshot + article + classification,
    不需要分两次调用。`article` 保留仅为向后兼容, 输出会包含 deprecation 警告。
    """
    import sys
    sys.stderr.write(
        "[DEPRECATED] `sb article` 已废弃, 请用 `sb browse <url> --json-out` 替代\n"
    )
    sys.stderr.flush()
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser()
        await sb.start()
        try:
            result = await sb.browse(url)
            if result.article:
                if markdown:
                    console.print(result.article.to_markdown())
                else:
                    _print_article(result.article)
            else:
                console.print("[yellow]未检测到文章内容[/yellow]")
                console.print(f"页面类型: {result.classification.page_type}")
        finally:
            await sb.close()

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.option("--json-out", is_flag=True, help="输出 JSON (含 nodes/edges/树状结构)")
def graph(url, json_out):
    """查看站点拓扑图。"""
    from semantic_browser.engine import SemanticBrowser
    sb = SemanticBrowser()
    g = sb.get_site_graph(url)
    if json_out:
        _emit_json(g.to_dict())
    else:
        console.print(Panel(g.to_tree_text(), title=f"🌐 {g.domain}", border_style="blue"))


@cli.command()
@click.argument("domain", required=False)
@click.option("--limit", default=50, show_default=True, help="最多返回多少条")
@click.option("--json-out", is_flag=True, help="输出 JSON (含每页 url/title/type/visit_count)")
def history(domain, limit, json_out):
    """查看访问历史。"""
    from semantic_browser.engine import SemanticBrowser
    sb = SemanticBrowser()
    pages = sb.get_visited_pages(domain or "")[:limit]
    if json_out:
        _emit_json({"count": len(pages), "pages": pages})
    else:
        if not pages:
            console.print("[yellow]暂无访问历史[/yellow]")
            return
        _print_pages_table(pages)


@cli.command()
@click.option("--json-out", is_flag=True, help="输出 JSON (含 pages_count/links_count/actions_count/sessions_count/db_size_mb)")
def stats(json_out):
    """查看记忆统计。"""
    from semantic_browser.engine import SemanticBrowser
    sb = SemanticBrowser()
    s = sb.get_memory_stats()
    if json_out:
        _emit_json(s)
    else:
        _print_stats(s)


@cli.command()
@click.option("--older-than-days", default=30, show_default=True,
              help="删除 N 天前访问过的页面 (级联: 链接、动作历史)")
@click.option("--dry-run", is_flag=True, help="只统计会被删除的数量, 不实际删除")
@click.option("--yes", "-y", is_flag=True, help="跳过确认提示 (默认会询问确认)")
@click.option("--json-out", is_flag=True, help="输出 JSON (含 pages/links/actions/urls 删除数量)")
def cleanup(older_than_days, dry_run, yes, json_out):
    """清理旧访问记录。

    保留: notes (用户笔记独立于页面生命周期), sessions (会话索引)。
    删除: pages, links (from_url 指向被删页), actions (早于 cut-off)。
    """
    from semantic_browser.memory.store import MemoryStore
    store = MemoryStore(Path.home() / ".semantic-browser" / "memory.db")
    if not dry_run and not yes:
        console.print(
            f"[yellow]⚠️  将删除 {older_than_days} 天前的所有页面记录 (级联删除链接/动作)[/yellow]"
        )
        if not click.confirm("确认执行? ", default=False):
            console.print("[dim]已取消[/dim]")
            return
    result = store.cleanup_older_than(older_than_days, dry_run=dry_run)
    if json_out:
        _emit_json({"dry_run": dry_run, "older_than_days": older_than_days, **result})
    else:
        verb = "将删除" if dry_run else "已删除"
        console.print(
            f"[green]✓ {verb} {result['pages']} 个页面, "
            f"{result['links']} 个链接, {result['actions']} 个动作"
            + (" (dry run)" if dry_run else "")
        )


@cli.command()
@click.argument("url", required=False)
@click.option("--limit", default=20, show_default=True)
@click.option("--json-out", is_flag=True, help="输出 JSON (含 url/created_at/note)")
def notes(url, limit, json_out):
    """查看 URL 的笔记 (省略 URL 时列出所有最近的笔记)。"""
    from pathlib import Path
    from semantic_browser.memory.store import MemoryStore
    store = MemoryStore(Path.home() / ".semantic-browser" / "memory.db")
    import time as _t
    if url:
        rows = store.get_notes(url)
        if json_out:
            _emit_json({"url": url, "count": len(rows), "notes": rows[:limit]})
            return
        if not rows:
            console.print(f"[yellow]该 URL 没有笔记: {url}[/yellow]")
            return
        table = Table(title=f"📝 Notes for {url}", border_style="yellow")
        table.add_column("时间", style="dim", width=18)
        table.add_column("笔记", width=80)
        for n in rows[:limit]:
            ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(n["created_at"]))
            table.add_row(ts, n["note"])
        console.print(table)
    else:
        # 列出所有 URL 的最近笔记
        with store._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            notes_list = [dict(r) for r in rows]
        if json_out:
            _emit_json({"count": len(notes_list), "notes": notes_list})
            return
        if not notes_list:
            console.print("[yellow]还没有任何笔记。在 REPL 里用 `note <text>` 添加。[/yellow]")
            return
        table = Table(title="📝 Recent Notes (all URLs)", border_style="yellow")
        table.add_column("时间", style="dim", width=18)
        table.add_column("URL", style="cyan", width=40)
        table.add_column("笔记", width=60)
        for n in notes_list:
            ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(n["created_at"]))
            table.add_row(ts, (n["url"] or "")[:40], (n["note"] or "")[:60])
        console.print(table)


@cli.command()
@click.argument("url")
@click.option("--state", default="~/.semantic-browser/storage-state.json", help="登录态保存路径")
def login(url, state):
    """打开可视浏览器完成登录，并保存 cookies/localStorage。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser(headless=False, storage_state_path=state)
        await sb.start()
        try:
            await sb.controller.open(url)
            console.print("[cyan]在打开的浏览器里完成登录，然后回到终端按 Enter 保存登录态。[/cyan]")
            await asyncio.to_thread(input)
            saved = await sb.save_storage_state(state)
            console.print(f"[green]✓ 登录态已保存: {saved}[/green]")
        finally:
            await sb.close()

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.option("--max-pages", default=20, help="最多爬取页面数")
@click.option("--max-depth", default=3, help="最大爬取深度")
@click.option("--no-same-domain", is_flag=True, help="允许跨域爬取")
@click.option("--json-out", is_flag=True, help="输出 JSON 格式 (含 visited/failed/stats)")
@click.option("--llm/--no-llm", default=None,
              help="启用/禁用 LLM 增强分类 (默认: OPENAI_API_KEY 存在时启用)")
def crawl(url, max_pages, max_depth, no_same_domain, json_out, llm):
    """从 URL 开始，自动爬取站内页面（BFS + 断点续跑）。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        from semantic_browser.crawler.runner import Crawler

        sb = SemanticBrowser(use_llm_classifier=_resolve_llm_flag(llm))
        crawler = Crawler(sb)
        if not json_out:
            console.print(f"[cyan]🕷️  Starting crawl: {url}[/cyan]")
            console.print(f"   max_pages={max_pages} max_depth={max_depth} same_domain={not no_same_domain}")
        result = await crawler.crawl(
            url,
            max_pages=max_pages,
            max_depth=max_depth,
            same_domain_only=not no_same_domain,
        )
        if json_out:
            _emit_json(result.to_dict())
        else:
            console.print(f"\n[green]✓ Crawl complete[/green]")
            console.print(f"   Visited: {len(result.visited_urls)} pages")
            console.print(f"   Failed:  {len(result.failed_urls)} pages")
            if result.stats:
                for k, v in result.stats.items():
                    console.print(f"   {k}: {v}")

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.option("--out", default="screenshot.png", show_default=True,
              help="保存截图的路径 (PNG)")
@click.option("--full-page", is_flag=True, help="截整页 (默认只视口)")
def screenshot(url, out, full_page):
    """打开 URL 并保存当前页面的截图。"""
    async def _run():
        from semantic_browser.browser.controller import BrowserController, BrowserConfig
        controller = BrowserController(BrowserConfig())
        try:
            await controller.start()
            await controller.open(url)
            page = controller.current_page
            data = await page.screenshot(path=out, full_page=full_page)
            from pathlib import Path
            p = Path(out)
            if p.exists():
                click.echo(f"saved: {p} ({p.stat().st_size} bytes)")
            else:
                p.write_bytes(data)
                click.echo(f"saved: {p} ({len(data)} bytes)")
        finally:
            await controller.close()

    _run_async(_run())


@cli.command()
@click.argument("url")
@click.argument("keyword")
@click.option("--max-results", default=10, help="最多返回 section 数")
@click.option("--json-out", is_flag=True)
@click.option("--llm/--no-llm", default=None,
              help="启用/禁用 LLM 增强分类 (默认: OPENAI_API_KEY 存在时启用)")
def find(url, keyword, max_results, json_out, llm):
    """在 url 文章中查找包含 keyword 的 sections。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser(use_llm_classifier=_resolve_llm_flag(llm))
        await sb.start()
        try:
            data = await sb.find(url, keyword, max_results=max_results)
            if json_out:
                _emit_json(data)
            else:
                _print_find_result(data, keyword)
        finally:
            await sb.close()

    _run_async(_run())


@cli.command("query")
@click.argument("query")
@click.option("--start-url", default=None, help="入口 URL (省略时仅返回 plan)")
@click.option("--budget", type=int, default=None, help="LLM token 预算 (默认 2000)")
@click.option("--max-pages", type=int, default=None, help="最大浏览页数 (默认 1)")
@click.option("--cache-persist-path", default=None,
              help="持久化 cache 路径 (跨 sb query 调用复用)")
@click.option("--clear-cache", is_flag=True, help="清空 cache 后再跑")
@click.option("--json-out", is_flag=True, help="输出结构化 JSON")
@click.option("--quiet", "-q", is_flag=True, help="只输出最终 answer markdown")
@click.option("--verbose", "-v", is_flag=True, help="显示 cache stats + steps 详情")
def semantic_query(query, start_url, budget, max_pages, cache_persist_path, clear_cache, json_out, quiet, verbose):
    """Model-driven semantic query: 自然语言问题 → M3 驱动浏览 → 紧凑答案.

    这是"模型驱动的浏览器语义层"的顶层入口. 顶层 agent 用一次调用获取:
    - markdown 精炼答案 (≤ max_chars)
    - sources URL 列表
    - confidence (M3 自评)
    - tokens_used (透明披露)
    - 二次调用命中 cache → 0 token 消耗 (跨进程可用 --cache-persist-path)

    示例:
        sb query "找到 GitHub 关于 PEP 703 的最新讨论并给我 3 个观点" \\
                --start-url https://github.com/python/peps

        sb query "Python 3.13 features" \\
                --start-url https://docs.python.org/3/whatsnew/3.13.html \\
                --cache-persist-path /tmp/my_cache.json \\
                --verbose  # 显示 cache stats + steps
    """
    async def _run():
        from semantic_browser.query import SemanticQuery
        sq = SemanticQuery(
            budget=budget or SemanticQuery.DEFAULT_BUDGET,
            max_pages=max_pages if max_pages is not None else SemanticQuery.DEFAULT_MAX_PAGES,
            cache_persist_path=cache_persist_path,
        )
        try:
            if clear_cache:
                cleared = sq.clear_cache()
                if verbose:
                    click.echo(f"[cache cleared: {cleared['cleared']} entries]", err=True)
            result = await sq.run(query, start_url=start_url)
            if json_out:
                _emit_json(result.to_dict())
            elif quiet:
                click.echo(result.answer)
            else:
                # 可读视图
                click.echo("=" * 60)
                click.echo(f"Query: {query}")
                click.echo("=" * 60)
                click.echo(result.answer)
                click.echo("-" * 60)
                tu = result.tokens_used or {}
                click.echo(
                    f"[confidence={result.confidence:.2f}  "
                    f"tokens={tu.get('used', {}).get('total', 0)}/{tu.get('max_total', 0)}  "
                    f"sources={len(result.sources)}  "
                    f"success={result.success}]"
                )
                # T70: cache hit 标注
                if tu.get('cache_hit'):
                    click.echo(f"[cache_hit=True age={tu.get('cache_age_s')}s]")
                # T70: --verbose 显示 cache stats + 步骤 phase 详情
                if verbose:
                    click.echo(f"[cache_stats: {sq.cache_stats()}]", err=True)
                    if result.steps:
                        phases = [s.get('phase') for s in result.steps if s.get('phase')]
                        click.echo(f"[step phases: {phases}]", err=True)
                elif result.steps:
                    click.echo(f"[steps={len(result.steps)} see --json-out for detail]")
        finally:
            await sq.close()


@cli.command("query-log")
@click.option("--limit", type=int, default=50, help="返回条数 (1-100)")
@click.option("--json-out", is_flag=True, help="输出 JSON")
@click.option("--daemon-base", default=None,
              help="daemon HTTP base URL (默认读 SMOKE_PORT 或 localhost:8765)")
def query_log(limit, json_out, daemon_base):
    """T83: 查看 daemon 最近 N 条 query log (audit/debug)."""
    import urllib.request
    import urllib.error
    import json as _json
    base = daemon_base or os.environ.get(
        "SB_DAEMON_BASE", f"http://127.0.0.1:{os.environ.get('SMOKE_PORT', '8765')}")
    try:
        with urllib.request.urlopen(f"{base}/v1/query/log?limit={limit}", timeout=5) as r:
            data = _json.loads(r.read())
    except urllib.error.HTTPError as e:
        click.echo(f"daemon 返 {e.code}: {e.read().decode()[:200]}", err=True)
        sys.exit(2)
    except Exception as e:
        click.echo(f"daemon 不可达 ({base}): {type(e).__name__}: {e}", err=True)
        sys.exit(2)
    if json_out:
        click.echo(_json.dumps(data, ensure_ascii=False, indent=2))
        return
    entries = data.get("entries", [])
    click.echo(f"=== 最近 {len(entries)} 条 query (limit={data.get('limit')}, total_logged={data.get('count')}) ===")
    for i, e in enumerate(entries, 1):
        ts = e.get("started_at", 0)
        from datetime import datetime
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
        click.echo(
            f"  [{i}] {time_str} {e.get('status','?')[:8]:<8} "
            f"tokens={e.get('tokens_used', 0):>5} "
            f"cache_hit={str(e.get('cache_hit', False))[:5]:<5} "
            f"q={e.get('query','')[:60]}"
        )


@cli.command("query-stats")
@click.option("--json-out", is_flag=True, help="输出 JSON")
@click.option("--daemon-base", default=None,
              help="daemon HTTP base URL (默认 localhost:8765)")
def query_stats(json_out, daemon_base):
    """T83: 查看 daemon query stats (cache + concurrency + LLM)."""
    import urllib.request
    import urllib.error
    import json as _json
    base = daemon_base or os.environ.get(
        "SB_DAEMON_BASE", f"http://127.0.0.1:{os.environ.get('SMOKE_PORT', '8765')}")
    try:
        with urllib.request.urlopen(f"{base}/v1/query/stats", timeout=5) as r:
            data = _json.loads(r.read())["data"]
    except Exception as e:
        click.echo(f"daemon 不可达 ({base}): {type(e).__name__}: {e}", err=True)
        sys.exit(2)
    if json_out:
        click.echo(_json.dumps(data, ensure_ascii=False, indent=2))
        return
    cache = data.get("cache", {})
    conc = data.get("concurrency", {})
    log_sum = data.get("query_log_summary", {})
    llm = data.get("llm", {})
    click.echo(f"=== daemon query stats ({base}) ===")
    click.echo(f"  cache:  hits={cache.get('hits')} misses={cache.get('misses')} hit_rate={cache.get('hit_rate')} size={cache.get('size')}/{cache.get('max_size')}")
    click.echo(f"  conc:   limit={conc.get('concurrency_limit')} available={conc.get('available_now')}")
    click.echo(f"  llm:    {llm.get('provider')} calls={llm.get('call_counts')}")
    if log_sum:
        click.echo(f"  log:    total={log_sum.get('total_logged')} success={log_sum.get('recent_success')}/{log_sum.get('recent_total')}")


@cli.command()
@click.argument("url")
@click.argument("keyword")
@click.option("--max-chars", default=4000, help="摘要最大字符数")
@click.option("--markdown", "as_md", is_flag=True, help="输出 Markdown 格式")
@click.option("--json-out", is_flag=True)
@click.option("--llm/--no-llm", default=None,
              help="启用/禁用 LLM 增强分类 (默认: OPENAI_API_KEY 存在时启用)")
def extract_topic(url, keyword, max_chars, as_md, json_out, llm):
    """从 url 抽取 keyword 相关的主题摘要。"""
    async def _run():
        from semantic_browser.engine import SemanticBrowser
        sb = SemanticBrowser(use_llm_classifier=_resolve_llm_flag(llm))
        await sb.start()
        try:
            data = await sb.extract_topic(url, keyword, max_chars=max_chars)
            if as_md and data.get("found"):
                # 重新渲染为 markdown 方便直接阅读
                from semantic_browser.extractor.content import ArticleContent
                result = await sb.browse(url)
                if result.article:
                    _emit(result.article.to_topic_markdown(keyword, max_chars=max_chars))
            elif json_out:
                _emit_json(data)
            else:
                _print_topic_result(data, keyword)
        finally:
            await sb.close()

    _run_async(_run())


def _emit(text: str) -> None:
    """输出纯文本到 stdout。"""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


@cli.command()
@click.argument("url", required=False)
@click.option("--no-headless", is_flag=True, help="显示浏览器窗口")
def interactive(url, no_headless):
    """打开页面并进入交互式 REPL。可选起始 URL。"""
    from semantic_browser.browser.controller import BrowserController, BrowserConfig
    from semantic_browser.cli.repl import REPLSession, parse_command

    async def _run():
        config = BrowserConfig(headless=not no_headless)
        controller = BrowserController(config)
        session = REPLSession(controller)
        try:
            await session._ensure_started()
            if url:
                # 不要走 parse_command: f"open {url}" 会让 shlex 把含 <>" 的 URL
                # (典型 data URL) 切成多段。直接构造 Command 保留 URL 完整。
                from semantic_browser.cli.repl import Command
                result = await session.execute(Command(name="open", args=[url], raw=f"open {url}"))
                session._render(result)
            await session.run_loop()
        finally:
            await session.close()

    _run_async(_run())


# ── 格式化输出 ──────────────────────────────────────────────

def _print_browse_result(result):
    """Rich 格式打印浏览结果。"""
    from semantic_browser.classifier.heuristic import ClassificationResult

    snap = result.snapshot
    cls = result.classification

    # 基本信息
    info = Table(show_header=False, box=None, padding=(0, 1))
    info.add_row("URL", snap.url)
    info.add_row("Title", snap.title)
    info.add_row("Type", f"{cls.page_type} ({cls.confidence:.0%})")
    info.add_row("Time", f"{result.elapsed:.2f}s")
    info.add_row("Blocks", str(len(snap.text_blocks)))
    info.add_row("Links", str(len(snap.links)))
    info.add_row("Controls", str(len(snap.controls)))
    console.print(Panel(info, title="📋 Page Snapshot", border_style="cyan"))

    # 分类信号
    if cls.signals:
        console.print(f"[dim]Signals: {', '.join(cls.signals)}[/dim]")
        console.print(f"[dim]Reason: {cls.reason}[/dim]")

    # 正文摘要
    if result.article and result.article.title:
        console.print()
        art_table = Table(show_header=False, box=None, padding=(0, 1))
        art_table.add_row("Title", result.article.title)
        if result.article.author:
            art_table.add_row("Author", result.article.author)
        if result.article.publish_date:
            art_table.add_row("Date", result.article.publish_date)
        art_table.add_row("Sections", str(len(result.article.sections)))
        art_table.add_row("Chars", str(result.article.word_count))
        art_table.add_row("Confidence", f"{result.article.extraction_confidence:.0%}")
        console.print(Panel(art_table, title="📄 Article", border_style="green"))

    # 接口摘要
    if result.interfaces:
        console.print()
        console.print(Panel(result.interfaces.summary(), title="🎛️  Interfaces", border_style="yellow"))

    # 链接列表（前 10 个）
    if snap.links:
        console.print()
        link_table = Table(title=f"🔗 Links ({len(snap.links)})", border_style="dim")
        link_table.add_column("Ref", style="cyan", width=10)
        link_table.add_column("Text", width=40)
        link_table.add_column("Internal", justify="center")
        for link in snap.links[:10]:
            link_table.add_row(
                link.ref, link.text[:40] or "(no text)",
                "✓" if link.internal else "✗",
            )
        console.print(link_table)
        if len(snap.links) > 10:
            console.print(f"  [dim]...and {len(snap.links) - 10} more[/dim]")

    # 控件列表（前 10 个）
    if snap.controls:
        console.print()
        ctrl_table = Table(title=f"🎛️  Controls ({len(snap.controls)})", border_style="dim")
        ctrl_table.add_column("Ref", style="cyan", width=10)
        ctrl_table.add_column("Kind", style="yellow", width=12)
        ctrl_table.add_column("Label", width=40)
        for ctrl in snap.controls[:10]:
            ctrl_table.add_row(ctrl.ref, ctrl.kind, ctrl.label[:40])
        console.print(ctrl_table)


def _print_article(article):
    """打印文章内容。"""
    console.print(Panel(article.title, title="📄 Title", border_style="green"))
    if article.author:
        console.print(f"Author: {article.author}")
    if article.publish_date:
        console.print(f"Date: {article.publish_date}")
    console.print()
    for section in article.sections:
        if section.get("heading"):
            console.print(f"\n[bold]{section['heading']}[/bold]")
        for para in section.get("paragraphs", []):
            console.print(para)
        for code in section.get("code_blocks", []):
            console.print(f"[dim]```{code[:200]}...```[/dim]")


def _print_pages_table(pages):
    """打印页面列表。data: URL 用占位符 (它们的 URL 长度可能几百字符污染表格)。"""
    table = Table(title="📚 Visited Pages", border_style="blue")
    table.add_column("Type", style="yellow", width=10)
    table.add_column("Title", width=40)
    table.add_column("URL", style="dim")
    table.add_column("Visits", justify="right")

    data_count = 0
    for p in pages:
        url = p.get("url", "") or ""
        if url.startswith("data:"):
            data_count += 1
            url = "(data: URL, hidden)"
        table.add_row(
            p.get("page_type", "?"),
            (p.get("title") or "")[:40],
            url[:60],
            str(p.get("visited_count", 1)),
        )
    console.print(table)
    if data_count:
        console.print(
            f"[dim]({data_count} data: URL entries hidden — they hold raw HTML and clutter the table)[/dim]"
        )


def _print_stats(stats_dict):
    """打印统计。"""
    table = Table(title="📊 Memory Stats", border_style="green")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold")
    for k, v in stats_dict.items():
        table.add_row(k.replace("_", " ").title(), str(v))
    console.print(table)


def _print_find_result(data: dict, keyword: str) -> None:
    """打印 find 命令的富文本结果。"""
    if not data.get("found"):
        console.print(f"[yellow](未找到关于 \"{keyword}\" 的内容)[/yellow]")
        return
    sections = data["sections"]
    total = data.get("total_sections", "?")
    console.print(f"[bold]在 {total} 个 section 中找到 {len(sections)} 个匹配 \"{keyword}\":[/bold]\n")
    for s in sections:
        console.print(Panel(
            f"[bold]{s['heading'] or '(无标题)'}[/bold]  [dim](score={s['score']})[/dim]\n\n"
            + "\n".join(f"  • {p[:200]}{'…' if len(p) > 200 else ''}"
                        for p in s["matched_paragraphs"][:3]),
            border_style="cyan",
        ))


def _print_topic_result(data: dict, keyword: str) -> None:
    """打印 extract-topic 命令的富文本结果。"""
    if not data.get("found"):
        console.print(f"[yellow](未找到关于 \"{keyword}\" 的内容)[/yellow]")
        return
    console.print(f"[bold cyan]关于 \"{keyword}\" 的摘要 ({data['section_count']} sections, {data['total_chars']} chars):[/bold cyan]\n")
    for s in data["sections"]:
        console.print(Panel(s["excerpt"], title=s["heading"] or "(无标题)",
                            border_style="green"))


def main():
    cli()


if __name__ == "__main__":
    main()
