"""Choose the link BR-F07 and BR-F13 offer to a portal the bot cannot query (D208)."""

from __future__ import annotations

from collections.abc import Sequence

from invoicebot.channels.base import Link
from invoicebot.config.models import Portal
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.chain import Attempt
from invoicebot.pipeline.manual_message import PORTAL_FETCHER
from invoicebot.pipeline.outgoing import safe_links
from invoicebot.rules.messages_es import LINK_OPEN_PORTAL

#: The portal fetcher's last reasons for which the portal is known but not queried (D208 (2)).
_KNOWN_PORTAL_REASONS = (FetchReason.CAPTCHA_UNSOLVABLE, FetchReason.PORTAL_INACTIVE)


def _last_portal_reason(attempts: Sequence[Attempt]) -> FetchReason | None:
    """Return the reason of the last portal attempt, or None when there was none."""
    reasons = [attempt.reason for attempt in attempts if attempt.fetcher == PORTAL_FETCHER]
    return reasons[-1] if reasons else None


def _candidate_urls(reason: FetchReason | None, portal: Portal | None, taught_url: str | None) -> list[str]:
    """Return the URLs D208 (2) may offer for this reason, the preferred one first."""
    if reason in _KNOWN_PORTAL_REASONS:
        candidates = [portal.url if portal is not None else None, taught_url]
    elif reason is FetchReason.NO_PORTAL:
        candidates = [taught_url]
    else:
        candidates = []
    return [url for url in candidates if url is not None]


def open_portal_link(
    attempts: Sequence[Attempt], portal: Portal | None, taught_url: str | None
) -> Link | None:
    """Return the "open the portal" link, or None when D208 (2) offers none.

    The portal entry's URL comes first and the approved taught link second; ``Link`` takes
    only a plain https URL (D194), so an http portal falls back to the taught link.
    """
    pairs = [
        (LINK_OPEN_PORTAL, url) for url in _candidate_urls(_last_portal_reason(attempts), portal, taught_url)
    ]
    links = safe_links(pairs)
    return links[0] if links else None
