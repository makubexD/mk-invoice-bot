"""R-9.16: a read conversation logs no extracted value (S3.11)."""

import logging
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Positions in a SUNAT QR payload.
_ISSUER, _SERIE, _NUMERO, _TOTAL, _BUYER = 0, 2, 3, 5, 8


def _secrets_of(payload: str) -> list[str]:
    """Return the values no log record may carry: RUCs, serie-número, total and the QR text."""
    parts = payload.split("|")
    return [parts[_ISSUER], parts[_BUYER], f"{parts[_SERIE]}-{parts[_NUMERO]}", parts[_TOTAL], payload]


def _record_text(record: logging.LogRecord) -> str:
    """Return the record's message and every attribute, extras included, as one string."""
    return " ".join([record.getMessage(), *(str(value) for value in vars(record).values())])


async def test_read_conversations_log_no_extracted_value(
    read_conversation: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """R-9.16: neither a QR nor a blank conversation logs a RUC, serie-número, total or QR text."""
    caplog.set_level(logging.DEBUG)

    _, payload = await read_conversation(True)
    await read_conversation(False)

    assert any(getattr(record, "step", None) == "route" for record in caplog.records)
    leaks = [
        (record.name, secret)
        for record in caplog.records
        for secret in _secrets_of(payload)
        if secret in _record_text(record)
    ]
    assert leaks == []
