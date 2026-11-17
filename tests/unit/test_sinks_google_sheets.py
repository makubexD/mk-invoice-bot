"""Tests for the Google Sheets client (R-6.2, R-6.3, R-6.6)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from invoicebot.sinks.google_api import GoogleError, GoogleTransient
from invoicebot.sinks.google_sheets import (
    INSERT_ROWS,
    RAW,
    SHEETS_URL,
    GoogleSheets,
)

ACCOUNT = "google"
BEARER = "synthetic-bearer-value"
TIMEOUT_S = 5.0
MAX_ROWS = 1000
SPREADSHEET_ID = "1" + "a" * 30
TAB = "Facturas"
SHEET_ID = 42

Handler = Callable[[httpx.Request], httpx.Response]


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return BEARER


@contextlib.asynccontextmanager
async def _make_sheets(handler: Handler) -> AsyncIterator[GoogleSheets]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield GoogleSheets(http, FakeTokens(), TIMEOUT_S, account=ACCOUNT, max_rows=MAX_ROWS)


def _values_path(a1: str) -> str:
    """Return the decoded path of the values endpoint for ``a1``."""
    return f"/v4/spreadsheets/{SPREADSHEET_ID}/values/{a1}"


async def test_keys_maps_each_key_to_its_one_based_row() -> None:
    """R-6.3: keys maps each key in the first column to its row, skipping the header."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"values": [["Clave"], ["F001-1"], [], [], ["F001-2"]]})

    async with _make_sheets(handler) as sheets:
        keys = await sheets.keys(SPREADSHEET_ID, TAB)

    assert keys == {"F001-1": 2, "F001-2": 5}
    assert [request.method for request in requests] == ["GET"]
    assert requests[0].url.path == _values_path("'Facturas'!A1:A1000")
    assert requests[0].headers["Authorization"] == f"Bearer {BEARER}"


async def test_keys_skips_empty_and_non_text_cells() -> None:
    """R-6.3: an empty cell or one that is not text is not a key."""
    values = [["Clave"], [""], [None], [7], ["F001-2"]]

    async with _make_sheets(lambda request: httpx.Response(200, json={"values": values})) as sheets:
        keys = await sheets.keys(SPREADSHEET_ID, TAB)

    assert keys == {"F001-2": 5}


async def test_keys_keeps_the_first_row_of_a_repeated_key() -> None:
    """R-6.3: the first row a key appears in wins, so an update targets the oldest one."""
    values = [["Clave"], ["F001-1"], ["F001-1"]]

    async with _make_sheets(lambda request: httpx.Response(200, json={"values": values})) as sheets:
        keys = await sheets.keys(SPREADSHEET_ID, TAB)

    assert keys == {"F001-1": 2}


async def test_append_sends_raw_insert_and_returns_the_updated_row() -> None:
    """R-6.2: an append uses RAW with INSERT_ROWS and reports the row from updatedRange."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"updates": {"updatedRange": "Facturas!A7:K7"}})

    async with _make_sheets(handler) as sheets:
        row_number = await sheets.append(SPREADSHEET_ID, TAB, ["F001-1", "2026-11-17"])

    assert row_number == 7
    append = requests[0]
    assert append.method == "POST"
    expected_path = f"/v4/spreadsheets/{SPREADSHEET_ID}/values/'Facturas'!A1:append"
    assert append.url.path == expected_path
    assert append.url.params["valueInputOption"] == RAW
    assert append.url.params["insertDataOption"] == INSERT_ROWS
    assert json.loads(append.content) == {"values": [["F001-1", "2026-11-17"]]}
    assert append.headers["Authorization"] == f"Bearer {BEARER}"


async def test_append_escapes_a_formula_cell() -> None:
    """R-6.6: a cell that starts like a formula is sent as text."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"updates": {"updatedRange": "Facturas!A8:K8"}})

    async with _make_sheets(handler) as sheets:
        await sheets.append(SPREADSHEET_ID, TAB, ['=HYPERLINK("x")', "ok"])

    assert json.loads(requests[0].content) == {"values": [['\'=HYPERLINK("x")', "ok"]]}


async def test_update_writes_raw_to_the_rows_range() -> None:
    """R-6.6: an update uses RAW and escapes every cell of the row."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    row = ['=HYPERLINK("x")', "+55", "Ferretería"]
    async with _make_sheets(handler) as sheets:
        await sheets.update(SPREADSHEET_ID, TAB, 7, row)

    update = requests[0]
    assert update.method == "PUT"
    assert update.url.path == _values_path("'Facturas'!A7")
    assert update.url.params["valueInputOption"] == RAW
    assert json.loads(update.content) == {"values": [['\'=HYPERLINK("x")', "'+55", "Ferretería"]]}
    assert update.headers["Authorization"] == f"Bearer {BEARER}"


async def test_a_quote_in_a_tab_is_doubled_and_the_range_is_path_encoded() -> None:
    """R-6.2: a tab named O'Brien is quoted as 'O''Brien' and encoded in the path."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with _make_sheets(handler) as sheets:
        await sheets.update(SPREADSHEET_ID, "O'Brien", 7, ["F001-1"])

    update = requests[0]
    assert update.url.path == _values_path("'O''Brien'!A7")
    assert "%27O%27%27Brien%27%21A7" in update.url.raw_path.decode()


@pytest.mark.parametrize("updated_range", ["Facturas", "Facturas!", "Facturas!K:K"])
async def test_an_append_with_an_unparseable_range_raises_google_error(updated_range: str) -> None:
    """R-6.2: an updatedRange without a row number is a GoogleError."""
    response = httpx.Response(200, json={"updates": {"updatedRange": updated_range}})

    async with _make_sheets(lambda request: response) as sheets:
        with pytest.raises(GoogleError):
            await sheets.append(SPREADSHEET_ID, TAB, ["F001-1"])


async def test_row_link_reads_the_sheet_id_once_and_returns_the_docs_link() -> None:
    """R-6.2: row_link finds the tab's id once and links each row in it."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"sheets": [{"properties": {"sheetId": SHEET_ID, "title": TAB}}]},
        )

    async with _make_sheets(handler) as sheets:
        first = await sheets.row_link(SPREADSHEET_ID, TAB, 7)
        second = await sheets.row_link(SPREADSHEET_ID, TAB, 8)

    base = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
    assert first == f"{base}#gid={SHEET_ID}&range=A7"
    assert second == f"{base}#gid={SHEET_ID}&range=A8"
    assert len(requests) == 1
    lookup = requests[0]
    assert lookup.method == "GET"
    assert str(lookup.url.copy_with(query=None)) == f"{SHEETS_URL}/{SPREADSHEET_ID}"
    assert lookup.url.params["fields"] == "sheets.properties"
    assert lookup.headers["Authorization"] == f"Bearer {BEARER}"


async def test_row_link_with_a_missing_tab_raises_google_error() -> None:
    """R-6.2: a tab the spreadsheet does not have cannot be linked."""
    body = {"sheets": [{"properties": {"sheetId": SHEET_ID, "title": "Otra"}}]}

    async with _make_sheets(lambda request: httpx.Response(200, json=body)) as sheets:
        with pytest.raises(GoogleError):
            await sheets.row_link(SPREADSHEET_ID, TAB, 7)


async def test_row_link_with_a_non_int_sheet_id_raises_google_error() -> None:
    """R-6.2: a tab id that is not a number cannot build a link."""
    body = {"sheets": [{"properties": {"sheetId": "42", "title": TAB}}]}

    async with _make_sheets(lambda request: httpx.Response(200, json=body)) as sheets:
        with pytest.raises(GoogleError):
            await sheets.row_link(SPREADSHEET_ID, TAB, 7)


async def test_a_429_maps_to_google_transient() -> None:
    """R-6.4: a 429 is a transient failure the queue retries, carrying only the status."""

    async with _make_sheets(lambda request: httpx.Response(429)) as sheets:
        with pytest.raises(GoogleTransient) as raised:
            await sheets.append(SPREADSHEET_ID, TAB, ["F001-1"])

    assert raised.value.status == 429
    assert SPREADSHEET_ID not in str(raised.value)
