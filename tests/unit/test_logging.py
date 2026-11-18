"""Tests for JSON logging, step timings and secret redaction (R-9.5, R-9.16)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from invoicebot.logging import PROPAGATING_LOGGERS, JsonFormatter, configure_logging, step_timer


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Route the root logger into a string stream and restore it afterwards."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_named = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in PROPAGATING_LOGGERS
    }
    stream = io.StringIO()
    try:
        yield stream
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, (handlers, propagate, level) in saved_named.items():
            child = logging.getLogger(name)
            child.handlers[:] = handlers
            child.propagate = propagate
            child.setLevel(level)


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse every emitted line as a JSON object."""
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_has_expected_fields(log_stream: io.StringIO) -> None:
    """R-9.5: a record is one JSON object with the fields and omits absent ones."""
    configure_logging(stream=log_stream)
    logging.getLogger("invoicebot.demo").info("job started", extra={"invoice_id": "inv-1", "job_id": "job-1"})

    records = _records(log_stream)

    assert len(records) == 1
    record = records[0]
    assert record["event"] == "job started"
    assert record["level"] == "INFO"
    assert record["logger"] == "invoicebot.demo"
    assert record["invoice_id"] == "inv-1"
    assert record["job_id"] == "job-1"
    assert datetime.fromisoformat(record["ts"]).tzinfo == UTC
    for absent in ("step", "rule_id", "duration_ms", "exc"):
        assert absent not in record


def test_step_timer_records_duration(log_stream: io.StringIO) -> None:
    """R-9.5: step_timer logs the step name and a non-negative integer duration."""
    configure_logging(stream=log_stream)
    log = logging.getLogger("invoicebot.timer")

    with step_timer("ocr", logger=log, invoice_id="inv-2"):
        pass

    record = _records(log_stream)[-1]
    assert record["event"] == "step done"
    assert record["step"] == "ocr"
    assert record["invoice_id"] == "inv-2"
    assert isinstance(record["duration_ms"], int)
    assert record["duration_ms"] >= 0


def test_step_timer_logs_failure_and_reraises(log_stream: io.StringIO) -> None:
    """R-9.5: a failing body logs step failed at ERROR and the exception propagates."""
    configure_logging(stream=log_stream)
    log = logging.getLogger("invoicebot.timer")

    with pytest.raises(RuntimeError, match="boom"), step_timer("parse", logger=log):
        raise RuntimeError("boom")

    record = _records(log_stream)[-1]
    assert record["event"] == "step failed"
    assert record["level"] == "ERROR"
    assert record["step"] == "parse"
    assert isinstance(record["duration_ms"], int)
    assert "RuntimeError: boom" in record["exc"]


def test_telegram_token_in_httpx_record_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.16: a token in an httpx request URL becomes bot*** even when it is a secret."""
    token = "bot" + "123456789" + ":" + "AAFakeToken_ForTests-abc"
    url = f"https://api.telegram.org/{token}/sendMessage"
    configure_logging(secret_values=[token], stream=log_stream)

    logging.getLogger("httpx").warning("HTTP Request: %s %s", "POST", url)

    output = log_stream.getvalue()
    assert token not in output
    assert "bot***" in output
    assert _records(log_stream)[-1]["logger"] == "httpx"


def test_httpx_request_lines_are_not_logged(log_stream: io.StringIO) -> None:
    """R-9.16: httpx and httpcore INFO lines, which carry full URLs, never reach the output."""
    configure_logging(stream=log_stream)
    url = "https://example.invalid/upload?tempauth=synthetic-capability"

    logging.getLogger("httpx").info("HTTP Request: %s %s", "PUT", url)
    logging.getLogger("httpcore").info("send_request_headers.started %s", url)

    assert "tempauth" not in log_stream.getvalue()


def test_secret_is_redacted_in_args_extra_and_exception(log_stream: io.StringIO) -> None:
    """R-9.16: secrets in args, extra fields and tracebacks never reach the output."""
    secret = "synthetic-" + "secret-value-42"
    configure_logging(secret_values=[secret], stream=log_stream)
    log = logging.getLogger("invoicebot.demo")

    log.info("token is %s", secret)
    log.info("rule matched", extra={"rule_id": secret})
    try:
        raise ValueError(f"bad token {secret}")
    except ValueError:
        log.exception("handler failed")

    output = log_stream.getvalue()
    records = _records(log_stream)
    assert secret not in output
    assert "***" in records[0]["event"]
    assert records[1]["rule_id"] == "***"
    assert "***" in records[2]["exc"]


def test_short_value_is_not_a_secret(log_stream: io.StringIO) -> None:
    """R-9.16: values shorter than six characters are left in the output."""
    configure_logging(secret_values=["abc12"], stream=log_stream)

    logging.getLogger("invoicebot.demo").info("value %s", "abc12")

    assert "abc12" in log_stream.getvalue()


def test_extra_field_is_rendered(log_stream: io.StringIO) -> None:
    """R-9.5: an extra= key that is not a standard attribute appears under its own key."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").warning("backup failed", extra={"reason": "dump"})

    assert _records(log_stream)[-1]["reason"] == "dump"


def test_extra_string_value_is_redacted() -> None:
    """R-9.5: a secret in an extra string is redacted by the formatter itself."""
    secret = "synthetic-" + "secret-value-42"
    record = logging.LogRecord("invoicebot.demo", logging.INFO, "", 0, "failed", (), None)
    record.__dict__["detail"] = f"token {secret}"

    line = JsonFormatter(secret_values=[secret]).format(record)

    assert json.loads(line)["detail"] == "token ***"


@pytest.mark.parametrize("key", ["sender", "Sender", "PHONE"])
def test_personal_extra_key_is_redacted(log_stream: io.StringIO, key: str) -> None:
    """R-9.12: an extra= value under a personal-data key, in any case, is written as [redacted]."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={key: "51999999999"})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1][key] == "[redacted]"
    assert "51999999999" not in output


def test_non_personal_extra_key_is_written(log_stream: io.StringIO) -> None:
    """R-9.12: an extra= key outside the personal-data set is written as is."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"reason": "status"})

    assert _records(log_stream)[-1]["reason"] == "status"


def test_extra_number_stays_a_number(log_stream: io.StringIO) -> None:
    """R-9.5: an extra= int value is emitted as a JSON number, not a string."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("backup done", extra={"count": 3})

    assert _records(log_stream)[-1]["count"] == 3


def test_extra_does_not_replace_a_formatter_field(log_stream: io.StringIO) -> None:
    """R-9.5: an extra= key equal to a formatter field leaves the field unchanged."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("real event", extra={"event": "spoofed"})

    assert _records(log_stream)[-1]["event"] == "real event"


def test_configure_logging_twice_keeps_one_handler(log_stream: io.StringIO) -> None:
    """R-9.5: configure_logging is idempotent and replaces existing handlers."""
    configure_logging(stream=log_stream)
    configure_logging(stream=log_stream)

    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger("httpx").handlers == []
    assert logging.getLogger("httpx").propagate is True
    logging.getLogger("invoicebot.demo").info("once")
    assert len(_records(log_stream)) == 1
