"""Unified structured logging for the clipboard Git tunnel.

Log line format (console and file), timestamps in Beijing time (UTC+8):

    2026-08-28 14:30:05 | INFO | A | event.name | key=value ...

Sensitive request headers and clipboard/frame payloads must never be passed to
``log_event``. Only operational metadata (sizes, status, session prefix, timing)
is recorded.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

# Beijing time is UTC+8 year-round (no DST); a fixed offset avoids depending on
# IANA tzdata being present in the embeddable Windows Python.
_BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


class BeijingFormatter(logging.Formatter):
    """Logging formatter with Beijing-time ``yyyy-MM-dd HH:mm:ss`` stamps."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, tz=_BEIJING_TZ).strftime(
            "%Y-%m-%d %H:%M:%S")


def _render(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit one stable event name followed by sorted ``key=value`` fields."""

    details = " ".join(f"{key}={_render(fields[key])}" for key in sorted(fields))
    logger.log(level, event if not details else f"{event} | {details}")


def log_exception(logger: logging.Logger, event: str, exc: BaseException,
                  **fields: Any) -> None:
    """Emit one structured error event without multiline tracebacks."""

    fields = {**fields, "error_type": type(exc).__name__, "error": str(exc)}
    details = " ".join(f"{key}={_render(fields[key])}" for key in sorted(fields))
    logger.error(event if not details else f"{event} | {details}")


def safe_http_path(path: str, max_length: int = 512) -> str:
    """Keep request logging useful without recording URL userinfo."""

    try:
        parsed = urlsplit(path)
        cleaned = urlunsplit(("", "", parsed.path, parsed.query, ""))
    except ValueError:
        cleaned = path
    return cleaned[:max_length]


def setup_logging(
    *,
    side: str,
    project_root: Path,
    log_level: str = "INFO",
    log_dir: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> tuple[logging.Logger, Path]:
    """Configure one side's console + rotating file handlers.

    Repeated calls for the same side replace handlers so tests and embedded
    entrypoints do not duplicate log lines.
    """

    side = side.upper()
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"invalid log level: {log_level}")

    resolved_dir = (log_dir or (project_root / "logs")).resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    log_path = resolved_dir / f"{side.lower()}-tunnel.log"

    logger = logging.getLogger(f"clipboard_git_tunnel.{side}")
    logger.setLevel(numeric_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = BeijingFormatter(
        fmt="%(asctime)s | %(levelname)s | " + side + " | %(message)s"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, log_path
