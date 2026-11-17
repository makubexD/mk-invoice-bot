"""Tests for the Sheets sink: one row per key, upserted in config column order (R-6.2, R-6.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, GoogleSheetsSink
from invoicebot.sinks.sheet_rows import LINK_ROW, upsert_row

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"

#: Synthetic invoice values only; nothing here belongs to a real invoice.
FECHA = date(2026, 11, 5)
SERIE = "F001"
NUMERO = "123"
KEY = "F001-123"
NEW_ROW = 2
KNOWN_ROW = 7


class FakeSheets:
    """An in-memory Sheets that records the keys read and the rows written (R-6.2, R-6.3)."""

    def __init__(self, known: dict[str, int] | None = None) -> None:
        self.known = known or {}
        self.key_calls: list[tuple[str, str]] = []
        self.appends: list[tuple[str, str, list[str]]] = []
        self.updates: list[tuple[str, str, int, list[str]]] = []
        self.link_calls: list[tuple[str, str, int]] = []

    async def keys(self, spreadsheet_id: str, tab: str) -> dict[str, int]:
        """Record the lookup and return the fake tab's known keys."""
        self.key_calls.append((spreadsheet_id, tab))
        return dict(self.known)

    async def append(self, spreadsheet_id: str, tab: str, row: list[str]) -> int:
        """Record the appended row and report its number."""
        self.appends.append((spreadsheet_id, tab, row))
        return NEW_ROW

    async def update(self, spreadsheet_id: str, tab: str, row_number: int, row: list[str]) -> None:
        """Record the row written over ``row_number``."""
        self.updates.append((spreadsheet_id, tab, row_number, row))

    async def row_link(self, spreadsheet_id: str, tab: str, row_number: int) -> str:
        """Record the link request and return a fake row link."""
        self.link_calls.append((spreadsheet_id, tab, row_number))
        return f"https://docs.example/spreadsheets/{spreadsheet_id}/row/{row_number}"


@pytest.fixture(scope="module")
def example_config() -> Config:
    """Load the shipped example config the way the app does (R-9.6)."""
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def example_sink(example_config: Config) -> GoogleSheetsSink:
    """Return the first Google Sheets sink in ``config/example.yaml`` (R-6.2)."""
    for flow in example_config.flows.values():
        for sink in flow.sinks:
            if isinstance(sink, GoogleSheetsSink):
                return sink
    raise AssertionError("config/example.yaml has no google_sheets sink")


@pytest.fixture(scope="module")
def values(example_config: Config) -> dict[str, object]:
    """Return the synthetic invoice values; the issuer placeholder comes from the example config (D27)."""
    return {
        "fecha": FECHA,
        "ruc_emisor": example_config.owner.ruc,
        "razon_social": "Proveedor Sintético SAC",
        "tipo_desc": "Factura",
        "serie": SERIE,
        "numero": NUMERO,
        "base": Decimal("100.00"),
        "igv": Decimal("18.00"),
        "total": Decimal("118.00"),
        "estado_sunat": "Aceptado",
        "link_photo": "https://drive.example/photos/photo",
        "link_pdf": "https://drive.example/pdfs/pdf",
        "link_xml": "https://drive.example/xmls/xml",
    }


def _expected_row(sink: GoogleSheetsSink, values: dict[str, object]) -> list[str]:
    """Return the row the example sink's columns render, key first (R-6.2, D130)."""
    return [
        KEY,
        "05/11/2026",
        str(values["ruc_emisor"]),
        "Proveedor Sintético SAC",
        "Factura",
        "F001-123",
        "100.00",
        "18.00",
        "118.00",
        "Aceptado",
        "https://drive.example/photos/photo",
        "https://drive.example/pdfs/pdf",
        "https://drive.example/xmls/xml",
    ]


async def test_new_key_appends_one_row_with_the_key_first(
    example_sink: GoogleSheetsSink, values: dict[str, object]
) -> None:
    """R-6.2: a new key appends one row with ``Clave`` first and the columns in config order."""
    sheets = FakeSheets()

    links = await upsert_row(example_sink, sheets, KEY, values)

    assert sheets.key_calls == [(example_sink.spreadsheet_id, example_sink.tab)]
    assert sheets.updates == []
    assert sheets.appends == [
        (example_sink.spreadsheet_id, example_sink.tab, _expected_row(example_sink, values))
    ]
    assert sheets.link_calls == [(example_sink.spreadsheet_id, example_sink.tab, NEW_ROW)]
    assert links == {
        LINK_ROW: f"https://docs.example/spreadsheets/{example_sink.spreadsheet_id}/row/{NEW_ROW}"
    }


async def test_known_key_updates_its_row_and_appends_nothing(
    example_sink: GoogleSheetsSink, values: dict[str, object]
) -> None:
    """R-6.3: a known key updates its row and appends nothing, so a retry is safe."""
    sheets = FakeSheets({KEY: KNOWN_ROW})

    links = await upsert_row(example_sink, sheets, KEY, values)

    assert sheets.appends == []
    assert sheets.updates == [
        (
            example_sink.spreadsheet_id,
            example_sink.tab,
            KNOWN_ROW,
            _expected_row(example_sink, values),
        )
    ]
    assert sheets.link_calls == [(example_sink.spreadsheet_id, example_sink.tab, KNOWN_ROW)]
    assert links == {
        LINK_ROW: f"https://docs.example/spreadsheets/{example_sink.spreadsheet_id}/row/{KNOWN_ROW}"
    }


async def test_drive_links_render_into_their_columns(
    example_sink: GoogleSheetsSink, values: dict[str, object]
) -> None:
    """R-6.2: the Drive links render into the columns configured for them."""
    sheets = FakeSheets()
    headers = list(example_sink.columns)

    await upsert_row(example_sink, sheets, KEY, values)

    row = sheets.appends[0][2]
    for header, link_name in (
        ("Foto", "link_photo"),
        ("PDF", "link_pdf"),
        ("XML", "link_xml"),
    ):
        assert row[1 + headers.index(header)] == values[link_name]
