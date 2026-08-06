"""Safety guard wiring tests — super_plan Round 2c.

The daemon's interactive endpoints (/click /type /drag ...) must pass through
the destructive-action guard: dangerous targets return CONFIRM_REQUIRED (HTTP
409) until the caller re-submits with confirm_destructive=true.
"""
from __future__ import annotations

import json
import urllib.request
from urllib.error import HTTPError

from tests.test_daemon import _http, daemon  # noqa: F401  (fixture reuse)

_DELETE_BUTTON_PAGE = (
    "data:text/html,"
    "<html><body>"
    "<button id='b1' data-sb-ref='e1'>Delete account</button>"
    "<input id='i1' data-sb-ref='e2' placeholder='comment'/>"
    "</body></html>"
)


class TestDaemonGuardClick:
    def test_click_destructive_label_requires_confirm(self, daemon):
        r0 = _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        assert r0["ok"] is True
        r = _http("POST", f"{daemon}/click", {"ref": "e1"})
        assert r["ok"] is False
        assert r["error"]["code"] == "CONFIRM_REQUIRED"
        assert r["error"]["retryable"] is False

    def test_click_destructive_returns_http_409(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        req = urllib.request.Request(
            f"{daemon}/click",
            data=json.dumps({"ref": "e1"}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=30)
            raise AssertionError("expected HTTP 409 for unconfirmed destructive click")
        except HTTPError as e:
            assert e.code == 409
            assert json.loads(e.read())["error"]["code"] == "CONFIRM_REQUIRED"

    def test_click_with_confirm_destructive_passes(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/click", {"ref": "e1", "confirm_destructive": True})
        assert r["ok"] is True

    def test_click_harmless_label_passes(self, daemon):
        page = (
            "data:text/html,<html><body>"
            "<button data-sb-ref='e1'>Expand details</button></body></html>"
        )
        _http("POST", f"{daemon}/open", {"url": page})
        r = _http("POST", f"{daemon}/click", {"ref": "e1"})
        assert r["ok"] is True


class TestDaemonGuardType:
    def test_type_destructive_text_requires_confirm(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/type", {"ref": "e2", "text": "rm -rf /"})
        assert r["ok"] is False
        assert r["error"]["code"] == "CONFIRM_REQUIRED"

    def test_type_harmless_text_passes(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/type", {"ref": "e2", "text": "hello world"})
        assert r["ok"] is True

    def test_type_destructive_with_confirm_passes(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/type",
                  {"ref": "e2", "text": "drop table users", "confirm_destructive": True})
        assert r["ok"] is True


class TestDaemonGuardDrag:
    def test_drag_to_trash_requires_confirm(self, daemon):
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/drag", {"from_ref": "e1", "to_ref": "trash-can"})
        assert r["ok"] is False
        assert r["error"]["code"] == "CONFIRM_REQUIRED"

    def test_drag_to_trash_with_confirm_passes(self, daemon):
        """confirm_destructive 放行 — drag 本身会失败 (无元素) 但守卫不拦."""
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        r = _http("POST", f"{daemon}/drag",
                  {"from_ref": "e1", "to_ref": "trash-can", "confirm_destructive": True})
        assert r.get("ok", False) is True or r["error"]["code"] != "CONFIRM_REQUIRED"


class TestGuardErrorClassification:
    def test_classify_exception_maps_confirm_required(self):
        from semantic_browser.result import classify_exception
        from semantic_browser.safety import SafetyGuardError
        out = classify_exception(SafetyGuardError("click() target label contains: 'delete'"))
        assert out["ok"] is False
        assert out["error"]["code"] == "CONFIRM_REQUIRED"
        assert out["error"]["retryable"] is False

    def test_get_ref_label_returns_label(self, daemon):
        """get_ref_label 是守卫判断 click 目标的依据 — 通过真实页面验证."""
        _http("POST", f"{daemon}/open", {"url": _DELETE_BUTTON_PAGE})
        # 经由 /click 的行为间接验证已在上面覆盖; 这里直接调 controller API
        # 走 daemon 没有直接端点, 用 type 守卫对 aria-label 的识别替代验证:
        page = (
            "data:text/html,<html><body>"
            "<button data-sb-ref='e1' aria-label='Remove item'>&times;</button>"
            "</body></html>"
        )
        _http("POST", f"{daemon}/open", {"url": page})
        r = _http("POST", f"{daemon}/click", {"ref": "e1"})
        assert r["ok"] is False
        assert r["error"]["code"] == "CONFIRM_REQUIRED"
