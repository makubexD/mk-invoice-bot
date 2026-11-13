"""Structured JSON logs, step timings and secret redaction (R-9.5, R-9.16).

Every log record is rendered as one JSON object per line so logs can be
grepped and shipped without a parser. Records carry ``ts`` (UTC ISO 8601),
``level``, ``event``, ``logger`` and, when supplied through ``extra=``, the
correlation fields ``invoice_id``, ``job_id``, ``step``, ``rule_id`` and
``duration_ms``; tracebacks go in ``exc``.

Redaction happens on the handler, before formatting, so records propagated
from third-party loggers (``httpx``, ``httpcore``, ``uvicorn``) are covered
too. Every loaded secret value (six characters or longer) and every Telegram
bot token URL segment becomes ``***`` / ``bot***``; the formatter redacts a
second time in case a record reaches it without passing the filter.

This module deliberately imports the standard library with absolute imports;
it must not import itself instead of :mod:`logging`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TextIO

#: Event fields copied from ``extra=`` onto the JSON object, in output order.
EXTRA_FIELDS = ("invoice_id", "job_id", "step", "rule_id", "duration_ms", "variant", "decoder")

#: Replacement for a configured secret value (R-9.16).
REDACTED = "***"

#: Replacement for the token part of a Telegram bot URL (R-9.16).
REDACTED_BOT = "bot***"

#: Values shorter than this are common words and would corrupt the log (R-9.16).
MIN_SECRET_LENGTH = 6

#: ``https://api.telegram.org/bot<digits>:<token>/...`` (R-9.16).
BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

#: Loggers that must feed the root handler instead of writing on their own.
PROPAGATING_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")

#: Attributes the logging machinery owns; anything else came from ``extra=``.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

_logger = logging.getLogger(__name__)


def _secret_pattern(secret_values: Iterable[str]) -> re.Pattern[str] | None:
    """Compile one longest-first alternation of the values worth redacting."""
    usable = {value for value in secret_values if len(value) >= MIN_SECRET_LENGTH}
    if not usable:
        return None
    ordered = sorted(usable, key=len, reverse=True)
    return re.compile("|".join(re.escape(value) for value in ordered))


def _redact(text: str, secret_re: re.Pattern[str] | None) -> str:
    """Replace bot token segments, then configured secret values."""
    # The token goes first: a token that is also a configured secret must still
    # read "bot***" rather than being swallowed whole as "***".
    text = BOT_TOKEN_RE.sub(REDACTED_BOT, text)
    if secret_re is not None:
        text = secret_re.sub(REDACTED, text)
    return text


def _format_exception(exc_info: Any) -> str:
    """Render ``exc_info`` the way the standard formatter would."""
    return logging.Formatter().formatException(exc_info)


class RedactionFilter(logging.Filter):
    """Rewrite a record so no secret reaches any handler (R-9.16).

    The message and its arguments are rendered first, so secrets passed as
    ``%s`` arguments are caught. Exception text and string values supplied via
    ``extra=`` are redacted as well. Attach this filter to handlers rather than
    loggers: filters on a logger never see records propagated from elsewhere.
    """

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        super().__init__()
        self._secret_re = _secret_pattern(secret_values)

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact ``record`` in place and let it through."""
        record.msg = _redact(record.getMessage(), self._secret_re)
        record.args = ()

        if record.exc_info is not None:
            record.exc_text = _redact(_format_exception(record.exc_info), self._secret_re)
        elif record.exc_text:
            record.exc_text = _redact(record.exc_text, self._secret_re)

        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                record.__dict__[key] = _redact(value, self._secret_re)
        return True


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object with a stable set of fields (R-9.5)."""

    def __init__(self, *, secret_values: Iterable[str] = ()) -> None:
        super().__init__()
        # Second line of defense for records that bypass RedactionFilter.
        self._secret_re = _secret_pattern(secret_values)

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single-line JSON object."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "event": _redact(record.getMessage(), self._secret_re),
            "logger": record.name,
        }
        for field in EXTRA_FIELDS:
            if field in record.__dict__:
                payload[field] = _redact_value(record.__dict__[field], self._secret_re)

        exc_text = record.exc_text
        if exc_text is None and record.exc_info is not None:
            exc_text = _format_exception(record.exc_info)
        if exc_text:
            payload["exc"] = _redact(exc_text, self._secret_re)

        return json.dumps(payload, ensure_ascii=False)


def _redact_value(value: Any, secret_re: re.Pattern[str] | None) -> Any:
    """Redact a string value; pass any other type through untouched."""
    return _redact(value, secret_re) if isinstance(value, str) else value


def configure_logging(
    secret_values: Iterable[str] = (),
    *,
    level: str | int = "INFO",
    stream: TextIO | None = None,
) -> None:
    """Install one JSON handler on the root logger (R-9.5, R-9.16).

    Existing root handlers are replaced, so calling this twice leaves exactly
    one handler. The named third-party loggers propagate to the root instead of
    keeping handlers (and duplicate or unredacted output) of their own.
    """
    values = tuple(secret_values)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter(secret_values=values))
    handler.addFilter(RedactionFilter(values))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in PROPAGATING_LOGGERS:
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True
        child.setLevel(logging.NOTSET)


def _duration_ms(started: float) -> int:
    """Whole milliseconds elapsed since ``started`` on the perf clock."""
    return int((time.perf_counter() - started) * 1000)


@contextmanager
def step_timer(
    step: str,
    *,
    logger: logging.Logger | None = None,
    **fields: Any,
) -> Iterator[None]:
    """Log how long ``step`` took, and its failure if the body raises (R-9.5).

    ``fields`` are copied onto both events, so callers can correlate a step
    with its invoice or job.
    """
    log = logger if logger is not None else _logger
    started = time.perf_counter()
    try:
        yield
    except Exception:  # cancellation and shutdown are not step failures
        log.error(
            "step failed",
            extra={**fields, "step": step, "duration_ms": _duration_ms(started)},
            exc_info=True,
        )
        raise
    log.info(
        "step done",
        extra={**fields, "step": step, "duration_ms": _duration_ms(started)},
    )
