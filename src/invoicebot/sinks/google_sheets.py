"""Google Sheets REST client for flow A delivery (R-6.2, R-6.3, R-6.6, D129).

The client is plain ``httpx``; tokens come from an injected :class:`TokenSource`
and are fetched per call. Every request has the configured timeout and a bearer
header. Writes use ``valueInputOption=RAW`` and every text cell is escaped, so no
cell is ever evaluated as a formula (R-6.6). Errors carry the HTTP status only,
never a URL, a path or a body (R-9.16).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import quote

import httpx

from invoicebot.config.models import GOOGLE_ID_PATTERN
from invoicebot.sinks.base import TokenSource
from invoicebot.sinks.google_api import (
    GoogleError,
    auth_headers,
    json_object,
    raise_google_error,
)
from invoicebot.text import escape_formula

#: The Sheets v4 endpoint; the only host this client talks to.
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"

#: Sheets stores the values as sent; nothing is parsed or reformatted (R-6.6).
RAW = "RAW"

#: An append inserts new rows instead of writing over the existing ones (R-6.2).
INSERT_ROWS = "INSERT_ROWS"

#: The link to one row of a spreadsheet.
ROW_LINK_TEMPLATE = (
    "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}&range=A{row_number}"
)

#: The fields that carry a tab's title and its numeric id.
_PROPERTIES_FIELDS = "sheets.properties"

#: The first row number in an A1 range such as ``A7:K7``.
_ROW_IN_RANGE = re.compile(r"(\d+)")


def _a1(tab: str, cells: str) -> str:
    """Return the A1 range for ``cells`` on ``tab``, quoting the tab (R-6.2).

    A tab name is wrapped in single quotes and each of its own single quotes is
    doubled, so a name such as ``O'Brien`` stays one range token.
    """
    quoted = tab.replace("'", "''")
    return f"'{quoted}'!{cells}"


def _updated_row(body: Mapping[str, object]) -> int:
    """Return the row number an append landed on (R-6.2).

    Google reports it in ``updates.updatedRange`` as ``Tab!A7:K7``; a body
    without that shape is a :class:`GoogleError`.
    """
    updates = body.get("updates")
    if not isinstance(updates, dict):
        raise GoogleError()
    updated_range = updates.get("updatedRange")
    if not isinstance(updated_range, str):
        raise GoogleError()
    _, separator, cells = updated_range.rpartition("!")
    if not separator:
        raise GoogleError()
    found = _ROW_IN_RANGE.search(cells)
    if found is None:
        raise GoogleError()
    return int(found.group(1))


def _spreadsheet_id(body: Mapping[str, object]) -> str:
    """Return the new spreadsheet's id, refusing one Google should never have sent (R-6.2)."""
    value = body.get("spreadsheetId")
    if not isinstance(value, str) or GOOGLE_ID_PATTERN.fullmatch(value) is None:
        raise GoogleError()
    return value


def _sheet_id(body: Mapping[str, object], tab: str) -> int:
    """Return the numeric id of ``tab``, or raise when it is missing (R-6.2)."""
    sheets = body.get("sheets")
    if not isinstance(sheets, list):
        raise GoogleError()
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        properties = sheet.get("properties")
        if not isinstance(properties, dict) or properties.get("title") != tab:
            continue
        value = properties.get("sheetId")
        if not isinstance(value, int) or isinstance(value, bool):
            raise GoogleError()
        return value
    raise GoogleError()


class GoogleSheets:
    """One Google account's spreadsheets, over an injected HTTP client (D129).

    Long-lived: one instance per signed-in account. The tab id behind a row
    link is fetched once and remembered per spreadsheet and tab.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        timeout_s: float,
        *,
        account: str,
        max_rows: int,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._timeout_s = timeout_s
        self._account = account
        self._max_rows = max_rows
        self._sheet_ids: dict[tuple[str, str], int] = {}

    async def create(self, title: str, tab: str, header: list[str]) -> str:
        """Create a spreadsheet with one tab and write ``header`` to its first row (R-6.2, D129).

        Returns:
            The new spreadsheet's id.

        Raises:
            GoogleError: Google refuses, or answers without an id that can be one.
        """
        response = await self._http.post(
            SHEETS_URL,
            json={
                "properties": {"title": title},
                "sheets": [{"properties": {"title": tab}}],
            },
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        spreadsheet_id = _spreadsheet_id(json_object(response))
        await self.update(spreadsheet_id, tab, 1, header)
        return spreadsheet_id

    async def keys(self, spreadsheet_id: str, tab: str) -> dict[str, int]:
        """Return each key in the tab's first column mapped to its row (R-6.3).

        Row 1 is the header and is skipped, an empty or non-text cell is skipped,
        and the first row a key appears in wins.
        """
        response = await self._http.get(
            self._values_url(spreadsheet_id, _a1(tab, f"A1:A{self._max_rows}")),
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        values = json_object(response).get("values", [])
        if not isinstance(values, list):
            raise GoogleError()
        keys: dict[str, int] = {}
        for row_number, row in enumerate(values[1:], start=2):
            if not isinstance(row, list) or not row:
                continue
            cell = row[0]
            if isinstance(cell, str) and cell:
                keys.setdefault(cell, row_number)
        return keys

    async def append(self, spreadsheet_id: str, tab: str, row: list[str]) -> int:
        """Append ``row`` and return the row number it landed on (R-6.2, R-6.6).

        The row is sent as RAW with new rows inserted rather than overwritten.
        """
        response = await self._http.post(
            f"{self._values_url(spreadsheet_id, _a1(tab, 'A1'))}:append",
            params={"valueInputOption": RAW, "insertDataOption": INSERT_ROWS},
            json={"values": [self._escaped(row)]},
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)
        return _updated_row(json_object(response))

    async def update(self, spreadsheet_id: str, tab: str, row_number: int, row: list[str]) -> None:
        """Write ``row`` over the row at ``row_number`` (R-6.3, R-6.6).

        The values are sent as RAW, so a retried delivery changes the row instead
        of appending a second one.
        """
        response = await self._http.put(
            self._values_url(spreadsheet_id, _a1(tab, f"A{row_number}")),
            params={"valueInputOption": RAW},
            json={"values": [self._escaped(row)]},
            headers=await self._headers(),
            timeout=self._timeout_s,
        )
        raise_google_error(response, self._account)

    async def row_link(self, spreadsheet_id: str, tab: str, row_number: int) -> str:
        """Return the browser link to ``row_number`` in ``tab`` (R-6.2).

        The tab's numeric id is read once per spreadsheet and tab, then cached.
        """
        key = (spreadsheet_id, tab)
        sheet_id = self._sheet_ids.get(key)
        if sheet_id is None:
            response = await self._http.get(
                f"{SHEETS_URL}/{spreadsheet_id}",
                params={"fields": _PROPERTIES_FIELDS},
                headers=await self._headers(),
                timeout=self._timeout_s,
            )
            raise_google_error(response, self._account)
            sheet_id = _sheet_id(json_object(response), tab)
            self._sheet_ids[key] = sheet_id
        return ROW_LINK_TEMPLATE.format(
            spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_id,
            row_number=row_number,
        )

    def _values_url(self, spreadsheet_id: str, a1: str) -> str:
        """Return the ``values`` URL for ``a1`` with the range path-encoded."""
        return f"{SHEETS_URL}/{spreadsheet_id}/values/{quote(a1, safe='')}"

    async def _headers(self) -> dict[str, str]:
        return auth_headers(await self._tokens.token())

    @staticmethod
    def _escaped(row: list[str]) -> list[str]:
        """Return the row with every cell escaped before it is written (R-6.6)."""
        return [escape_formula(cell) for cell in row]
