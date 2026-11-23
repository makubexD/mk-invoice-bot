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

import dataclasses
import json
import logging
import re
import sys
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, TextIO, cast

from pydantic import BaseModel
from pydantic.fields import FieldInfo

#: Event fields copied from ``extra=`` onto the JSON object, in output order.
EXTRA_FIELDS = (
    "invoice_id",
    "job_id",
    "step",
    "rule_id",
    "duration_ms",
    "variant",
    "decoder",
    "expected",
    "actual",
)

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

#: HTTP client loggers kept at WARNING: their INFO request lines carry full URLs, such as a Graph
#: upload session's pre-authenticated ``uploadUrl`` and drive paths built from invoice data (R-9.16).
QUIET_LOGGERS = ("httpx", "httpcore")

#: Attributes the logging machinery owns; anything else came from ``extra=``.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

#: Keys the formatter writes; an ``extra=`` value never replaces one.
_OUTPUT_FIELDS = frozenset({"ts", "level", "event", "logger", "exc", *EXTRA_FIELDS})

#: ``extra=`` keys whose values are personal data (R-9.5, R-9.12): a sender id is logged only as a
#: hash, and names, phone numbers, RUCs and e-mail addresses are not logged at all. ``authorization``
#: and ``cookie`` keep a header mapping's credentials hidden (R-9.16): runtime tokens are not in the
#: loaded secret values, and walking ``httpx.Headers`` bypasses its own ``[secure]`` repr.
_PERSONAL_KEYS = frozenset(
    {
        "sender",
        "sender_id",
        "phone",
        "wa_id",
        "ruc",
        "dni",
        "razon_social",
        "email",
        "nombre",
        "authorization",
        "cookie",
    }
)
#: One word of a key: a run of lower-case letters or digits, an upper-case run, or a capitalized word.
_KEY_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

#: Replacement for a value under a personal-data key (R-9.5, R-9.12).
_PERSONAL_REDACTED = "[redacted]"

#: Deepest nesting a value is scrubbed to; anything deeper becomes ``_NESTED`` (R-9.12).
_SCRUB_MAX_DEPTH = 5

#: Replacement for a value nested deeper than ``_SCRUB_MAX_DEPTH`` (R-9.12).
_NESTED = "[nested]"

#: Replacement for an attribute that raises ``AttributeError`` when read (R-9.12).
_UNREADABLE = "[unreadable]"


class _AsDictTuple(Protocol):
    """A tuple that exposes its named fields, such as a ``NamedTuple``."""

    def _asdict(self) -> dict[str, Any]: ...


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


def _is_personal(key: str) -> bool:
    """True when ``key`` holds a personal key as a run of its words (R-9.12).

    Words are split at ``_``, ``-`` and camelCase humps, and match whole, so
    ``buyer_ruc``, ``phoneNumber``, ``wa-id`` and ``emisor_razon_social`` match
    while ``instructions`` does not.
    """
    words = "_".join(_KEY_WORD.findall(key)).lower()
    return any(f"_{personal}_" in f"_{words}_" for personal in _PERSONAL_KEYS)


def _read_field(value: Any, name: str) -> Any:
    """Return ``value.name``, or ``_UNREADABLE`` when the attribute is not set (R-9.12)."""
    try:
        return getattr(value, name)
    except AttributeError:
        return _UNREADABLE


def _field_key(name: str, field: FieldInfo) -> str:
    """Return the key a model field is logged under: a personal alias, else its name (R-9.12)."""
    for alias in (field.serialization_alias, field.alias):
        if alias is not None and _is_personal(alias):
            return alias
    return name


def _fields_of(value: Any) -> dict[str, Any] | None:
    """Return a dataclass instance's, NamedTuple's or pydantic model's fields, or ``None`` (R-9.12).

    A dataclass class has no field values, so only an instance converts. Fields
    are read, not copied with ``asdict``, so the caller's depth cap still
    applies to the values. A field declared ``repr=False`` is left out, as the
    class's own text form hides it (``SolCredentials``, R-9.16). A field that
    raises ``AttributeError`` on read becomes ``_UNREADABLE``; a model field
    whose alias is personal is keyed by that alias, so the dict branch redacts
    it. A NamedTuple is a tuple, so it converts with ``_asdict`` and keeps its
    field names instead of being treated as an anonymous tuple.
    """
    if isinstance(value, tuple) and hasattr(value, "_fields"):
        return cast(_AsDictTuple, value)._asdict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _read_field(value, field.name) for field in dataclasses.fields(value) if field.repr
        }
    if isinstance(value, BaseModel):
        shown = type(value).model_fields
        return {
            (_field_key(name, shown[name]) if name in shown else name): _read_field(value, name)
            for name, _ in value
            if name not in shown or shown[name].repr
        }
    return None


def _scrub_mapping(value: Mapping[Any, Any], secret_re: re.Pattern[str] | None, depth: int) -> dict[str, Any]:
    """Scrub a mapping key by key, redacting personal keys (R-9.12)."""
    return {
        _redact(str(key), secret_re): (
            _PERSONAL_REDACTED if _is_personal(str(key)) else _scrub(item, secret_re, depth + 1)
        )
        for key, item in value.items()
    }


def _scrub_items(items: Iterable[Any], secret_re: re.Pattern[str] | None, depth: int) -> list[Any]:
    """Scrub each item of a list, tuple or set (R-9.12)."""
    return [_scrub(item, secret_re, depth + 1) for item in items]


def _scrub(value: Any, secret_re: re.Pattern[str] | None, depth: int = 0) -> Any:
    """Redact personal keys and secrets in ``value`` down to ``_SCRUB_MAX_DEPTH`` (R-9.12).

    Dataclasses, NamedTuples, pydantic models and any mapping are scrubbed as
    their fields or keys; a set is scrubbed as a list sorted by text (R-9.12).
    """
    if depth >= _SCRUB_MAX_DEPTH:
        return _NESTED
    fields = _fields_of(value)
    if fields is not None:
        value = fields
    if isinstance(value, Mapping):
        return _scrub_mapping(value, secret_re, depth)
    if isinstance(value, str):
        return _redact(value, secret_re)
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return _scrub_items(value, secret_re, depth)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _redact(str(value), secret_re)


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
                payload[field] = _scrub(record.__dict__[field], self._secret_re)

        exc_text = record.exc_text
        if exc_text is None and record.exc_info is not None:
            exc_text = _format_exception(record.exc_info)
        if exc_text:
            payload["exc"] = _redact(exc_text, self._secret_re)

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in _OUTPUT_FIELDS:
                continue
            if _is_personal(key):
                payload[key] = _PERSONAL_REDACTED
                continue
            payload[key] = _scrub(value, self._secret_re)

        return json.dumps(payload, ensure_ascii=False)


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
        child.setLevel(logging.WARNING if name in QUIET_LOGGERS else logging.NOTSET)


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
