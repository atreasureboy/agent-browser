"""Metrics registry — per-request counters + histograms with Prometheus output.

Extracted from server.py.
Thread-safe (multiple HTTP threads).
"""
from __future__ import annotations

from typing import Any


class MetricsRegistry:
    """Request-level metrics — counters + histograms (fixed buckets).

    Prometheus text format output. Collected in _handle hooks, exposed via /metrics.
    Thread-safe (one daemon, multiple HTTP threads).
    """

    _LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

    def __init__(self) -> None:
        import threading as _threading
        self._lock = _threading.Lock()
        # {label_key: count} — e.g. ("GET", "/open", "200") → N
        self._counters: dict[tuple[str, str, str], int] = {}
        # {label_key: {"count": N, "sum": S, "buckets": [cumulative]}}
        self._histograms: dict[tuple[str, str], dict[str, Any]] = {}

    def _labels(self, labels: dict[str, str]) -> str:
        return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))

    def inc(self, name: str, labels: dict[str, str], value: int = 1) -> None:
        key = tuple(sorted(labels.items()))
        full_key = (name, key)
        with self._lock:
            self._counters[full_key] = self._counters.get(full_key, 0) + value

    def observe(self, name: str, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        full_key = (name, key)
        with self._lock:
            h = self._histograms.get(full_key)
            if h is None:
                h = {
                    "count": 0,
                    "sum": 0.0,
                    "buckets": [0] * len(self._LATENCY_BUCKETS),
                }
                self._histograms[full_key] = h
            h["count"] += 1
            h["sum"] += value
            for i, b in enumerate(self._LATENCY_BUCKETS):
                if value <= b:
                    h["buckets"][i] += 1

    def render_prometheus(self) -> str:
        """Prometheus text format (0.0.4). E.g.:

          tb_requests_total{method="GET",path="/open",status="200"} 42
          tb_request_duration_seconds_bucket{method="GET",path="/open",le="0.5"} 38
        """
        lines: list[str] = []
        with self._lock:
            # counters — group by metric name
            counter_names = sorted({name for (name, _) in self._counters})
            for name in counter_names:
                lines.append(f"# TYPE tb_{name} counter")
                for (n, labels), value in sorted(self._counters.items()):
                    if n != name:
                        continue
                    label_str = self._labels(dict(labels))
                    lines.append(f"tb_{n}_total{{{label_str}}} {value}")
            # histograms
            hist_names = sorted({name for (name, _) in self._histograms})
            for name in hist_names:
                lines.append(f"# TYPE tb_{name} histogram")
                for (n, labels), h in sorted(self._histograms.items()):
                    if n != name:
                        continue
                    label_str = self._labels(dict(labels))
                    running = 0
                    for i, b in enumerate(self._LATENCY_BUCKETS):
                        running = h["buckets"][i]
                        lines.append(
                            f'tb_{n}_bucket{{{label_str},le="{b}"}} {running}'
                        )
                    lines.append(f'tb_{n}_bucket{{{label_str},le="+Inf"}} {h["count"]}')
                    lines.append(f'tb_{n}_count{{{label_str}}} {h["count"]}')
                    lines.append(f'tb_{n}_sum{{{label_str}}} {h["sum"]:.6f}')
        return "\n".join(lines) + "\n"
