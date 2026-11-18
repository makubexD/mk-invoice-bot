"""Re-evaluate open config gaps and resume the invoices they stopped (R-11.9, D157).

:func:`reevaluate` runs in one transaction over
:func:`invoicebot.store.gaps.fixable_gaps`. For each gap whose per-kind fix
rule now holds it marks the gap fixed and sends its still-waiting invoices back
to the step that failed, telling each sender with BR-F12. A fixed gap with no
waiting invoice closes at once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.digest import config_digest
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.gaps.kinds import GAP_KINDS
from invoicebot.pipeline.deliver import RULE_TEMPLATE_INVALID
from invoicebot.pipeline.gaps import close_gap
from invoicebot.pipeline.manual_message import RULE_EXHAUSTED
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.resume import resume_delivery, retry_documents
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import MANUAL_REQUIRED
from invoicebot.store.db import Pool
from invoicebot.store.gaps import GapRow, fixable_gaps, mark_fixed, stuck_invoices
from invoicebot.store.invoices import load_invoice_head

logger = logging.getLogger(__name__)

#: The rule id of the notice that a fixed gap re-enqueued an invoice (BR-F12).
RULE_RESUMED = "BR-F12"

_PORTAL_MISSING = GAP_KINDS["PORTAL_MISSING"].kind
_PORTAL_BROKEN = GAP_KINDS["PORTAL_BROKEN"].kind
_TEMPLATE_INVALID = GAP_KINDS["TEMPLATE_INVALID"].kind

#: One gap kind's rule for whether the loaded config fixed it (D157).
type FixRule = Callable[[GapRow, Config, StoreDirectory], bool]

#: Sends one waiting invoice back to the step its gap's kind failed at (D157).
type Resume = Callable[[AsyncConnection[Any], int], Awaitable[object]]


def _fingerprint_parts(gap: GapRow) -> tuple[str, ...]:
    """Return ``gap``'s fingerprint parts after its kind prefix (D157)."""
    prefix = GAP_KINDS[gap.kind].fingerprint_prefix
    head, *parts = gap.fingerprint.split(":")
    if head != prefix:
        raise ValueError(f"fingerprint {gap.fingerprint!r} has prefix {head!r}, expected {prefix!r}")
    return tuple(parts)


def _portal_missing_fixed(gap: GapRow, _cfg: Config, directory: StoreDirectory) -> bool:
    """D157: fixed when the RUC has a store whose portal is active."""
    (ruc,) = _fingerprint_parts(gap)
    store = directory.stores.get(ruc)
    if store is None or store.portal is None:
        return False
    portal = directory.portals.get(store.portal)
    return portal is not None and portal.status == "active"


def _portal_broken_fixed(gap: GapRow, _cfg: Config, directory: StoreDirectory) -> bool:
    """D157: fixed when the portal is active and its entry changed."""
    (key,) = _fingerprint_parts(gap)
    portal = directory.portals.get(key)
    return portal is not None and portal.status == "active" and config_digest(portal) != gap.raised_with


def _template_invalid_fixed(gap: GapRow, cfg: Config, _directory: StoreDirectory) -> bool:
    """D157: fixed when the flow and sink still exist and the sink entry changed."""
    flow_name, sink_name = _fingerprint_parts(gap)
    flow = cfg.flows.get(flow_name)
    if flow is None:
        return False
    sink = next((entry for entry in flow.sinks if entry.kind == sink_name), None)
    return sink is not None and config_digest(sink) != gap.raised_with


_FIX_RULES: Mapping[str, FixRule] = {
    _PORTAL_MISSING: _portal_missing_fixed,
    _PORTAL_BROKEN: _portal_broken_fixed,
    _TEMPLATE_INVALID: _template_invalid_fixed,
}


async def _retry_fetch(conn: AsyncConnection[Any], invoice_id: int) -> bool:
    """Send a manual invoice back to its fetch step (D157)."""
    return await retry_documents(conn, invoice_id, MANUAL_REQUIRED)


#: Per kind: the rule its failing step leaves the invoice under, and how to resume it (D157).
#: The rule keeps an invoice linked to an old gap, now waiting for another reason, where it is.
_RESUME: Mapping[str, tuple[str, Resume]] = {
    _PORTAL_MISSING: (RULE_EXHAUSTED, _retry_fetch),
    _PORTAL_BROKEN: (RULE_EXHAUSTED, _retry_fetch),
    _TEMPLATE_INVALID: (RULE_TEMPLATE_INVALID, resume_delivery),
}


async def reevaluate(pool: Pool, cfg: Config, directory: StoreDirectory) -> int:
    """Fix the gaps the loaded config resolves and resume their invoices (R-11.9, D157).

    One transaction fixes every :func:`invoicebot.store.gaps.fixable_gaps` row
    whose per-kind rule holds, sends each waiting invoice back to its step and
    queues BR-F12 to its sender. A fixed gap with no waiting invoice closes at
    once. Returns the number of fixed gaps and logs one ``gaps reevaluated``
    line with the fixed and resumed counts.
    """
    fixed = 0
    resumed = 0
    async with pool.connection() as conn, conn.transaction():
        for gap in await fixable_gaps(conn):
            if not _FIX_RULES[gap.kind](gap, cfg, directory):
                continue
            await mark_fixed(conn, gap.fingerprint)
            rule_id, resume = _RESUME[gap.kind]
            invoice_ids = await stuck_invoices(conn, gap.fingerprint, rule_id)
            for invoice_id in invoice_ids:
                await resume(conn, invoice_id)
                await _queue_resumed(conn, invoice_id)
                resumed += 1
            if not invoice_ids:
                await close_gap(conn, gap, cfg.gaps.github.repo)
            fixed += 1
    logger.info("gaps reevaluated", extra={"fixed": fixed, "resumed": resumed})
    return fixed


async def _queue_resumed(conn: AsyncConnection[Any], invoice_id: int) -> None:
    """Queue BR-F12 for the invoice's sender in the caller's transaction."""
    head = await load_invoice_head(conn, invoice_id)
    if head is None:
        return
    await queue_message(
        conn,
        Addressee(head.origin_channel, head.profile_id),
        invoice_id=invoice_id,
        rule_id=RULE_RESUMED,
        text=MESSAGES[RULE_RESUMED].format(id=invoice_id),
    )
