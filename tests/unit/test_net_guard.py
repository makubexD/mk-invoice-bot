"""Tests for the outbound request guard (R-5.10, BR-F08)."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from invoicebot.net.guard import GuardReason, Resolver, check_addresses, check_url, resolve_and_check

PUBLIC_ADDRESS = "93.184.216.34"
HTTP_HOSTS = frozenset({"portal.example"})


class FakeResolver(Resolver):
    """A ``Resolver`` that answers from a fixed list of addresses."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed addresses, whatever the host is."""
        return list(self.addresses)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://portal.example/invoice", None),
        ("https://PORTAL.EXAMPLE/invoice", None),
        ("http://portal.example/invoice", None),
        ("http://other.example/invoice", GuardReason.HTTP_NOT_ALLOWED),
        ("file:///etc/passwd", GuardReason.SCHEME_BLOCKED),
        ("data:text/plain,hello", GuardReason.SCHEME_BLOCKED),
        ("javascript:alert(1)", GuardReason.SCHEME_BLOCKED),
        ("ftp://portal.example/invoice", GuardReason.SCHEME_BLOCKED),
        ("/relative/path", GuardReason.SCHEME_BLOCKED),
        ("https://user:pw@portal.example/", GuardReason.BAD_URL),
        ("https:///path", GuardReason.BAD_URL),
    ],
)
def test_check_url(url: str, expected: GuardReason | None) -> None:
    """R-5.10: only https, and http for a listed host, pass the URL guard."""
    assert check_url(url, HTTP_HOSTS) == expected


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (("127.0.0.1",), GuardReason.PRIVATE_ADDRESS),
        (("10.0.0.1",), GuardReason.PRIVATE_ADDRESS),
        (("169.254.169.254",), GuardReason.PRIVATE_ADDRESS),
        (("100.64.0.1",), GuardReason.PRIVATE_ADDRESS),
        (("::1",), GuardReason.PRIVATE_ADDRESS),
        (("::ffff:127.0.0.1",), GuardReason.PRIVATE_ADDRESS),
        (("fe80::1",), GuardReason.PRIVATE_ADDRESS),
        (("fec0::1",), GuardReason.PRIVATE_ADDRESS),
        (("not-an-address",), GuardReason.BAD_URL),
        ((PUBLIC_ADDRESS,), None),
        ((PUBLIC_ADDRESS, "10.0.0.1"), GuardReason.PRIVATE_ADDRESS),
    ],
)
def test_check_addresses(addresses: tuple[str, ...], expected: GuardReason | None) -> None:
    """BR-F08: private, loopback, link-local, site-local, CGNAT and mapped addresses are refused."""
    assert check_addresses(addresses) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("64:ff9b::7f00:1", GuardReason.PRIVATE_ADDRESS),
        ("2002:7f00:1::", GuardReason.PRIVATE_ADDRESS),
        ("64:ff9b::808:808", None),
    ],
)
def test_check_addresses_checks_the_embedded_ipv4(address: str, expected: GuardReason | None) -> None:
    """R-5.10: a NAT64 or 6to4 address is checked through the IPv4 it embeds."""
    assert check_addresses((address,)) == expected


async def test_resolve_and_check_refuses_any_private_answer() -> None:
    """BR-F08: a host answering one public and one private address is refused (DNS rebinding)."""
    address, reason = await resolve_and_check("portal.example", FakeResolver([PUBLIC_ADDRESS, "10.0.0.1"]))
    assert (address, reason) == (PUBLIC_ADDRESS, GuardReason.PRIVATE_ADDRESS)


async def test_resolve_and_check_refuses_an_empty_answer() -> None:
    """R-5.10: a host with no addresses is unresolvable."""
    assert await resolve_and_check("portal.example", FakeResolver([])) == ("", GuardReason.UNRESOLVABLE)


async def test_resolve_and_check_returns_the_first_public_address() -> None:
    """R-5.10: a host with only public addresses yields the address to pin."""
    assert await resolve_and_check("portal.example", FakeResolver([PUBLIC_ADDRESS])) == (
        PUBLIC_ADDRESS,
        None,
    )


def _refuse_all(addresses: Iterable[str]) -> GuardReason:
    """Refuse any answer."""
    del addresses
    return GuardReason.PRIVATE_ADDRESS


async def test_resolve_and_check_uses_the_given_check() -> None:
    """R-5.10: the caller's check judges the answer, so the proxy shares this one resolve path."""
    resolver = FakeResolver([PUBLIC_ADDRESS])
    assert await resolve_and_check("portal.example", resolver, _refuse_all) == (
        PUBLIC_ADDRESS,
        GuardReason.PRIVATE_ADDRESS,
    )
