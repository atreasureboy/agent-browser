"""Circuit breaker for browser instances and sites.

§5.6: 熔断器 — 实例级 + 站点级.

实例熔断: 10min 内 ≥2 crash / context 创建失败率 >50% / ≥3 session 同窗 RECOVERING
站点熔断: 60s 窗导航失败率 ≥50% 且样本 ≥20, 或连续超时 ≥5

状态机: CLOSED → OPEN → HALF_OPEN → CLOSED
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """单个熔断器实例."""
    name: str
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_s: float = 30.0
    max_timeout_s: float = 240.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_ts: float = 0.0
    opened_at: float = 0.0
    current_timeout_s: float = 0.0  # 0 means use timeout_s

    def __post_init__(self):
        if self.current_timeout_s == 0.0:
            self.current_timeout_s = self.timeout_s

    def allow_request(self) -> bool:
        """是否允许请求通过."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.opened_at >= self.current_timeout_s:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                logger.info("Circuit %s: OPEN → HALF_OPEN", self.name)
                return True
            return False
        # HALF_OPEN: 允许探测请求
        return True

    def record_success(self) -> None:
        """记录成功."""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.current_timeout_s = self.timeout_s
                logger.info("Circuit %s: HALF_OPEN → CLOSED", self.name)
        elif self.state == CircuitState.CLOSED:
            # 成功时重置失败计数 (滑动窗口简化)
            self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self) -> None:
        """记录失败."""
        self.failure_count += 1
        self.last_failure_ts = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # HALF_OPEN 失败 → 回 OPEN, 超时翻倍
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            self.current_timeout_s = min(self.current_timeout_s * 2, self.max_timeout_s)
            logger.warning("Circuit %s: HALF_OPEN → OPEN (timeout=%.0fs)",
                          self.name, self.current_timeout_s)
        elif self.state == CircuitState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.time()
                # current_timeout_s 已在 __post_init__ 初始化
                logger.warning("Circuit %s: CLOSED → OPEN (failures=%d)",
                              self.name, self.failure_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_ts": self.last_failure_ts,
            "current_timeout_s": self.current_timeout_s,
        }


@dataclass
class CircuitBreakerRegistry:
    """管理多个熔断器."""
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def get_or_create(self, name: str, **kwargs) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(name=name, **kwargs)
        return self.breakers[name]

    def get(self, name: str) -> CircuitBreaker | None:
        return self.breakers.get(name)

    def check(self, name: str) -> bool:
        """检查是否允许请求."""
        cb = self.breakers.get(name)
        if cb is None:
            return True
        return cb.allow_request()

    def record_success(self, name: str) -> None:
        cb = self.breakers.get(name)
        if cb:
            cb.record_success()

    def record_failure(self, name: str) -> None:
        cb = self.breakers.get(name)
        if cb:
            cb.record_failure()

    def to_dict(self) -> dict[str, Any]:
        return {name: cb.to_dict() for name, cb in self.breakers.items()}
