"""Re-evaluate open config gaps and resume the invoices they stopped (R-11.9, D157).

:func:`reevaluate` runs in one transaction over
:func:`invoicebot.store.gaps.fixable_gaps`. For each gap whose per-kind fix
rule now holds it marks the gap fixed and sends its still-waiting invoices back
to the step that failed, telling each sender with BR-F12. A fixed gap with no
waiting invoice closes at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from invoicebot.config.digest import config_digest
from invoicebot.config.models import Config, StoreDirectory
from invoicebot.gaps.kinds import GAP_KINDS
from invoicebot.pipeline.gaps import close_gap
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
            invoice_ids = await stuck_invoices(conn, gap.fingerprint)
            for invoice_id in invoice_ids:
                await _resume_invoice(conn, invoice_id, gap.kind)
                resumed += 1
            if not invoice_ids:
                await close_gap(conn, gap, cfg.gaps.github.repo)
            fixed += 1
    logger.info("gaps reevaluated", extra={"fixed": fixed, "resumed": resumed})
    return fixed


async def _resume_invoice(conn: AsyncConnection[Any], invoice_id: int, kind: str) -> None:
    """Send one waiting invoice back to its failing step and tell its sender (R-11.9, BR-F12)."""
    if kind == _TEMPLATE_INVALID:
        await resume_delivery(conn, invoice_id)
    else:
        await retry_documents(conn, invoice_id, MANUAL_REQUIRED)
    await _queue_resumed(conn, invoice_id)


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
