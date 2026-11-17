"""The Sheets sink: one invoice as one row, keyed by its natural key (R-6.2, R-6.3, D130).

One job: render the configured columns and upsert the row whose first cell is the key.
The spreadsheet is a port; nothing here talks to HTTP.
"""

from __future__ import annotations

from collections.abc import Mapping

from invoicebot.config.models import GoogleSheetsSink
from invoicebot.sinks.expr import render
from invoicebot.sinks.google_sheets import GoogleSheets

#: The flat value every sink returns for the later sinks and the completion message (D131).
LINK_ROW = "link_row"


async def upsert_row(
    sink: GoogleSheetsSink,
    sheets: GoogleSheets,
    key: str,
    values: Mapping[str, object],
) -> dict[str, str]:
    """Write the invoice as one row and return its link (R-6.2, R-6.3, D131).

    Column A is the key (D130); the other cells are the configured columns in
    config order. A key already in the tab's first column updates its row instead
    of appending a second one, so a retried delivery is safe.
    """
    row = [key, *(render(expression, values) for expression in sink.columns.values())]
    known_row = (await sheets.keys(sink.spreadsheet_id, sink.tab)).get(key)
    if known_row is None:
        row_number = await sheets.append(sink.spreadsheet_id, sink.tab, row)
    else:
        await sheets.update(sink.spreadsheet_id, sink.tab, known_row, row)
        row_number = known_row
    link = await sheets.row_link(sink.spreadsheet_id, sink.tab, row_number)
    return {LINK_ROW: link}
