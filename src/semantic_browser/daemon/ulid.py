"""
ULID — 26 字符 time-ordered 唯一 ID (Crockford Base32).

T65.7: 多 agent 共享 daemon 时 lease_id / run_id 用 ULID — 比 UUID 短 12 字符,
且 time-prefix 让 seq 隐含时间序, 调试 log / SSE event id 友好.

格式: 26 chars = 10 chars timestamp (ms) + 16 chars randomness.
       例子: 01J3ZQ8K4F9X7Y6T3P2R8H4D1W

完全自包含 — 无外部依赖. 不追求 RFC 4122 兼容, 仅需本进程内唯一 + 时间序.

参考: github.com/ulid/spec (简化版, 不带 monotonic 随机保证).
"""

from __future__ import annotations

import os
import time

# Crockford Base32 字母表 (无 I/L/O/U 歧义字符).
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ALPHABET_DECODE = {c: i for i, c in enumerate(_ALPHABET)}


def _encode(num: int, length: int) -> str:
    """整数 → Base32 字符串 (固定长度, 高位补 0)."""
    if num < 0:
        raise ValueError(f"num must be non-negative, got {num}")
    chars = [_ALPHABET[0]] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _ALPHABET[num & 0x1F]
        num >>= 5
    return "".join(chars)


def _decode(s: str) -> int:
    """Base32 字符串 → 整数. 非法字符 → ValueError."""
    n = 0
    for c in s:
        if c not in _ALPHABET_DECODE:
            raise ValueError(f"invalid ULID character: {c!r}")
        n = (n << 5) | _ALPHABET_DECODE[c]
    return n


def ulid_new(ts_ms: int | None = None) -> str:
    """生成新 ULID.

    Args:
        ts_ms: 可选时间戳 (ms). 默认 time.time() * 1000.

    Returns:
        26 字符 ULID 字符串.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    if not (0 <= ts_ms < (1 << 50)):
        raise ValueError(f"ts_ms out of range: {ts_ms}")
    ts_part = _encode(ts_ms, 10)  # 50 bits = 10 chars in Base32
    # 随机部分: 80 bits = 16 chars, 用 os.urandom (CSPRNG, 不阻塞)
    rnd_bytes = os.urandom(10)  # 80 bits
    rnd_int = int.from_bytes(rnd_bytes, "big")
    rnd_part = _encode(rnd_int, 16)
    return ts_part + rnd_part


def ulid_timestamp(ulid: str) -> int:
    """从 ULID 提取时间戳 (ms)."""
    if len(ulid) != 26:
        raise ValueError(f"ULID must be 26 chars, got {len(ulid)}: {ulid!r}")
    return _decode(ulid[:10])


def ulid_validate(ulid: str) -> bool:
    """检查字符串是否为合法 ULID 格式."""
    if len(ulid) != 26:
        return False
    try:
        _decode(ulid)
        return True
    except ValueError:
        return False