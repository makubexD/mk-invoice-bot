"""Validation of a portal link a sender teaches in chat (R-11.4).

The URL and address checks are the request guard's in :mod:`invoicebot.net.guard`; this module
adds only the taught-link limits: at most 300 visible ASCII characters, the default port, no fragment
and a plain host that is neither an IP literal, punycode nor SUNAT's.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from enum import StrEnum
from urllib.parse import urlsplit

from invoicebot.extract.fields import is_printed_host
from invoicebot.net.guard import GuardReason, Resolver, check_url, resolve_and_check
from invoicebot.sunat_origins import is_sunat_host

#: Longest a taught link may be, in characters (R-11.4).
MAX_TAUGHT_URL_CHARS = 300
#: The BR-F02 answer that says the sender does not know the portal, after normalization.
DONT_KNOW_FORMS = frozenset({"nose"})
#: Visible ASCII only: ``urlsplit`` drops tabs and newlines, so the host checked could differ from
#: the host shown (D191).
_URL_CHARS_PATTERN = re.compile(r"[!-~]+")
_HTTPS_PORT = 443


class TaughtUrlReason(StrEnum):
    """Why a taught link was refused, beyond the request guard's own reason codes."""

    TOO_LONG = "too_long"
    IP_LITERAL = "ip_literal"
    FRAGMENT = "fragment"
    BAD_CHARS = "bad_chars"
    BAD_HOST = "bad_host"
    BAD_PORT = "bad_port"
    SUNAT = "sunat"


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


def looks_like_url(text: str) -> bool:
    """Return whether ``text`` is one token whose scheme is ``https`` (R-11.4).

    Shape only: no DNS, length or host checks. :func:`check_taught_url` performs
    those later and its refusal repeats BR-F02.
    """
    stripped = text.strip()
    if not stripped or any(char.isspace() for char in stripped):
        return False
    try:
        return urlsplit(stripped).scheme.lower() == "https"
    except ValueError:
        return False


def taught_url_refusal(url: str) -> GuardReason | TaughtUrlReason | None:
    """Return why ``url`` may not be offered or taught, or None; nothing is resolved (R-11.4, D191)."""
    if len(url) > MAX_TAUGHT_URL_CHARS:
        return TaughtUrlReason.TOO_LONG
    if _URL_CHARS_PATTERN.fullmatch(url) is None:
        return TaughtUrlReason.BAD_CHARS
    refusal = check_url(url, frozenset())
    if refusal is not None:
        return refusal
    parts = urlsplit(url)
    if parts.fragment:
        return TaughtUrlReason.FRAGMENT
    return _host_refusal(parts.hostname or "", parts.port)


def _host_refusal(host: str, port: int | None) -> TaughtUrlReason | None:
    """Return why a link to ``host`` and ``port`` is refused, or None (R-11.4, D191)."""
    if _is_ip_literal(host):
        return TaughtUrlReason.IP_LITERAL
    if not is_printed_host(host):
        return TaughtUrlReason.BAD_HOST
    if port not in (None, _HTTPS_PORT):
        return TaughtUrlReason.BAD_PORT
    return TaughtUrlReason.SUNAT if is_sunat_host(host) else None


async def check_taught_url(text: str, resolver: Resolver) -> str:
    """Return the cleaned link ``text`` teaches, or raise :class:`TaughtUrlError` (R-11.4).

    The link must pass :func:`taught_url_refusal` and its host must resolve only to
    addresses the request guard allows.
    """
    url = text.strip()
    refusal = taught_url_refusal(url)
    if refusal is not None:
        raise TaughtUrlError(refusal)
    _, unsafe = await resolve_and_check(urlsplit(url).hostname or "", resolver)
    if unsafe is not None:
        raise TaughtUrlError(unsafe)
    return url


def _is_ip_literal(host: str) -> bool:
    """Return whether ``host`` is an IPv4 or IPv6 literal, brackets already stripped."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
