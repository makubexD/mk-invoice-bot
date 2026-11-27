"""Choose the link BR-F07 and BR-F13 offer to a portal the bot cannot query (D208)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from invoicebot.channels.base import Link
from invoicebot.config.models import Portal
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.chain import Attempt
from invoicebot.pipeline.manual_message import PORTAL_FETCHER
from invoicebot.rules.messages_es import LINK_OPEN_PORTAL


def _last_portal_reason(attempts: Sequence[Attempt]) -> FetchReason | None:
    """Return the reason of the last portal attempt, or None when there was none."""
    reasons = [a.reason for a in attempts if a.fetcher == PORTAL_FETCHER]
    return reasons[-1] if reasons else None


def _candidate_url(reason: FetchReason | None, portal: Portal | None, taught_url: str | None) -> str | None:
    """Return the URL D208 (2) offers for this reason, before the https check."""
    if reason in (FetchReason.CAPTCHA_UNSOLVABLE, FetchReason.PORTAL_INACTIVE):
        return portal.url if portal is not None else None
    if reason is FetchReason.NO_PORTAL:
        return taught_url
    return None


def open_portal_link(
    attempts: Sequence[Attempt], portal: Portal | None, taught_url: str | None
) -> Link | None:
    """Return the "open the portal" link, or None when D208 (2) offers none."""
    url = _candidate_url(_last_portal_reason(attempts), portal, taught_url)
    if url is None:
        return None
    try:  # Link takes only a plain https URL (D194), so an http portal gives no link
        return Link(title=LINK_OPEN_PORTAL, url=url)
    except ValidationError:
        return None
