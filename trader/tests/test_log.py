"""Tests for swing_trader.log — structured JSON logging + secret redaction."""

import io
import json

from swing_trader.log import REDACTED, get_logger, setup_logging


def capture(fmt: str = "json", level: str = "INFO") -> io.StringIO:
    buf = io.StringIO()
    setup_logging(level=level, fmt=fmt, stream=buf)
    return buf


def test_json_line_structure():
    buf = capture()
    get_logger("t").info("hello", extra={"symbol": "NVDA", "qty": 3})
    rec = json.loads(buf.getvalue().strip())
    assert rec["msg"] == "hello"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "t"
    assert rec["symbol"] == "NVDA"
    assert rec["qty"] == 3
    assert "ts" in rec


def test_secret_extras_redacted():
    """Loop.md §3: secrets never in logs."""
    buf = capture()
    get_logger("t").info(
        "auth",
        extra={"api_key": "sk-123", "telegram_bot_token": "999:zzz", "password": "p"},
    )
    line = buf.getvalue()
    assert "sk-123" not in line
    assert "999:zzz" not in line
    rec = json.loads(line.strip())
    assert rec["api_key"] == REDACTED
    assert rec["telegram_bot_token"] == REDACTED
    assert rec["password"] == REDACTED


def test_non_secret_extras_untouched():
    buf = capture()
    get_logger("t").info("x", extra={"limit_px": 101.5})
    assert json.loads(buf.getvalue().strip())["limit_px"] == 101.5


def test_level_filtering():
    buf = capture(level="WARNING")
    get_logger("t").info("should not appear")
    assert buf.getvalue() == ""


def test_exception_info_serialized():
    buf = capture()
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("t").exception("failed")
    rec = json.loads(buf.getvalue().strip())
    assert "ValueError: boom" in rec["exc"]


def test_non_json_serializable_extra_coerced():
    from pathlib import Path

    buf = capture()
    get_logger("t").info("x", extra={"path": Path("/tmp/x")})
    assert json.loads(buf.getvalue().strip())["path"] == "/tmp/x"


def test_console_format_smoke():
    buf = capture(fmt="console")
    get_logger("t").warning("plain text")
    assert "plain text" in buf.getvalue()


def test_setup_is_idempotent():
    buf1 = capture()
    buf2 = capture()  # replaces handler; no duplicate emission
    get_logger("t").info("once")
    assert buf1.getvalue() == ""
    assert len(buf2.getvalue().strip().splitlines()) == 1
