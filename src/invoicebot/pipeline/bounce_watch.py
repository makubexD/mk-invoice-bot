"""Job handler that watches an ``smtp`` send for a bounce and leaves a draft (D207, BR-D26, BR-D27).

Deliver schedules the first ``bounce_check`` after an ``smtp`` send. Each run reads the
sending account's inbox for the mail's delivery report and schedules the next run until
``limits.bounce_watch_s`` after the send. A bounce sets the batch ``bounced``, drafts the
mail again from its Sent copy and tells the sender; the invoice stays ``DONE``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from psycopg import AsyncConnection

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.channels.base import Button
from invoicebot.channels.buttons import button_id
from invoicebot.config.models import SMTP_MODE, Config, MailerSink
from invoicebot.pipeline.actions import SENT_ACTION, SENT_BATCH_STATE
from invoicebot.pipeline.done import MAIL_INVOICE_KEY
from invoicebot.pipeline.outgoing import Addressee, outbound, queue_outbound, safe_links
from invoicebot.pipeline.steps import BOUNCE_CHECK_STEP, FOLDER_STATUS_STEP
from invoicebot.queue.jobs import Job, db_now_plus, enqueue, is_superseded
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    BOUNCE_STATUS_UNKNOWN,
    BOUNCED_DRAFT,
    BOUNCED_NO_DRAFT,
    BUTTON_OPEN_DRAFT,
    BUTTON_SENT,
)
from invoicebot.sinks.mail_bounce import Bounce, DraftUnavailable
from invoicebot.sinks.onedrive import GraphError, GraphTransient
from invoicebot.store.db import Pool
from invoicebot.store.flow_b import (
    BOUNCED_BATCH_STATE,
    load_flow_data,
    load_sent_batch,
    merge_flow_data,
    set_batch_state,
)
from invoicebot.store.invoices import load_flow, load_invoice_head, load_reading, lock_invoice

logger = logging.getLogger(__name__)

#: BR-D26: the bounce left a draft (D207).
RULE_BOUNCED_DRAFT = "BR-D26"
#: BR-D27: the bounce left no draft (D207).
RULE_BOUNCED_NO_DRAFT = "BR-D27"
#: The flow-data key that records the bounce's status code and whether a draft was made (D207).
BOUNCE_KEY = "bounce"
#: The status ``parse_bounce`` gives a report without an enhanced status code (D207).
_NO_STATUS = "?"
#: The ``status`` a blocked watch logs when no reader serves the flow's ``smtp`` account.
_NO_READER = "no_reader"
#: The ``status`` a blocked watch logs when the account needs a sign-in.
_AUTH_REQUIRED = "auth_required"
#: How far before ``sent_at`` the inbox is read. The Microsoft and host clocks may differ;
#: the id match keeps an older report from counting.
_SINCE_MARGIN_S = 60


class BounceReader(Protocol):
    """One ``smtp`` account's inbox and Drafts, bound to its token source and client (D207).

    Both methods raise ``GraphError`` for a final Graph failure, ``GraphTransient`` for a
    retryable one and ``AuthRequired`` when the account needs a sign-in. Neither puts a
    token, an address or a link in an error.
    """

    async def find_bounce(self, *, message_id: str, since: datetime) -> Bounce | None:
        """Return the delivery report of ``message_id`` received since ``since``, or None."""
        ...

    async def draft_from_sent(self, *, sent_id: str, message_id: str) -> str:
        """Draft the Sent copy of ``sent_id`` again and return the draft's web link.

        The copy must carry the bot's ``message_id`` as its original id.

        Raises:
            DraftUnavailable: the copy is missing, too big or another mail, or the link
                is not Outlook's.
        """
        ...


@dataclass(frozen=True)
class _Watch:
    """The sent batch one run watches, the flow's ``smtp`` mailer and the database time (D207)."""

    batch_id: int
    email_ref: str
    sent_at: datetime
    now: datetime
    account: str
    to: str


def make_bounce_check_handler(pool: Pool, cfg: Config, readers: Mapping[str, BounceReader]) -> Handler:
    """Build the handler of the ``bounce_check`` step (D207, BR-D26, BR-D27).

    A short transaction reads the batch and stops when a newer ``bounce_check`` exists
    (a forked chain) or unless the batch is ``sent`` with a Message-ID.
    The inbox read and the draft run with no transaction held. A final Graph error or a
    missing sign-in logs ``bounce watch blocked`` and ends the watch without touching the
    batch or the invoice; a transient error propagates to the worker's retry. With no
    report, the job schedules itself ``limits.bounce_check_every_s`` later until
    ``limits.bounce_watch_s`` after the send. With one, a failed draft, transient errors
    included, or a link the outbound check drops only means BR-D27 instead of BR-D26. The
    last transaction locks the invoice and reads the batch again, so a batch that is no
    longer ``sent``, or that already carries a recorded bounce, is left alone; otherwise
    it sets the batch ``bounced``, records the bounce in flow data, queues
    ``folder_status`` and the message. The recorded bounce also keeps a batch set ``sent``
    again by [Ya lo envié] (BR-D08) from being watched again. The draft's link is never
    logged.

    A crash after the draft and before that commit makes a second draft on the retry. That
    is accepted: the owner sends one and deletes the other, and the mail is never sent twice.
    """

    async def handle(job: Job) -> None:
        watch = await _start(pool, cfg, job)
        if watch is None:
            return
        reader = readers.get(watch.account)
        if reader is None:
            _blocked(job.invoice_id, _NO_READER)
            return
        since = watch.sent_at - timedelta(seconds=_SINCE_MARGIN_S)
        try:
            bounce = await reader.find_bounce(message_id=watch.email_ref, since=since)
        except GraphError as err:
            _blocked(job.invoice_id, str(err.status))
            return
        except AuthRequired:
            _blocked(job.invoice_id, _AUTH_REQUIRED)
            return
        if bounce is None:
            await _watch_on(pool, cfg, job.invoice_id, watch)
            return
        link = await _draft(reader, job.invoice_id, bounce, watch.email_ref)
        await _report(pool, job.invoice_id, watch, bounce, link)

    return handle


async def _start(pool: Pool, cfg: Config, job: Job) -> _Watch | None:
    """Return what this run watches, or None unless the batch is ``sent`` with a Message-ID.

    A newer ``bounce_check`` for the invoice means this run is a stale fork of the chain
    (R-5.1), so it does nothing. A batch set ``sent`` again after its bounce (BR-D08)
    carries the recorded bounce, so it is not watched again.
    """
    invoice_id = job.invoice_id
    async with pool.connection() as conn, conn.transaction():
        if await is_superseded(conn, job):
            return None
        batch = await load_sent_batch(conn, invoice_id)
        if batch is None or batch.state != SENT_BATCH_STATE:
            return None
        if batch.email_ref is None or batch.sent_at is None:
            return None
        if BOUNCE_KEY in await load_flow_data(conn, invoice_id):
            return None
        flow_name = await load_flow(conn, invoice_id)
        now = await db_now_plus(conn)
    mailer = _smtp_mailer(cfg, flow_name)
    if mailer is None:
        _blocked(invoice_id, _NO_READER)
        return None
    return _Watch(
        batch_id=batch.id,
        email_ref=batch.email_ref,
        sent_at=batch.sent_at,
        now=now,
        account=mailer.account,
        to=mailer.to[0],
    )


def _smtp_mailer(cfg: Config, flow_name: str | None) -> MailerSink | None:
    """Return the flow's mailer sink when it lists ``smtp``, else None."""
    flow = None if flow_name is None else cfg.flows.get(flow_name)
    if flow is None:
        return None
    return next(
        (sink for sink in flow.sinks if isinstance(sink, MailerSink) and SMTP_MODE in sink.modes), None
    )


def _blocked(invoice_id: int, status: str) -> None:
    """Log that the watch cannot read the mailbox and ends here (D207)."""
    logger.warning("bounce watch blocked", extra={"invoice_id": invoice_id, "status": status})


async def _watch_on(pool: Pool, cfg: Config, invoice_id: int, watch: _Watch) -> None:
    """Schedule the next check while the watch lasts, else log that the mail is clear (D207)."""
    if watch.now >= watch.sent_at + timedelta(seconds=cfg.limits.bounce_watch_s):
        logger.info("bounce watch clear", extra={"invoice_id": invoice_id})
        return
    async with pool.connection() as conn, conn.transaction():
        after = await db_now_plus(conn, seconds=cfg.limits.bounce_check_every_s)
        await enqueue(conn, invoice_id, BOUNCE_CHECK_STEP, after)


async def _draft(reader: BounceReader, invoice_id: int, bounce: Bounce, message_id: str) -> str | None:
    """Return the draft's web link, or None when no draft could be made (D207, BR-D27).

    A transient or network error is "no draft" too: the bounce is already found, so the
    sender hears of it now instead of the job retrying, or parking, with it unreported.
    """
    try:
        return await reader.draft_from_sent(sent_id=bounce.sent_id, message_id=message_id)
    except (DraftUnavailable, GraphError, GraphTransient, httpx.HTTPError, AuthRequired) as err:
        logger.warning(
            "bounce draft unavailable",
            extra={"invoice_id": invoice_id, "status": bounce.status, "error": type(err).__name__},
        )
        return None


async def _report(pool: Pool, invoice_id: int, watch: _Watch, bounce: Bounce, link: str | None) -> None:
    """Mark the batch ``bounced`` and queue BR-D26 or BR-D27, unless it changed meanwhile (D207)."""
    async with pool.connection() as conn, conn.transaction():
        await lock_invoice(conn, invoice_id)
        batch = await load_sent_batch(conn, invoice_id)
        if batch is None or batch.id != watch.batch_id or batch.state != SENT_BATCH_STATE:
            return
        flow_data = await load_flow_data(conn, invoice_id)
        if batch.email_ref != watch.email_ref or BOUNCE_KEY in flow_data:
            return
        head = await load_invoice_head(conn, invoice_id)
        if head is None:
            return
        serie, numero = await _serie_numero(conn, invoice_id, flow_data)
        links = [] if link is None else safe_links([(BUTTON_OPEN_DRAFT, link)])
        await set_batch_state(conn, batch.id, BOUNCED_BATCH_STATE)
        await merge_flow_data(conn, invoice_id, {BOUNCE_KEY: {"status": bounce.status, "draft": bool(links)}})
        await enqueue(conn, invoice_id, FOLDER_STATUS_STEP)
        status = BOUNCE_STATUS_UNKNOWN if bounce.status == _NO_STATUS else bounce.status
        values = {"id": invoice_id, "serie": serie, "numero": numero, "to": watch.to, "status": status}
        sent = Button(id=button_id(invoice_id, SENT_ACTION), title=BUTTON_SENT)
        if links:
            rule_id, text = RULE_BOUNCED_DRAFT, BOUNCED_DRAFT.format(**values)
        else:
            rule_id, text = RULE_BOUNCED_NO_DRAFT, BOUNCED_NO_DRAFT.format(**values)
        message = outbound(
            Addressee(head.origin_channel, head.profile_id),
            text,
            rule_id=rule_id,
            buttons=[sent],
            links=links,
        )
        await queue_outbound(conn, message, invoice_id=invoice_id)
    logger.info("bounce found", extra={"invoice_id": invoice_id, "status": bounce.status})


async def _serie_numero(
    conn: AsyncConnection[Any], invoice_id: int, flow_data: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """Return the [serie, numero] the mail was built from, or the reading's when none was saved (R-7.4)."""
    saved = flow_data.get(MAIL_INVOICE_KEY)
    if isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, str) for v in saved):
        return saved[0], saved[1]
    reading = await load_reading(conn, invoice_id)
    if reading is None:
        return None, None
    return reading.value("serie"), reading.value("numero")
