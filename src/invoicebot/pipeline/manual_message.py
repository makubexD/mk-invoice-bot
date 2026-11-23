"""Build BR-F07's text for an invoice no fetcher could complete (BR-F07, S7.4).

The message names the fetchers that failed, in the order they were first tried, and
says which documents the sender can supply by hand. Store facts (the portal host and
the comercio's name) come from the store directory; the flow decides whether SUNAT's
"not applicable" line belongs in the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from invoicebot.config.checks import MANUAL_PORTAL
from invoicebot.config.models import Flow, Portal, Store, StoreDirectory
from invoicebot.extract.fields import Reading
from invoicebot.fetchers.base import TECHNICAL_REASONS as TECHNICAL_REASONS
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.chain import Attempt
from invoicebot.rules.messages_es import (
    FETCH_AUTH_LINE,
    FETCH_NO_PORTAL_LINE,
    FETCH_NOT_FOUND_LINE,
    FETCH_PORTAL_TODO_LINE,
    FETCH_TECHNICAL_LINE,
    MESSAGES,
    STORE_FALLBACK,
    STORE_FALLBACK_OF,
    docs_desc,
    fetcher_desc,
)
from invoicebot.sunat_origins import SUNAT_SOL

#: BR-F07: the rule that reports an invoice nobody could fetch.
RULE_EXHAUSTED = "BR-F07"

#: The fetcher whose retry schedule comes from the store's portal (D65); BR-F03 names it.
PORTAL_FETCHER = "portal_registry"

#: The BR-F message of a fetcher's chain-final reason (spec 06 §1, BR-F07).
_REASON_RULES: dict[FetchReason, str] = {
    FetchReason.XML_MISMATCH: "BR-F06",
    FetchReason.GUARD_BLOCKED: "BR-F08",
    FetchReason.XML_INVALID: "BR-F09",
}


def manual_message(
    reading: Reading,
    flow: Flow,
    attempts: Sequence[Attempt],
    directory: StoreDirectory,
    gap_line: str = "",
    portal_broken: bool = False,
) -> str:
    """Return BR-F07's message for an invoice no fetcher could complete (BR-F07).

    With ``portal_broken``, the portal fetcher's technical failure bullet is
    BR-F03's line instead of the plain technical one (BR-F03).
    """
    store = _store_for(reading, directory)
    portal = portal_for(reading.value("ruc_emisor") or "", directory)
    fetcher_lines = _fetcher_lines(attempts, store, portal, portal_broken)
    if SUNAT_SOL not in flow.fetchers:
        fetcher_lines = "\n".join(line for line in (fetcher_lines, MESSAGES["BR-F01"]) if line)
    return (
        MESSAGES[RULE_EXHAUSTED]
        .format(
            serie=reading.value("serie") or "",
            numero=reading.value("numero") or "",
            fetcher_lines=fetcher_lines,
            manual_docs_desc=docs_desc(flow.required_docs),
            gap_line=gap_line,
        )
        .rstrip()
    )


def _store_for(reading: Reading, directory: StoreDirectory) -> Store | None:
    """Return the issuer's store, or None when the directory does not know it (R-5.8)."""
    return directory.stores.get(reading.value("ruc_emisor") or "")


def _fetcher_lines(
    attempts: Sequence[Attempt],
    store: Store | None,
    portal: Portal | None,
    portal_broken: bool,
) -> str:
    """Return one BR-F07 bullet per fetcher in first-attempt order, from its last reason (BR-F07)."""
    last: dict[str, FetchReason] = {attempt.fetcher: attempt.reason for attempt in attempts}
    lines = (_reason_line(fetcher, reason, store, portal, portal_broken) for fetcher, reason in last.items())
    return "\n".join(line for line in lines if line)


#: BR-F07 bullets that name no store-specific value (S7.4).
_FIXED_LINES: dict[FetchReason, str] = {
    FetchReason.NO_PORTAL: FETCH_NO_PORTAL_LINE,
    FetchReason.AUTH_REQUIRED: FETCH_AUTH_LINE,
}


def _reason_line(
    fetcher: str,
    reason: FetchReason,
    store: Store | None,
    portal: Portal | None,
    portal_broken: bool,
) -> str:
    """Return the BR-F07 bullet for one fetcher's final reason (BR-F07, BR-F03)."""
    if reason is FetchReason.NO_URL:
        return ""
    if portal_broken and fetcher == PORTAL_FETCHER and reason in TECHNICAL_REASONS:
        return MESSAGES["BR-F03"].format(portal=_portal_desc(store, portal))
    if reason in (FetchReason.PORTAL_INACTIVE, FetchReason.CAPTCHA_UNSOLVABLE):
        return _portal_line(reason, store, portal)
    if reason is FetchReason.NO_WEBSITE:
        return MESSAGES["BR-F11"].format(comercio=_comercio(store))
    return _fetcher_line(fetcher, reason)


def _portal_line(reason: FetchReason, store: Store | None, portal: Portal | None) -> str:
    """Return a BR-F07 bullet that names the store's portal (BR-F07)."""
    template = FETCH_PORTAL_TODO_LINE if reason is FetchReason.PORTAL_INACTIVE else MESSAGES["BR-F04"]
    return template.format(portal=_portal_desc(store, portal))


def _fetcher_line(fetcher: str, reason: FetchReason) -> str:
    """Return a BR-F07 bullet that names the fetcher or its rule (BR-F07)."""
    if reason in TECHNICAL_REASONS:
        return FETCH_TECHNICAL_LINE.format(fetcher=fetcher_desc(fetcher))
    fixed = _FIXED_LINES.get(reason)
    if fixed is not None:
        return fixed
    rule_id = _REASON_RULES.get(reason)
    if rule_id is None:
        return FETCH_NOT_FOUND_LINE.format(fetcher=fetcher_desc(fetcher))
    return MESSAGES[rule_id].format(fetcher=fetcher_desc(fetcher))


def _comercio(store: Store | None) -> str:
    """Return BR-F11's ``{comercio}``: the store's first brand, or a generic name (BR-F07)."""
    return STORE_FALLBACK if store is None else store.brands[0].comercio


def _portal_desc(store: Store | None, portal: Portal | None) -> str:
    """Return ``{portal}``: the portal URL's host, else its key, else a generic name (BR-F07)."""
    if store is None or store.portal is None or store.portal == MANUAL_PORTAL:
        return STORE_FALLBACK_OF
    if portal is None or portal.url is None:
        return store.portal
    return urlsplit(portal.url).hostname or store.portal


def portal_for(ruc_emisor: str, directory: StoreDirectory) -> Portal | None:
    """Return the issuer's configured portal, or None when it has none (R-5.8)."""
    store = directory.stores.get(ruc_emisor)
    if store is None or store.portal is None or store.portal == MANUAL_PORTAL:
        return None
    return directory.portals.get(store.portal)
