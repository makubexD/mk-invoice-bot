"""D193, R-5.6: a lone portal PDF is kept only when it reads as the stored invoice."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.pipeline.probe_files import pdf_matches

pytestmark = pytest.mark.asyncio(loop_scope="session")

_INVOICE = FetchInvoice(
    ruc_emisor="20123456789",
    tipo="01",
    serie="F001",
    numero="42",
    fecha=date(2026, 1, 15),
    total=Decimal("118.00"),
)
_VALUES = {"ruc_emisor": "20123456789", "serie": "F001", "numero": "42", "total": "118.00"}


def _reading(**changes: str | None) -> Reading:
    values = {**_VALUES, **changes}
    fields = {
        name: Field(value=value, source="ocr", confidence=FULL_CONFIDENCE)
        for name, value in values.items()
        if value is not None
    }
    return Reading(fields=fields, document_url=None)


async def _matches(reading: Reading | None) -> bool:
    async def reader(_path: Path) -> Reading | None:
        return reading

    return await pdf_matches(Path("lone.pdf"), _INVOICE, reader)


async def test_all_four_equal_matches() -> None:
    """D193, R-5.6: RUC, serie, número and total all equal is a match."""
    assert await _matches(_reading()) is True


@pytest.mark.parametrize(
    "change",
    [{"total": "119.00"}, {"numero": "43"}, {"ruc_emisor": "20987654321"}],
    ids=["total", "numero", "ruc"],
)
async def test_one_different_field_does_not_match(change: dict[str, str]) -> None:
    """D193, R-5.6: a different total, número or RUC is no match."""
    assert await _matches(_reading(**change)) is False


async def test_missing_total_does_not_match() -> None:
    """D193, R-5.6: a reading with no total is no match."""
    assert await _matches(_reading(total=None)) is False


@pytest.mark.parametrize("error", [RuntimeError("helper failed"), TimeoutError()])
async def test_helper_failure_does_not_match(error: Exception) -> None:
    """D193, R-5.6: the reader raising the helper's error is a failed read, so False."""

    async def reader(_path: Path) -> Reading | None:
        raise error

    assert await pdf_matches(Path("lone.pdf"), _INVOICE, reader) is False


async def test_unreadable_pdf_does_not_match() -> None:
    """D193, R-5.6: a reader that returns None is a failed read, so False."""
    assert await _matches(None) is False
