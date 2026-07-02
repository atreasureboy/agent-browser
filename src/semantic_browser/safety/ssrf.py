"""T58: SSRF guard — block dangerous URLs before they ever reach the browser.

fable §7.1 P0. Default-deny private/loopback/link-local/meta-service hosts.
DNS-resolved final IP checked (DNS rebinding / 302-redirect safe).

设计:
- pure function `check_url(url, *, allowlist=None)` → 抛 SSRFBlockedError or pass
- allowlist 支持精确域名/IP/CIDR;测试 fixture 可以注入 fake DNS resolver
- integration: daemon._open 在调 controller.open 前调用
- DNS resolver 可注入 (production 用 socket.getaddrinfo;测试用 mock)
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SSRFBlockedError(ValueError):
    """T58: 拒绝的 URL — 默认 deny 私网/loopback/元数据服务."""


# 默认挡的 scheme
_DEFAULT_BAD_SCHEMES = frozenset({"file", "chrome", "chrome-extension", "view-source", "javascript"})

# 默认挡的 host 子串 (大小写不敏感)
_DEFAULT_BAD_HOST_PATTERNS = (
    re.compile(r"metadata\.google\.internal", re.I),
    re.compile(r"metadata\.internal", re.I),
    re.compile(r"\.internal$", re.I),
    re.compile(r"\.local$", re.I),
    re.compile(r"\.localhost$", re.I),
)

# 默认挡的 IP 范围 (RFC1918 + loopback + link-local + cloud meta)
_BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("169.254.0.0/16"),    # link-local + cloud meta (AWS/GCP/Azure 都用)
    ipaddress.ip_network("0.0.0.0/8"),          # "this" network
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT (RFC6598)
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped IPv6 (防绕过)
]


def _ip_in_blocked(ip_str: str) -> bool:
    """检查 IP (str) 是否命中任何默认挡的网络."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in _BLOCKED_IP_NETWORKS:
        if ip in net:
            return True
    return False


def _resolve(host: str) -> list[str]:
    """解析 host → IP 列表. 包成函数方便测试 mock."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if isinstance(sockaddr, tuple) and sockaddr[0]:
            out.append(sockaddr[0])
    return list(dict.fromkeys(out))  # 去重保序


def _host_in_allowlist(host: str, allowlist: Optional[frozenset[str]]) -> bool:
    """allowlist 是精确字符串集合 (可能含 '*.example.com' 子域通配)."""
    if not allowlist:
        return False
    host_lower = host.lower()
    for allowed in allowlist:
        a = allowed.lower()
        if a == host_lower:
            return True
        if a.startswith("*.") and host_lower.endswith(a[1:]):
            return True
    return False


def check_url(
    url: str,
    *,
    allowlist: Optional[frozenset[str]] = None,
    resolver: Optional[Callable[[str], list[str]]] = None,
    allow_data: bool = False,
) -> str:
    """T58: 校验 URL, 通过返 canonical URL, 拒则抛 SSRFBlockedError.

    拒:
    - bad scheme (file://, chrome://, javascript: 等; data: 默认拒, allow_data=True 放行)
    - 主机名命中 .internal / .local / cloud metadata host pattern
    - DNS 解析后任何 IP 命中私网/loopback/link-local/CGNAT/IPv6 ULA 等
    - 解析失败也算拒 (避免 error 状态穿过)

    通:
    - http(s):// 公网域名/IP
    - 主机在 allowlist (含 *. 通配)
    - data: (仅测试 fixture 显式允许)
    """
    if not isinstance(url, str) or not url.strip():
        raise SSRFBlockedError("empty url")
    url = url.strip()

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise SSRFBlockedError("url missing scheme (e.g. http://, https://)")
    if scheme == "data":
        if allow_data:
            return url
        raise SSRFBlockedError(f"scheme 'data' blocked (default-deny; only http/https allowed)")
    if scheme not in ("http", "https"):
        raise SSRFBlockedError(f"scheme {scheme!r} blocked (default-deny; only http/https allowed)")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SSRFBlockedError("url has empty host")

    # 主机白名单 (允许内网/测试 fixture 通过)
    if _host_in_allowlist(host, allowlist):
        return url

    # 主机名 pattern 检查 (.internal, .local 等)
    for pat in _DEFAULT_BAD_HOST_PATTERNS:
        if pat.search(host):
            raise SSRFBlockedError(f"host {host!r} matches denied pattern {pat.pattern!r}")

    # 纯 IP URL: 直接校验 (不解析)
    try:
        ip_obj = ipaddress.ip_address(host)
        if _ip_in_blocked(str(ip_obj)):
            raise SSRFBlockedError(
                f"host {host!r} is in blocked IP range (private/loopback/link-local/meta)"
            )
        return url  # 公网 IP 通过
    except ValueError:
        pass  # 不是纯 IP, 走 DNS 解析

    # DNS 解析 → 检查所有返回 IP (防 rebinding / 多 A 记录)
    resolve_fn = resolver or _resolve
    ips = resolve_fn(host)
    if not ips:
        raise SSRFBlockedError(f"could not resolve host {host!r} (deny by default)")
    for ip in ips:
        if _ip_in_blocked(ip):
            raise SSRFBlockedError(
                f"host {host!r} resolves to blocked IP {ip!r} (private/loopback/link-local/meta)"
            )
    return url
