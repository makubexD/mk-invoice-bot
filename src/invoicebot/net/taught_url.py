"""Validation of a portal link a sender teaches in chat (R-11.4).

The URL and address checks are the request guard's in :mod:`invoicebot.net.guard`; this module
adds only the taught-link limits: at most 300 characters, no fragment and no IP-literal host.
"""

from __future__ import annotations

import ipaddress
import unicodedata
from enum import StrEnum
from urllib.parse import urlsplit

from invoicebot.net.guard import GuardReason, Resolver, check_url, resolve_and_check

#: Longest a taught link may be, in characters (R-11.4).
MAX_TAUGHT_URL_CHARS = 300
#: The BR-F02 answer that says the sender does not know the portal, after normalization.
DONT_KNOW_FORMS = frozenset({"nose"})


class TaughtUrlReason(StrEnum):
    """Why a taught link was refused, beyond the request guard's own reason codes."""

    TOO_LONG = "too_long"
    IP_LITERAL = "ip_literal"
    FRAGMENT = "fragment"


class TaughtUrlError(ValueError):
    """A taught link was refused; ``reason`` says why."""

    def __init__(self, reason: GuardReason | TaughtUrlReason) -> None:
        super().__init__(str(reason))
        self.reason = reason


def is_dont_know(text: str) -> bool:
    """Return whether ``text`` answers BR-F02 with "don't know" (R-11.4).

    Accepts "no sé", "no se" and "nose" in any case and spacing.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(without_accents.split()) in DONT_KNOW_FORMS


async def check_taught_url(text: str, resolver: Resolver) -> str:
    """Return the cleaned link ``text`` teaches, or raise :class:`TaughtUrlError` (R-11.4).

    The link must be a public ``https`` URL of at most :data:`MAX_TAUGHT_URL_CHARS`
    characters, with no credentials, fragment or IP-literal host, and its host must resolve
    only to addresses the request guard allows.
    """
    url = text.strip()
    if len(url) > MAX_TAUGHT_URL_CHARS:
        raise TaughtUrlError(TaughtUrlReason.TOO_LONG)
    refusal = check_url(url, frozenset())
    if refusal is not None:
        raise TaughtUrlError(refusal)
    parts = urlsplit(url)
    if parts.fragment:
        raise TaughtUrlError(TaughtUrlReason.FRAGMENT)
    host = parts.hostname
    if host is None:
        # check_url refused a URL with no host already; this only narrows the type.
        raise TaughtUrlError(GuardReason.BAD_URL)
    if _is_ip_literal(host):
        raise TaughtUrlError(TaughtUrlReason.IP_LITERAL)
    _, refusal = await resolve_and_check(host, resolver)
    if refusal is not None:
        raise TaughtUrlError(refusal)
    return url


def _is_ip_literal(host: str) -> bool:
    """Return whether ``host`` is an IPv4 or IPv6 literal, brackets already stripped."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
