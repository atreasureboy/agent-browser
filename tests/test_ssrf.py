"""T58: SSRF guardrail unit tests (fable §7.1)."""
from __future__ import annotations

import pytest

from semantic_browser.safety.ssrf import (
    SSRFBlockedError,
    check_url,
)


class TestSSRFBlocks:
    def test_blocks_file_scheme(self):
        with pytest.raises(SSRFBlockedError, match="file"):
            check_url("file:///etc/passwd")

    def test_blocks_chrome_scheme(self):
        with pytest.raises(SSRFBlockedError, match="chrome"):
            check_url("chrome://settings/")

    def test_blocks_javascript_scheme(self):
        with pytest.raises(SSRFBlockedError, match="javascript"):
            check_url("javascript:alert(1)")

    def test_blocks_data_scheme(self):
        # data: 不是 http/https 也拒
        with pytest.raises(SSRFBlockedError):
            check_url("data:text/html,<script>alert(1)</script>")

    def test_blocks_loopback_ip(self):
        with pytest.raises(SSRFBlockedError, match="blocked IP"):
            check_url("http://127.0.0.1:8080/admin")

    def test_blocks_rfc1918(self):
        for url in ("http://10.0.0.1/api", "http://172.16.0.1/", "http://192.168.1.1/"):
            with pytest.raises(SSRFBlockedError, match="blocked IP"):
                check_url(url)

    def test_blocks_aws_metadata(self):
        with pytest.raises(SSRFBlockedError, match="blocked IP"):
            check_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_gcp_metadata_hostname(self):
        # 注意 GCP metadata 不需要 DNS 解析 (literal hostname)
        with pytest.raises(SSRFBlockedError):
            check_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_blocks_internal_tld(self):
        with pytest.raises(SSRFBlockedError, match="denied pattern"):
            check_url("http://server.internal/admin")

    def test_blocks_local_tld(self):
        with pytest.raises(SSRFBlockedError, match="denied pattern"):
            check_url("http://printer.local/")

    def test_blocks_localhost_tld(self):
        with pytest.raises(SSRFBlockedError, match="denied pattern"):
            check_url("http://something.localhost/")

    def test_blocks_empty_url(self):
        with pytest.raises(SSRFBlockedError, match="empty"):
            check_url("")

    def test_blocks_missing_scheme(self):
        with pytest.raises(SSRFBlockedError, match="scheme"):
            check_url("example.com/page")

    def test_blocks_unresolvable_host(self):
        # 不存在的域名 — 默认拒 (避免 error 状态穿过)
        with pytest.raises(SSRFBlockedError, match="could not resolve"):
            check_url("http://this-host-definitely-does-not-exist-12345.invalid/")


class TestSSRFAllows:
    def test_allows_public_https(self):
        # 公网域名 — 通过 (不 resolve, 单纯字符串 + 模式 pass)
        # 用 mock resolver 避开实际 DNS 查询
        result = check_url(
            "https://example.com/",
            resolver=lambda h: ["93.184.216.34"],
        )
        assert result == "https://example.com/"

    def test_allows_public_http(self):
        result = check_url(
            "http://example.com/page",
            resolver=lambda h: ["93.184.216.34"],
        )
        assert result == "http://example.com/page"

    def test_allows_public_ip(self):
        # 8.8.8.8 是公网
        result = check_url("http://8.8.8.8/")
        assert result == "http://8.8.8.8/"

    def test_allowlist_exact_host_passes_even_if_blocked(self):
        result = check_url(
            "http://internal.test/api",
            allowlist=frozenset({"internal.test"}),
        )
        assert result == "http://internal.test/api"

    def test_allowlist_wildcard_subdomain(self):
        result = check_url(
            "http://api.example.com/v1",
            allowlist=frozenset({"*.example.com"}),
        )
        assert result == "http://api.example.com/v1"

    def test_allowlist_wildcard_does_not_match_grandchild(self):
        # *.example.com 不该匹配 notexample.com
        with pytest.raises(SSRFBlockedError):
            check_url("http://notexample.com/", allowlist=frozenset({"*.example.com"}))

    def test_resolver_blocks_via_private_ip(self):
        # host 是公网但 DNS 解析到私网 (rebinding 防护)
        with pytest.raises(SSRFBlockedError, match="blocked IP"):
            check_url("http://evil.example.com/", resolver=lambda h: ["10.0.0.1"])

    def test_resolver_blocks_if_any_ip_blocked(self):
        # 多 A 记录 (DNS round-robin), 有一个是私网就拒
        with pytest.raises(SSRFBlockedError, match="blocked IP"):
            check_url(
                "http://multi.example.com/",
                resolver=lambda h: ["93.184.216.34", "127.0.0.1"],
            )

    def test_ipv6_blocks(self):
        # ::1 loopback, fs00:: ULA, fe80:: link-local — 至少 ::1 必挡
        for url in ("http://[::1]:8000/", "http://[fc00::1]/", "http://[fe80::1]/"):
            with pytest.raises(SSRFBlockedError, match="blocked IP"):
                check_url(url)

    def test_allows_ipv6_public(self):
        # 2001:4860:4860::8888 (Google DNS)
        result = check_url("http://[2001:4860:4860::8888]/dns-query")
        assert result == "http://[2001:4860:4860::8888]/dns-query"

    def test_blocked_ip_port_doesnt_matter(self):
        # 端口不影响判断
        with pytest.raises(SSRFBlockedError, match="blocked IP"):
            check_url("http://127.0.0.1:65535/")

    def test_case_insensitive_localhost_tld(self):
        with pytest.raises(SSRFBlockedError, match="denied pattern"):
            check_url("http://MyHost.LOCAL/")


class TestSSRFConfigChoices:
    """T58 测试 fixture 也得能跑 — daemon fixture 传 allowlist 绕过默认 deny."""

    def test_test_fixture_workaround_localhost_allowed(self):
        # 测试 fixture 使用 data: URL, 但需要能 allow某些host 才能跑
        result = check_url(
            "http://testserver.local/agent",
            allowlist=frozenset({"testserver.local", "*.example.com"}),
        )
        assert result == "http://testserver.local/agent"

    def test_whitespace_stripped(self):
        result = check_url(
            "  https://example.com/  ",
            resolver=lambda h: ["93.184.216.34"],
        )
        assert result == "https://example.com/"

    def test_uppercase_scheme_normalized(self):
        # scheme 都被 lower 后再比, 不因大小写绕过
        with pytest.raises(SSRFBlockedError, match="blocked"):
            check_url("FILE:///etc/passwd")
        with pytest.raises(SSRFBlockedError, match="blocked"):
            check_url("JavaScript:alert(1)")
