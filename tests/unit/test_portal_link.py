"""Tests for the open-portal link of BR-F07 and BR-F13 (D208)."""

from __future__ import annotations

from invoicebot.config.models import Portal
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.chain import Attempt
from invoicebot.pipeline.manual_message import PORTAL_FETCHER
from invoicebot.pipeline.portal_link import open_portal_link
from invoicebot.rules.messages_es import LINK_OPEN_PORTAL

PORTAL_URL = "https://portal.ejemplo.pe/consulta"
TAUGHT_URL = "https://portal.ejemplo.pe/ensenado"


def _portal(url: str | None = PORTAL_URL, *, allow_http: bool = False) -> Portal:
    return Portal(key="synthetic_portal", adapter="generic_form", url=url, allow_http=allow_http)


def _attempt(reason: FetchReason, fetcher: str = PORTAL_FETCHER) -> Attempt:
    return Attempt(fetcher=fetcher, reason=reason)


def test_captcha_unsolvable_links_the_portal_url() -> None:
    """D208: a captcha the bot cannot solve links the portal's https URL."""
    link = open_portal_link([_attempt(FetchReason.CAPTCHA_UNSOLVABLE)], _portal(), None)
    assert link is not None
    assert (link.title, link.url) == (LINK_OPEN_PORTAL, PORTAL_URL)


def test_portal_inactive_links_the_portal_url() -> None:
    """D208: an inactive portal links its https URL."""
    link = open_portal_link([_attempt(FetchReason.PORTAL_INACTIVE)], _portal(), None)
    assert link is not None
    assert link.url == PORTAL_URL


def test_http_portal_url_gives_no_link() -> None:
    """D208: a portal URL that is not https gives no link."""
    portal = _portal("http://portal.ejemplo.pe/consulta", allow_http=True)
    assert open_portal_link([_attempt(FetchReason.CAPTCHA_UNSOLVABLE)], portal, None) is None


def test_http_portal_url_falls_back_to_the_taught_url() -> None:
    """D208: a portal URL that is not https gives way to the approved taught link."""
    portal = _portal("http://portal.ejemplo.pe/consulta", allow_http=True)
    link = open_portal_link([_attempt(FetchReason.CAPTCHA_UNSOLVABLE)], portal, TAUGHT_URL)
    assert link is not None
    assert link.url == TAUGHT_URL


def test_no_portal_links_the_taught_url() -> None:
    """D208: no portal entry and an approved taught link links the taught URL."""
    link = open_portal_link([_attempt(FetchReason.NO_PORTAL)], None, TAUGHT_URL)
    assert link is not None
    assert (link.title, link.url) == (LINK_OPEN_PORTAL, TAUGHT_URL)


def test_no_portal_without_taught_url_gives_no_link() -> None:
    """D208: no portal entry and no taught link gives no link."""
    assert open_portal_link([_attempt(FetchReason.NO_PORTAL)], None, None) is None


def test_portal_url_wins_over_taught_url() -> None:
    """D208: with a portal entry and a taught link, the portal's URL is used."""
    link = open_portal_link([_attempt(FetchReason.CAPTCHA_UNSOLVABLE)], _portal(), TAUGHT_URL)
    assert link is not None
    assert link.url == PORTAL_URL


def test_not_found_gives_no_link() -> None:
    """D208: a portal that answered "not found" gives no link."""
    assert open_portal_link([_attempt(FetchReason.NOT_FOUND)], _portal(), TAUGHT_URL) is None


def test_no_portal_attempt_gives_no_link() -> None:
    """D208: without a portal_registry attempt there is no link."""
    attempts = [_attempt(FetchReason.CAPTCHA_UNSOLVABLE, fetcher="sunat_sol")]
    assert open_portal_link(attempts, _portal(), TAUGHT_URL) is None


def test_last_portal_attempt_decides() -> None:
    """D208: the last portal attempt's reason decides, not an earlier one."""
    attempts = [_attempt(FetchReason.TECHNICAL), _attempt(FetchReason.CAPTCHA_UNSOLVABLE)]
    link = open_portal_link(attempts, _portal(), None)
    assert link is not None
    assert link.url == PORTAL_URL
