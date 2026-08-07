"""Structured logging setup — super_plan Round 3b.

Provides a JSON formatter for production (machine-readable, easy to grep/aggregate)
and a console formatter for development. Call ``setup_logging()`` once at startup;
all modules use the standard ``logging.getLogger(__name__)`` as before.
"""
from __future__ import annotations

import json as _json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Structured JSON log lines for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage() % record.args if isinstance(record.msg, str) and "%" in record.msg and record.args else record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        if record.exc_info and record.exc_info[1]:
            payload["exc"] = self.formatException(record.exc_info)
        return _json.dumps(payload, default=str, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable for development (timestamp, level, logger, message)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return f"{ts} [{record.levelname[0]}] {record.name}: {record.getMessage()}"


def setup_logging(
    *,
    level: str = "INFO",
    json_mode: bool = False,
    verbose: bool = False,
) -> None:
    """Configure the root logger once at application start.

    Args:
        level: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``.
        json_mode: If True, emit structured JSON lines (production).
        verbose: Shorthand for ``level='DEBUG'`` + console mode.
    """
    if verbose:
        level = "DEBUG"
    fmt: logging.Formatter = _JsonFormatter() if json_mode else _ConsoleFormatter()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Quiet noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio",
                  "playwright", "tldextract", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
