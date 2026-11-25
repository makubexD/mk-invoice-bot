"""Try the portal addresses a ticket may mean and offer one (BR-F14, BR-F15, D192).

:func:`make_portal_check_handler` builds the handler of the ``portal_check`` job step. It
derives the addresses the ticket may mean, asks which one answers outside any transaction,
and in one transaction offers it as BR-F14; when none answers it offers the first guess as
``BR-F14_unverified``; a strip that yields no address at all falls back to the QR link, or
asks BR-F15 with the unread strip. The offer is stored so [Sí] teaches exactly the URL the
sender saw (D191). An invoice whose state left ``FETCHING_DOCS``/``MANUAL_REQUIRED``
meanwhile, or one with no reading, writes nothing and ends the job quietly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from psycopg import AsyncConnection

from invoicebot.extract.fields import Reading
from invoicebot.net.taught_url import taught_url_refusal
from invoicebot.net.url_probe import first_answering
from invoicebot.pipeline.outgoing import Addressee
from invoicebot.pipeline.portal_guess import MAX_CANDIDATES, candidate_urls
from invoicebot.pipeline.portal_offer import (
    MESSAGE_UNVERIFIED,
    ask_unread,
    offer_candidate,
    qr_offer,
)
from invoicebot.pipeline.steps import PORTAL_CHECK_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.states import FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store import taught_links
from invoicebot.store.db import Pool
from invoicebot.store.invoices import load_invoice_head, load_reading, lock_invoice

logger = logging.getLogger(__name__)

#: The states the portal is offered in (D192).
_CHECKED_STATES = frozenset({FETCHING_DOCS, MANUAL_REQUIRED})


@dataclass(frozen=True)
class _Check:
    """The invoice a check offers a portal to and what its ticket says (D192)."""

    invoice_id: int
    addressee: Addressee
    reading: Reading
    text: str
    hosts: frozenset[str]


def make_portal_check_handler(pool: Pool, http: httpx.AsyncClient) -> Handler:
    """Build the handler that offers the portal in ``portal_check`` (BR-F14, BR-F15, D192).

    It reads the invoice, its reading and the approved hosts, then probes the candidate
    addresses with no connection held. In one transaction it locks the row, rereads it
    and writes nothing when the state left ``FETCHING_DOCS``/``MANUAL_REQUIRED``; otherwise
    it offers the answering URL, the first guess as unverified, the QR link, or asks
    BR-F15 with the unread strip.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        check = await _read(pool, job.invoice_id)
        if check is None:
            return
        candidates = _candidates(check)
        answering = await first_answering(http, candidates) if candidates else None
        async with pool.connection() as conn, conn.transaction():
            await _record(conn, check, candidates, answering)
        logger.info(
            "step done",
            extra={
                "step": PORTAL_CHECK_STEP,
                "invoice_id": job.invoice_id,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    return handle


def _candidates(check: _Check) -> tuple[str, ...]:
    """Return the URLs to try, with a printed URL the OCR repair loses first, as printed (D192).

    The repair is made for a strip: it drops a query string and any top level it does not
    expect, both of which a printed URL may really have (D189).
    """
    candidates = candidate_urls(check.text, check.reading.value("razon_social"), check.hosts)
    printed = check.reading.printed_url
    if printed is None or printed in candidates or taught_url_refusal(printed) is not None:
        return candidates
    return (printed, *candidates)[:MAX_CANDIDATES]


async def _read(pool: Pool, invoice_id: int) -> _Check | None:
    """Load what the check needs, or None when there is nothing to offer (D192).

    A missing invoice, one in another state, one without a reading, or one whose ticket
    carries neither a printed URL nor a strip ends the job quietly.
    """
    async with pool.connection() as conn:
        head = await load_invoice_head(conn, invoice_id)
        if head is None or head.state not in _CHECKED_STATES:
            return None
        reading = await load_reading(conn, invoice_id)
        if reading is None:
            return None
        text = reading.printed_url or reading.url_strip
        if text is None:
            return None
        hosts = await taught_links.approved_hosts(conn)
        return _Check(
            invoice_id=invoice_id,
            addressee=Addressee(head.origin_channel, head.profile_id),
            reading=reading,
            text=text,
            hosts=hosts,
        )


async def _record(
    conn: AsyncConnection[Any],
    check: _Check,
    candidates: tuple[str, ...],
    answering: str | None,
) -> None:
    """Offer the portal in the caller's transaction, or write nothing when the state moved (D192).

    It locks the row first: an invoice cancelled or settled after :func:`_read`, or one
    moved to another state, is left untouched and nothing is queued.
    """
    await lock_invoice(conn, check.invoice_id)
    head = await load_invoice_head(conn, check.invoice_id)
    if head is None or head.state not in _CHECKED_STATES:
        return
    if answering is not None:
        await offer_candidate(conn, check.addressee, check.invoice_id, check.reading, answering)
        return
    if candidates:
        await offer_candidate(
            conn,
            check.addressee,
            check.invoice_id,
            check.reading,
            candidates[0],
            message=MESSAGE_UNVERIFIED,
        )
        return
    link = qr_offer(check.reading)
    if link is not None:
        await offer_candidate(conn, check.addressee, check.invoice_id, check.reading, link)
        return
    await ask_unread(conn, check.addressee, check.invoice_id, check.reading, check.text)
