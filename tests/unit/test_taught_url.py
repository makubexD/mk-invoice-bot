"""Tests for taught portal link validation (R-11.4)."""

from __future__ import annotations

import pytest

from invoicebot.net.guard import GuardReason, Resolver
from invoicebot.net.taught_url import (
    TaughtUrlError,
    TaughtUrlReason,
    check_taught_url,
    is_dont_know,
    without_session,
)

PUBLIC_ADDRESS = "93.184.216.34"
GOOD_URL = "https://portal.example.pe/consulta"


class FakeResolver(Resolver):
    """A ``Resolver`` that answers from a fixed list of addresses."""

    def __init__(self, addresses: list[str]) -> None:
        self.addresses = addresses

    async def resolve(self, host: str) -> list[str]:
        """Return the fixed addresses, whatever the host is."""
        return list(self.addresses)


def _url_of_length(length: int) -> str:
    """Return a valid-shaped https URL of exactly ``length`` characters."""
    base = "https://portal.example.pe/"
    return base + "a" * (length - len(base))


async def test_check_taught_url_accepts_a_public_https_link() -> None:
    """R-11.4: an https link on a public host is returned, stripped."""
    resolver = FakeResolver([PUBLIC_ADDRESS])
    assert await check_taught_url(f"  {GOOD_URL}  ", resolver) == GOOD_URL


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://portal.example.pe/consulta", GuardReason.HTTP_NOT_ALLOWED),
        ("javascript:alert(1)", GuardReason.SCHEME_BLOCKED),
        (_url_of_length(301), TaughtUrlReason.TOO_LONG),
        ("https://u:p@portal.example.pe/", GuardReason.BAD_URL),
        ("https://10.0.0.1/", TaughtUrlReason.IP_LITERAL),
        ("https://[::1]/", TaughtUrlReason.IP_LITERAL),
        ("https://portal.example.pe/consulta#top", TaughtUrlReason.FRAGMENT),
    ],
)
async def test_check_taught_url_refuses_with_its_reason(
    text: str, expected: GuardReason | TaughtUrlReason
) -> None:
    """R-11.4: a non-https, long, credentialed, IP-literal or fragmented link is refused."""
    with pytest.raises(TaughtUrlError) as raised:
        await check_taught_url(text, FakeResolver([PUBLIC_ADDRESS]))
    assert raised.value.reason is expected


async def test_check_taught_url_refuses_a_host_resolving_to_a_private_address() -> None:
    """R-11.4: a host answering a private address is refused (PRIVATE_ADDRESS)."""
    with pytest.raises(TaughtUrlError) as raised:
        await check_taught_url(GOOD_URL, FakeResolver(["127.0.0.1"]))
    assert raised.value.reason is GuardReason.PRIVATE_ADDRESS


async def test_check_taught_url_refuses_a_host_resolving_to_no_address() -> None:
    """R-11.4: a host with no answer is refused (UNRESOLVABLE)."""
    with pytest.raises(TaughtUrlError) as raised:
        await check_taught_url(GOOD_URL, FakeResolver([]))
    assert raised.value.reason is GuardReason.UNRESOLVABLE


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://p.example.pe/(S(abc123))/frm.aspx", "https://p.example.pe/frm.aspx"),
        ("https://p.example.pe/a/(S(x))/(F(y))/b", "https://p.example.pe/a/b"),
        ("https://p.example.pe/a;JSESSIONID=9F/b?x=1", "https://p.example.pe/a/b?x=1"),
        ("https://p.example.pe/a(1)/b", "https://p.example.pe/a(1)/b"),
        ("https://p.example.pe/", "https://p.example.pe/"),
        ("https://p.example.pe/(S(x))", "https://p.example.pe/"),
    ],
)
def test_without_session_removes_only_session_addresses(url: str, expected: str) -> None:
    """D193: session segments and ``;jsessionid=`` suffixes go; the rest of the link stays."""
    assert without_session(url) == expected


async def test_check_taught_url_returns_the_link_without_its_session_address() -> None:
    """D193: a taught link keeps its path but loses the session address the portal added."""
    resolver = FakeResolver([PUBLIC_ADDRESS])
    assert (
        await check_taught_url("https://p.example.pe/(S(abc123))/frm.aspx", resolver)
        == "https://p.example.pe/frm.aspx"
    )


@pytest.mark.parametrize("text", ["no sé", "no se", "nose", " NO  SÉ "])
def test_is_dont_know_accepts_the_forms(text: str) -> None:
    """R-11.4: "no sé", "no se" and "nose" are "don't know" in any case and spacing."""
    assert is_dont_know(text) is True


@pytest.mark.parametrize("text", ["", "no", "sí", "no see", "no sé nada"])
def test_is_dont_know_refuses_other_text(text: str) -> None:
    """R-11.4: any other text is not a "don't know" answer."""
    assert is_dont_know(text) is False
