"""Browser route handlers — extracted from server.py _dispatch.

Core browser operations (open, click, type, scroll, etc.),
debug operations (console, network, errors), tab/frame operations,
cookie/storage management, keyboard/focus operations, history/graph,
and utility endpoints (find, extract-topic, state/save, run-workflow).
"""

from __future__ import annotations

from typing import Any

from semantic_browser.daemon.routers import _register


# ── Browser core operations ────────────────────────────────────────

def handle_open(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /open — navigate to a URL."""
    return daemon.owner.run(daemon._open(
        args["url"], args.get("session"),
        detail=args.get("detail", "summary"),
        classify_force=str(args.get("classify", args.get("force", ""))).lower() in ("1", "true", "force"),
        classify_strict=str(args.get("strict", "")).lower() in ("1", "true"),
    ))


def handle_snapshot(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /snapshot — get DOM snapshot of current page."""
    return daemon.owner.run(daemon._snapshot(
        detail_level=args.get("detail_level", "normal"),
        session=args.get("session"),
    ))


def handle_snapshot_vision(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /snapshot-vision — vision-aware snapshot (screenshot + DOM)."""
    return daemon.owner.run(daemon._snapshot_vision(
        goal=args.get("goal", ""),
        provider=args.get("provider"),
        model=args.get("model"),
        full_page=str(args.get("full_page", "true")).lower() in ("1", "true"),
        session=args.get("session"),
    ))


def handle_read(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /read — read page content in markdown or plain text."""
    return daemon.owner.run(daemon._read(
        format=args.get("format", "markdown"),
        session=args.get("session"),
    ))


def handle_click(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /click — click an element by ref."""
    return daemon.owner.run(daemon._click(args["ref"], session=args.get("session")))


def handle_click_healed(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /click/healed — click with auto-healing selector fallback."""
    return daemon.owner.run(daemon._click_healed(args["ref"]))


def handle_type(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /type — type text into an element by ref."""
    return daemon.owner.run(daemon._type(args["ref"], args["text"], session=args.get("session")))


def handle_type_healed(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /type/healed — type with auto-healing selector fallback."""
    return daemon.owner.run(daemon._type_healed(args["ref"], args["text"]))


def handle_hover(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /hover — hover over an element by ref."""
    return daemon.owner.run(daemon._hover(args["ref"]))


def handle_dblclick(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /dblclick — double-click an element by ref."""
    return daemon.owner.run(daemon._dblclick(args["ref"]))


def handle_rightclick(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /rightclick — right-click an element by ref."""
    return daemon.owner.run(daemon._rightclick(args["ref"]))


def handle_drag(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /drag — drag from one element to another."""
    return daemon.owner.run(daemon._drag(args["from_ref"], args["to_ref"]))


def handle_drag_html5(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /drag/html5 — HTML5 drag-and-drop simulation."""
    return daemon.owner.run(daemon.owner.browser.controller.drag_html5(
        args["from_ref"], args["to_ref"],
    ))


def handle_select_option(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /select-option — select an option in a <select> element."""
    return daemon.owner.run(daemon._select_option(args["ref"], args["value"]))


def handle_fill_form(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /fill-form — fill multiple form fields at once."""
    return daemon.owner.run(daemon._fill_form(args["fields"]))


def handle_with_retry(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /with-retry — execute an action with retry logic.

    body: {"action": "click|type|open", "args": {...}, "max_retries": 2}

    T111 audit fix: SSRF guard — checks url in action_args for all actions.
    T99 audit fix: max_retries clamped to [0, 10].
    """
    action_name = args["action"]
    action_args = args.get("args", {})
    max_retries = int(args.get("max_retries", 2))
    # T111 audit fix: clamp max_retries to prevent lock timeout
    if max_retries < 0:
        max_retries = 0
    elif max_retries > 10:
        max_retries = 10
    # T99 (audit fix): SSRF guard for any action that may carry a url
    if "url" in action_args:
        daemon._check_url(action_args["url"], where=f"with_retry.{action_name}")
    return daemon.owner.run(daemon._with_retry(action_name, action_args, max_retries))


def handle_set_files(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /set-files — set file inputs on an element.

    T116 audit fix: resolves all paths through _safe_resolve_path to prevent
    arbitrary file reads via Playwright's set_input_files.
    """
    raw_paths = args["paths"]
    if not isinstance(raw_paths, list):
        raise ValueError("paths must be a list")
    safe_paths: list[str] = []
    for p in raw_paths:
        if not isinstance(p, str):
            raise ValueError(f"path must be a string (got {type(p).__name__})")
        safe_paths.append(daemon._safe_resolve_path(p, where="set_files"))
    return daemon.owner.run(daemon._set_files(args["ref"], safe_paths))


def handle_download(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /download — trigger a file download.

    T116 audit fix: save_to is resolved through _safe_resolve_path to prevent
    writing to arbitrary paths via Playwright's download_file.
    """
    save_to = args.get("save_to")
    if save_to is not None and not isinstance(save_to, str):
        raise ValueError("save_to must be a string")
    if save_to:
        save_to = daemon._safe_resolve_path(save_to, where="download")
    return daemon.owner.run(daemon._download(
        args.get("trigger_ref"),
        save_to,
        int(args.get("timeout_ms", 30000)),
    ))


def handle_scroll(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /scroll — scroll the page."""
    return daemon.owner.run(daemon._scroll(
        args.get("direction", "down"),
        int(args.get("amount", 500)),
    ))


def handle_press(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /press — press a keyboard key."""
    return daemon.owner.run(daemon._press(args["key"]))


def handle_back(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /back — navigate back in browser history."""
    return daemon.owner.run(daemon._back())


def handle_forward(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /forward — navigate forward in browser history."""
    return daemon.owner.run(daemon._forward())


def handle_screenshot(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /screenshot — take a screenshot.

    T111 audit fix: path is resolved through _safe_resolve_path to prevent
    arbitrary file writes.
    """
    spath: str | None = args.get("path")
    if spath:
        spath = daemon._safe_resolve_path(spath, where="screenshot")
    else:
        spath = None
    return daemon.owner.run(daemon._screenshot(spath))


def handle_screenshot_annotated(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /screenshot/annotated — take a screenshot with element annotations.

    T111 audit fix: path is resolved through _safe_resolve_path.
    """
    spath: str | None = args.get("path")
    if spath:
        spath = daemon._safe_resolve_path(spath, where="screenshot_annotated")
    else:
        spath = None
    return daemon.owner.run(daemon._screenshot_annotated(spath))


def handle_screenshot_sidecar(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /screenshot/sidecar — sidecar-only snapshot (no PNG), for LLM planning."""
    return daemon.owner.run(daemon._screenshot_sidecar())


# ── Wait / select operations ────────────────────────────────────────

def handle_wait_for_text(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /wait-for/text — wait for text to appear on the page."""
    return daemon.owner.run(daemon._wait_for_text(
        args["text"],
        int(args.get("timeout_ms", 10000)),
        args.get("in_selector", "body"),
    ))


def handle_wait_for_ref(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /wait-for/ref — wait for an element ref to appear."""
    return daemon.owner.run(daemon._wait_for_ref(
        args["ref"],
        int(args.get("timeout_ms", 10000)),
    ))


def handle_wait_for_url(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /wait-for/url — wait for URL to match a pattern."""
    return daemon.owner.run(daemon._wait_for_url(
        args["pattern"],
        int(args.get("timeout_ms", 10000)),
    ))


# ── Debug operations ────────────────────────────────────────────────

def handle_console(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /console — get buffered JS console messages.

    T18: Debug endpoint for agents to inspect console output.
    """
    type_filter = args.get("type") or None
    limit = int(args.get("limit", 100))
    return daemon.owner.browser.controller.get_console_messages(
        type_filter=type_filter, limit=limit,
    )


def handle_network(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /network — get buffered network requests.

    T18: Debug endpoint for agents to inspect network activity.
    """
    only_failed = args.get("only_failed", "false").lower() in ("1", "true", "yes")
    method_filter = args.get("method") or None
    limit = int(args.get("limit", 100))
    return daemon.owner.browser.controller.get_network_requests(
        only_failed=only_failed, method=method_filter, limit=limit,
    )


def handle_response_headers(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /response-headers — get response headers for a URL from network buffer.

    T39: Query response headers by URL from the network event buffer.
    T116 audit fix: SSRF check on URL to prevent internal network probing.
    """
    url = args.get("url", "")
    if not url:
        raise ValueError("url required")
    daemon._check_url(url, where="response_headers")
    return daemon.owner.run(daemon.owner.browser.controller.get_response_headers(url))


def handle_dom_diff(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /dom-diff — diff current DOM snapshot against a set of refs.

    T39: Compare current snapshot refs against a previously captured set.
    """
    refs_param = args.get("before_refs", "")
    before_refs = set(refs_param.split(",")) if refs_param else set()
    return daemon.owner.run(daemon.owner.browser.controller.get_dom_diff(before_refs))


def handle_script_source(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /script-source — fetch JS source by URL (deep mode).

    T39: Deep introspection — fetches JavaScript source for further analysis.
    T111 audit fix: SSRF check on URL to prevent internal network probing.
    """
    url = args.get("url", "")
    if not url:
        raise ValueError("url required")
    daemon._check_url(url, where="script_source")
    return daemon.owner.run(daemon.owner.browser.controller.fetch_script_source(url))


def handle_errors(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /errors — get buffered page errors (console.error, unhandled exceptions)."""
    limit = int(args.get("limit", 50))
    return daemon.owner.browser.controller.get_page_errors(limit=limit)


def handle_websockets(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /websockets — list active WebSocket connections.

    T40i: WebSocket connection inventory for debugging real-time communication.
    """
    limit = int(args.get("limit", 100))
    return daemon.owner.browser.controller.get_websockets(limit=limit)


def handle_debug_clear(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /debug/clear — clear the debug event buffer."""
    daemon.owner.browser.controller.clear_event_buffer()
    return {"cleared": True}


# ── Tab / frame operations ──────────────────────────────────────────

def handle_tab_list(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /tab/list — list all open tabs."""
    return daemon.owner.browser.controller.list_tabs()


def handle_tab_new(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /tab/new — open a new tab.

    T66.8: SSRF guard — url is checked before opening, matching /open behavior.
    """
    url = args.get("url", "")
    daemon._check_url(url, where="tab_new")
    return daemon.owner.run(daemon._tab_new(url))


def handle_tab_switch(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /tab/switch — switch to a tab by index."""
    idx = int(args["index"])
    return daemon.owner.run(daemon._tab_switch(idx))


def handle_tab_close(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /tab/close — close a tab by index (or current if not specified)."""
    idx = int(args["index"]) if "index" in args else None
    return daemon.owner.run(daemon._tab_close(idx))


def handle_frame_list(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /frame/list — list all frames in the current page."""
    return daemon.owner.run(daemon.owner.browser.controller.list_frames())


def handle_frame_switch(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /frame/switch — switch to a frame by name or URL.

    T110 audit fix: when name_or_url looks like a URL (contains ://, starts with
    // or /), it passes the SSRF check to prevent internal network probing.
    Frame names (e.g. "main", "iframe_0") are passed through directly.
    """
    name_or_url = args["name_or_url"]
    if "://" in name_or_url or name_or_url.startswith("//") or name_or_url.startswith("/"):
        daemon._check_url(name_or_url, where="frame_switch")
    return daemon.owner.run(daemon.owner.browser.controller.switch_frame(name_or_url))


def handle_frame_to_top(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /frame/to-top — navigate back to the top-level frame."""
    daemon.owner.run(daemon.owner.browser.controller.to_top_frame())
    return {"active": "main"}


# ── Cookie / storage operations ─────────────────────────────────────

def handle_cookies(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /cookies — get cookies, optionally filtered by URL."""
    url = args.get("url") or None
    return daemon.owner.run(daemon.owner.browser.controller.get_cookies(url))


def handle_cookies_set(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /cookies/set — set a cookie."""
    return daemon.owner.run(daemon.owner.browser.controller.set_cookie(
        name=args["name"], value=args["value"],
        url=args.get("url") or None,
        domain=args.get("domain") or None,
        path=args.get("path", "/"),
    ))


def handle_cookies_delete(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /cookies/delete — delete a cookie by name."""
    return daemon.owner.run(daemon.owner.browser.controller.delete_cookie(
        name=args["name"], url=args.get("url") or None,
    ))


def handle_cookies_clear(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /cookies/clear — clear all cookies."""
    n = daemon.owner.run(daemon.owner.browser.controller.clear_cookies())
    return {"cleared": n}


def handle_storage(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """[DEAD CODE — kept for reference]

    T17 read_storage with kind filtering. GET /storage is claimed by
    _security.py's handle_get_storage (T40a probe) which matches first in
    the original sequential dispatch and wins in the route table too.
    """
    kind = args.get("kind", "local")
    return daemon.owner.run(daemon.owner.browser.controller.read_storage(kind=kind))


def handle_storage_set(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /storage/set — set a value in browser storage (local or session)."""
    return daemon.owner.run(daemon.owner.browser.controller.set_storage(
        key=args["key"], value=args["value"], kind=args.get("kind", "local"),
    ))


def handle_storage_clear(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /storage/clear — clear browser storage (local or session)."""
    return daemon.owner.run(daemon.owner.browser.controller.clear_storage(
        kind=args.get("kind", "local"),
    ))


# ── Keyboard / focus operations ─────────────────────────────────────

def handle_focus_get(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /focus — get info about the currently focused element."""
    return daemon.owner.run(daemon.owner.browser.controller.get_focused_element())


def handle_focus(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /focus — focus an element by ref."""
    return daemon.owner.run(daemon.owner.browser.controller.focus(args["ref"]))


def handle_tab_key(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /tab — simulate Tab key press (with optional shift for reverse)."""
    shift = args.get("shift", "false").lower() in ("1", "true", "yes")
    count = int(args.get("count", 1))
    return daemon.owner.run(daemon.owner.browser.controller.tab(shift=shift, count=count))


def handle_keyboard_shortcut(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /keyboard/shortcut — press a keyboard shortcut (e.g. Ctrl+S)."""
    keys = args["keys"] if isinstance(args["keys"], list) else [args["keys"]]
    return daemon.owner.run(daemon.owner.browser.controller.keyboard_shortcut(*keys))


def handle_keyboard_type(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /keyboard/type — type text into the currently focused element."""
    text = args["text"]
    delay_ms = int(args.get("delay_ms", 0))
    return daemon.owner.run(daemon.owner.browser.controller.type_into_active(
        text, delay_ms=delay_ms,
    ))


# ── History / graph operations ──────────────────────────────────────

def handle_history(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /history — get visited page history, optionally filtered by domain."""
    pages = daemon._get_visited_pages(args.get("domain", ""))
    return {"pages": pages, "count": len(pages)}


def handle_graph(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """GET /graph — get the site link graph.

    If no url is specified, uses the current page URL.
    """
    url = args.get("url") or daemon.owner.run(daemon.owner.browser.controller.get_url())
    return daemon._get_site_graph(url).to_dict()


# ── Other utility operations ────────────────────────────────────────

def handle_find(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /find — search for a keyword across pages."""
    url = args["url"]
    keyword = args["keyword"]
    max_results = int(args.get("max_results", 10))
    return daemon.owner.run(daemon._find(url, keyword, max_results=max_results))


def handle_extract_topic(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /extract-topic — extract content matching a topic from a page."""
    url = args["url"]
    keyword = args["keyword"]
    max_chars = int(args.get("max_chars", 4000))
    return daemon.owner.run(daemon._extract_topic(url, keyword, max_chars=max_chars))


def handle_state_save(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /state/save — save browser storage state (cookies, localStorage, etc.).

    T111 audit fix: path is resolved through _safe_resolve_path to prevent
    writing auth tokens to attacker-controlled paths.
    """
    spath: str | None = args.get("path")
    if spath:
        spath = daemon._safe_resolve_path(spath, where="state_save")
    else:
        spath = None
    return daemon.owner.run(daemon._save_state(spath))


def handle_run_workflow(daemon: Any, args: dict[str, Any], req: Any) -> Any:
    """POST /run-workflow — execute a workflow from a JSON file.

    T111 audit fix: workflow_file path is resolved through _safe_resolve_path
    to prevent reading arbitrary files and bypassing SSRF guard via the "open"
    step.
    """
    wf_path = args["workflow_file"]
    wf_path = daemon._safe_resolve_path(wf_path, where="run_workflow")
    return daemon.owner.run(daemon._run_workflow(wf_path))


# ── Registration ────────────────────────────────────────────────────

# Core browser operations
_register("POST", "/open", handle_open)
_register("GET", "/snapshot", handle_snapshot)
_register("GET", "/snapshot-vision", handle_snapshot_vision)
_register("GET", "/read", handle_read)
_register("POST", "/click", handle_click)
_register("POST", "/click/healed", handle_click_healed)
_register("POST", "/type", handle_type)
_register("POST", "/type/healed", handle_type_healed)
_register("POST", "/hover", handle_hover)
_register("POST", "/dblclick", handle_dblclick)
_register("POST", "/rightclick", handle_rightclick)
_register("POST", "/drag", handle_drag)
_register("POST", "/drag/html5", handle_drag_html5)
_register("POST", "/select-option", handle_select_option)
_register("POST", "/fill-form", handle_fill_form)
_register("POST", "/with-retry", handle_with_retry)
_register("POST", "/set-files", handle_set_files)
_register("POST", "/download", handle_download)
_register("POST", "/scroll", handle_scroll)
_register("POST", "/press", handle_press)
_register("POST", "/back", handle_back)
_register("POST", "/forward", handle_forward)
_register("POST", "/screenshot", handle_screenshot)
_register("POST", "/screenshot/annotated", handle_screenshot_annotated)
_register("POST", "/screenshot/sidecar", handle_screenshot_sidecar)

# Wait / select operations
_register("POST", "/wait-for/text", handle_wait_for_text)
_register("POST", "/wait-for/ref", handle_wait_for_ref)
_register("POST", "/wait-for/url", handle_wait_for_url)

# Debug operations
_register("GET", "/console", handle_console)
_register("GET", "/network", handle_network)
_register("GET", "/response-headers", handle_response_headers)
_register("GET", "/dom-diff", handle_dom_diff)
_register("GET", "/script-source", handle_script_source)
_register("GET", "/errors", handle_errors)
_register("GET", "/websockets", handle_websockets)
_register("POST", "/debug/clear", handle_debug_clear)

# Tab / frame operations
_register("GET", "/tab/list", handle_tab_list)
_register("POST", "/tab/new", handle_tab_new)
_register("POST", "/tab/switch", handle_tab_switch)
_register("POST", "/tab/close", handle_tab_close)
_register("GET", "/frame/list", handle_frame_list)
_register("POST", "/frame/switch", handle_frame_switch)
_register("POST", "/frame/to-top", handle_frame_to_top)

# Cookie / storage operations
_register("GET", "/cookies", handle_cookies)
_register("POST", "/cookies/set", handle_cookies_set)
_register("POST", "/cookies/delete", handle_cookies_delete)
_register("POST", "/cookies/clear", handle_cookies_clear)
# GET /storage is registered by _security.py (T40a probe) — see handle_storage docstring
_register("POST", "/storage/set", handle_storage_set)
_register("POST", "/storage/clear", handle_storage_clear)

# Keyboard / focus operations
_register("GET", "/focus", handle_focus_get)
_register("POST", "/focus", handle_focus)
_register("POST", "/tab", handle_tab_key)
_register("POST", "/keyboard/shortcut", handle_keyboard_shortcut)
_register("POST", "/keyboard/type", handle_keyboard_type)

# History / graph operations
_register("GET", "/history", handle_history)
_register("GET", "/graph", handle_graph)

# Other utility operations
_register("POST", "/find", handle_find)
_register("POST", "/extract-topic", handle_extract_topic)
_register("POST", "/state/save", handle_state_save)
_register("POST", "/run-workflow", handle_run_workflow)
