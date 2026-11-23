"""Tests for JSON logging, step timings and secret redaction (R-9.5, R-9.16)."""

from __future__ import annotations

import dataclasses
import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, NamedTuple

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from invoicebot.logging import PROPAGATING_LOGGERS, JsonFormatter, configure_logging, step_timer


@dataclasses.dataclass(frozen=True)
class _Invoice:
    """A dataclass value carrying a synthetic RUC and a count."""

    ruc: str
    count: int


@dataclasses.dataclass(frozen=True)
class _Node:
    """A dataclass that nests another value, for the depth test."""

    child: Any = None


class _Contact(BaseModel):
    """A pydantic model value with a personal field and a plain one."""

    phone_number: str
    label: str


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


def test_personal_key_inside_a_longer_name_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: an extra= key with a personal part, such as buyer_ruc, is written as [redacted]."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"buyer_ruc": "20000000001"})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["buyer_ruc"] == "[redacted]"
    assert "20000000001" not in output


def test_personal_key_as_a_later_part_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: a personal key used as the second part of the name, such as phone_number, matches."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"phone_number": "51999999999"})

    assert _records(log_stream)[-1]["phone_number"] == "[redacted]"


@pytest.mark.parametrize(
    "key", ["phoneNumber", "wa-id", "senderId", "emisor_razon_social", "razonSocial", "buyer_dni"]
)
def test_personal_key_in_another_spelling_is_redacted(log_stream: io.StringIO, key: str) -> None:
    """R-9.12: camelCase, hyphenated and multi-word personal keys match as a run of words."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={key: "51999999999"})

    assert _records(log_stream)[-1][key] == "[redacted]"


def test_non_string_dict_key_is_written_as_text(log_stream: io.StringIO) -> None:
    """R-9.12: a tuple or number key is turned into text, so the line is still written."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": {(1, 2): "a", 3: "b"}})

    assert _records(log_stream)[-1]["payload"] == {"(1, 2)": "a", "3": "b"}


def test_personal_key_inside_a_dict_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: a dict value is scrubbed key by key instead of being stringified."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": {"ruc": "20000000001", "count": 3}})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"ruc": "[redacted]", "count": 3}
    assert "20000000001" not in output


def test_personal_key_inside_a_list_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: a list value is scrubbed item by item instead of being stringified."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"items": [{"email": "a@example.com"}]})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["items"] == [{"email": "[redacted]"}]
    assert "a@example.com" not in output


def test_personal_part_redacts_a_count_under_that_key(log_stream: io.StringIO) -> None:
    """R-9.12: a key part matches whole: ``ruc_count`` is redacted, ``instructions`` is not."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info(
        "x", extra={"reason": "status", "ruc_count": 4, "instructions": "x"}
    )

    record = _records(log_stream)[-1]
    assert record["reason"] == "status"
    assert record["ruc_count"] == "[redacted]"
    assert record["instructions"] == "x"


def test_value_nested_past_the_cap_becomes_nested(log_stream: io.StringIO) -> None:
    """R-9.12: a value nested deeper than five levels is written as [nested]."""
    configure_logging(stream=log_stream)
    nested: Any = "deep"
    for _ in range(6):
        nested = {"level": nested}

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": nested})

    value = _records(log_stream)[-1]["payload"]
    for _ in range(5):
        value = value["level"]
    assert value == "[nested]"
    assert "deep" not in log_stream.getvalue()


def test_dataclass_value_is_scrubbed_field_by_field(log_stream: io.StringIO) -> None:
    """R-9.12: a dataclass in extra= is written as its fields, so its ruc is redacted."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _Invoice(ruc="20000000001", count=2)})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"ruc": "[redacted]", "count": 2}
    assert "20000000001" not in output


def test_pydantic_model_value_is_scrubbed_field_by_field(log_stream: io.StringIO) -> None:
    """R-9.12: a pydantic model in extra= is written as its fields, so phone_number is redacted."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info(
        "x", extra={"payload": _Contact(phone_number="51999999999", label="ops")}
    )

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"phone_number": "[redacted]", "label": "ops"}
    assert "51999999999" not in output


def test_dataclass_nested_past_the_cap_becomes_nested(log_stream: io.StringIO) -> None:
    """R-9.12: a dataclass nested deeper than five levels is written as [nested]."""
    configure_logging(stream=log_stream)
    nested: Any = _Node(child="deep")
    for _ in range(5):
        nested = _Node(child=nested)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": nested})

    value = _records(log_stream)[-1]["payload"]
    for _ in range(5):
        value = value["child"]
    assert value == "[nested]"
    assert "deep" not in log_stream.getvalue()


def test_dataclass_class_is_logged_as_text(log_stream: io.StringIO) -> None:
    """R-9.12: a dataclass class, not an instance, is logged as its str instead of its fields."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _Invoice})

    assert _records(log_stream)[-1]["payload"] == str(_Invoice)


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


@dataclasses.dataclass(frozen=True)
class _Hidden:
    """A dataclass shaped like ``SolCredentials``: short values its repr hides."""

    user: str = dataclasses.field(repr=False)
    label: str = "ops"


class _HiddenModel(BaseModel):
    """A pydantic model with a field its repr hides."""

    memo: str = Field(repr=False)
    label: str


def test_dataclass_field_hidden_from_repr_is_left_out(log_stream: io.StringIO) -> None:
    """R-9.12, R-9.16: a dataclass field declared repr=False never reaches the log, however short."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _Hidden(user="u7q")})

    assert _records(log_stream)[-1]["payload"] == {"label": "ops"}
    assert "u7q" not in log_stream.getvalue()


def test_pydantic_field_hidden_from_repr_is_left_out(log_stream: io.StringIO) -> None:
    """R-9.12, R-9.16: a pydantic field declared repr=False never reaches the log."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _HiddenModel(memo="t9z", label="ops")})

    assert _records(log_stream)[-1]["payload"] == {"label": "ops"}
    assert "t9z" not in log_stream.getvalue()


class _AliasedRuc(BaseModel):
    """A pydantic model whose field name hides a personal alias."""

    tax_id: str = Field(alias="ruc")
    label: str


@dataclasses.dataclass(frozen=True)
class _UnsetField:
    """A dataclass with an ``init=False`` field that is never assigned."""

    label: str = "ops"
    memo: str = dataclasses.field(init=False)


def test_pydantic_personal_alias_is_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: a model field aliased as ruc logs under ruc as [redacted], never its value."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info(
        "x", extra={"payload": _AliasedRuc(ruc="20000000001", label="ops")}
    )

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"ruc": "[redacted]", "label": "ops"}
    assert "20000000001" not in output


def test_field_that_raises_on_read_becomes_unreadable(log_stream: io.StringIO) -> None:
    """R-9.12: an attribute raising AttributeError on read is [unreadable], and the rest still log."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _UnsetField()})

    assert _records(log_stream)[-1]["payload"] == {"label": "ops", "memo": "[unreadable]"}


class _ExtraAllowed(BaseModel):
    """A pydantic model that keeps extra keys."""

    model_config = ConfigDict(extra="allow")

    label: str


def test_pydantic_extra_field_is_logged_and_redacted(log_stream: io.StringIO) -> None:
    """R-9.12: a model's extra keys log under their own names, a personal one redacted."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info(
        "x", extra={"payload": _ExtraAllowed(label="ops", ruc="20000000001")}
    )

    assert _records(log_stream)[-1]["payload"] == {"label": "ops", "ruc": "[redacted]"}
    assert "20000000001" not in log_stream.getvalue()


class _RucEntry(NamedTuple):
    """A named tuple carrying a synthetic RUC and a count."""

    ruc: str
    count: int


def test_namedtuple_keeps_field_names_and_redacts(log_stream: io.StringIO) -> None:
    """R-9.12: a NamedTuple in extra= logs under its field names, so ruc is redacted."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": _RucEntry(ruc="20000000001", count=2)})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"ruc": "[redacted]", "count": 2}
    assert "20000000001" not in output


def test_mapping_proxy_is_scrubbed_key_by_key(log_stream: io.StringIO) -> None:
    """R-9.12: a read-only mapping in extra= is scrubbed key by key, so phone_number is redacted."""
    configure_logging(stream=log_stream)

    logging.getLogger("invoicebot.demo").info(
        "x",
        extra={"payload": MappingProxyType({"phone_number": "51999999999", "count": 3})},
    )

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {"phone_number": "[redacted]", "count": 3}
    assert "51999999999" not in output


def test_frozenset_holding_a_secret_is_masked(log_stream: io.StringIO) -> None:
    """R-9.12: a frozenset in extra= is scrubbed item by item, so a loaded secret is masked."""
    secret = "synthetic-" + "secret-value-42"
    configure_logging(secret_values=[secret], stream=log_stream)

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": frozenset({secret, "public-value"})})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == ["public-value", "***"]
    assert secret not in output


def test_header_mapping_credentials_are_redacted(log_stream: io.StringIO) -> None:
    """R-9.16: a walked httpx.Headers hides its authorization and cookie values, as its own repr did."""
    configure_logging(stream=log_stream)
    headers = httpx.Headers(
        {"Authorization": "Bearer runtime-token-value", "Cookie": "sid=runtime-cookie", "Accept": "*/*"}
    )

    logging.getLogger("invoicebot.demo").info("x", extra={"payload": headers})

    output = log_stream.getvalue()
    assert _records(log_stream)[-1]["payload"] == {
        "authorization": "[redacted]",
        "cookie": "[redacted]",
        "accept": "*/*",
    }
    assert "runtime-token-value" not in output
    assert "runtime-cookie" not in output
