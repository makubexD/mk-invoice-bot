"""Pure URL and address checks for outbound requests (R-5.10).

Nothing here opens a socket or reads a clock: the functions take a URL or a resolver answer
and return a reason code, so the same checks run in tests and in the browser helper (D82).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

#: Carrier-grade NAT; not ``is_private`` but unreachable from the public internet.
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
#: NAT64; the last 32 bits are the IPv4 address the request would really reach.
NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
#: IPv6 site-local; deprecated, not ``is_private``, but some legacy networks still route it.
SITE_LOCAL_NETWORK = ipaddress.ip_network("fec0::/10")
_IPV4_MASK = 0xFFFFFFFF
#: Schemes that may reach the network at all; everything else is blocked outright.
NETWORK_SCHEMES = frozenset({"http", "https"})


class GuardReason(StrEnum):
    """Why a request was refused; the values are the codes BR-F08 records."""

    SCHEME_BLOCKED = "scheme_blocked"
    HTTP_NOT_ALLOWED = "http_not_allowed"
    PRIVATE_ADDRESS = "private_address"
    UNRESOLVABLE = "unresolvable"
    BAD_URL = "bad_url"


class Resolver(Protocol):
    """Resolves a host to IP address strings.

    Invariant: returns every address the host resolves to, and an empty list when the host
    resolves to none.
    """

    async def resolve(self, host: str) -> list[str]:
        """Return one address string per answer for ``host``."""
        ...


def check_url(url: str, http_hosts: frozenset[str]) -> GuardReason | None:
    """Return why ``url`` may not be requested, or ``None`` when it may."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return GuardReason.BAD_URL
    scheme = parts.scheme.lower()
    if scheme not in NETWORK_SCHEMES:
        return GuardReason.SCHEME_BLOCKED
    if parts.username is not None or parts.password is not None or not parts.hostname:
        return GuardReason.BAD_URL
    if scheme == "http" and parts.hostname.lower() not in http_hosts:
        return GuardReason.HTTP_NOT_ALLOWED
    return None


def check_addresses(addresses: Iterable[str]) -> GuardReason | None:
    """Return why any of ``addresses`` may not be contacted, or ``None`` when all may."""
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return GuardReason.BAD_URL
        if _is_blocked(address):
            return GuardReason.PRIVATE_ADDRESS
    return None


type CheckAddresses = Callable[[Iterable[str]], GuardReason | None]


async def resolve_and_check(
    host: str, resolver: Resolver, check: CheckAddresses = check_addresses
) -> tuple[str, GuardReason | None]:
    """Resolve ``host`` once and return the first address to pin, or the reason to refuse.

    ``check`` judges the whole answer; tests pass their own to allow loopback servers.
    """
    addresses = await resolver.resolve(host)
    if not addresses:
        return "", GuardReason.UNRESOLVABLE
    return addresses[0], check(addresses)


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            address = embedded
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address in CGNAT_NETWORK
        or address in SITE_LOCAL_NETWORK
    )


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 transition address wraps, or ``None``.

    Covers IPv4-mapped, 6to4 (``sixtofour``) and NAT64, whose last 32 bits hold the
    IPv4 address the request really reaches.
    """
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in NAT64_NETWORK:
        return ipaddress.IPv4Address(int(address) & _IPV4_MASK)
    return None
