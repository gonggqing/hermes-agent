"""Structured logging (stdlib-based, no extra deps).

JSON lines to a stream; ``extra={...}`` kwargs become top-level JSON fields.
A redaction filter masks any extra field whose key looks like a secret
(Loop.md §3: secrets never in logs).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import TextIO

_PROBE = logging.LogRecord(
    name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
)
_STDLIB_ATTRS = frozenset(_PROBE.__dict__) | {"message", "asctime", "taskName"}

_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|passwd|api_?key|credential|auth)", re.IGNORECASE
)

REDACTED = "***REDACTED***"


class SecretRedactingFilter(logging.Filter):
    """Mask extra fields whose key names look secret-bearing."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key not in _STDLIB_ATTRS and _SECRET_KEY_PATTERN.search(key):
                record.__dict__[key] = REDACTED
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STDLIB_ATTRS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure the root logger. Idempotent: replaces existing handlers."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
