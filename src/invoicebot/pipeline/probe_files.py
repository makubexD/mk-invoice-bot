"""Decide whether a lone PDF a portal probe downloaded is this invoice (D193, R-5.6).

A probe fills a portal form once and keeps what it downloads. A PDF with no XML beside it is
kept only when its own reading shows the same issuer RUC, serie, número and total as the
stored invoice, so a wrong download is never filed as this invoice.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from invoicebot.extract.fields import Reading
from invoicebot.extract.ubl import matches_invoice
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.portal import invoice_reading

logger = logging.getLogger(__name__)

#: Reads one PDF into a reading, or None when it shows nothing usable.
type PdfReader = Callable[[Path], Awaitable[Reading | None]]


async def pdf_matches(path: Path, invoice: FetchInvoice, reader: PdfReader) -> bool:
    """Return whether the PDF at ``path`` reads as ``invoice`` (D193, R-5.6).

    True only when RUC, serie, número and total all match; :func:`matches_invoice` already
    treats a reading missing any of the four as a mismatch. A failed read is False: the
    reader returning None, or the extract helper failing (``RuntimeError``) or timing out
    (``TimeoutError``). Nothing read from the PDF is logged.
    """
    try:
        reading = await reader(path)
    except (RuntimeError, TimeoutError):
        logger.info("probe pdf not read")
        return False
    return reading is not None and matches_invoice(reading, invoice_reading(invoice))
