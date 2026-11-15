"""Decide which QR document URLs the bot may request (R-5.2, R-5.10).

A scanned QR code points at a document on SUNAT or on a configured portal. Only
``https`` URLs on those hosts are fetched; everything else is refused before a
request is made.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from invoicebot.config.models import StoreDirectory

SUNAT_DOMAIN = "sunat.gob.pe"


def allowed_hosts(directory: StoreDirectory) -> frozenset[str]:
    """Return the lowercased hosts of every portal that has a URL (R-5.2)."""
    hosts: set[str] = set()
    for portal in directory.portals.values():
        if portal.url is None:
            continue
        host = urlsplit(portal.url).hostname
        if host is not None:
            hosts.add(host.lower())
    return frozenset(hosts)


def is_allowed(url: str, hosts: frozenset[str]) -> bool:
    """Return whether ``url`` is an https URL on SUNAT or an allowed host (R-5.2, R-5.10)."""
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = parts.hostname
    if host is None:
        return False
    host = host.lower()
    return host == SUNAT_DOMAIN or host.endswith("." + SUNAT_DOMAIN) or host in hosts
