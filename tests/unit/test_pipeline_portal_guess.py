"""Tests for the portal URL repaired from a URL strip (BR-F14, D191)."""

from __future__ import annotations

import pytest

from invoicebot.extract.fields import MAX_PRINTED_URL_CHARS
from invoicebot.pipeline.portal_guess import repair_url

ISSUER = "TIENDA SOL S.A.C."
EMPTY_HOSTS: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    ("strip", "issuer_name", "known_hosts", "expected"),
    [
        ("hitps.//sy.example pe/tiemda", ISSUER, EMPTY_HOSTS, "https://sy.example.pe/tienda"),
        (
            "hitps.//sy.example pe/tiemda",
            ISSUER,
            frozenset({"sv.example.pe"}),
            "https://sv.example.pe/tienda",
        ),
        (
            "hitps.//sy.example pe/tiemda",
            ISSUER,
            frozenset({"other.example.com"}),
            "https://sy.example.pe/tienda",
        ),
        (
            "https://portal.example.pe/tienda",
            ISSUER,
            EMPTY_HOSTS,
            "https://portal.example.pe/tienda",
        ),
        ("portal.example.pe", None, EMPTY_HOSTS, "https://portal.example.pe"),
        ("hitps.//portal.example pe/abc123", ISSUER, EMPTY_HOSTS, "https://portal.example.pe/abc123"),
        ("hitps.//portal.example pe/tiemda", None, EMPTY_HOSTS, "https://portal.example.pe/tiemda"),
    ],
)
def test_repair_url_returns_the_repaired_portal_url(
    strip: str,
    issuer_name: str | None,
    known_hosts: frozenset[str],
    expected: str,
) -> None:
    """D191: the strip's host, known hosts and issuer name repair it into the offered URL."""
    assert repair_url(strip, issuer_name, known_hosts) == expected


@pytest.mark.parametrize(
    "strip",
    [
        "hiips:/is etomi, aio",
        "https://e-consulta.sunat.gob.pe/x",
        "https://xn--prtal-9ta.pe/x",
        "   ",
    ],
)
def test_repair_url_refuses_a_strip_that_is_not_a_portal(strip: str) -> None:
    """D191: a top level outside the accepted ones, SUNAT's host, punycode or blank is refused."""
    assert repair_url(strip, None, EMPTY_HOSTS) is None


def test_repair_url_refuses_a_url_over_the_cap() -> None:
    """D191: a repaired URL longer than MAX_PRINTED_URL_CHARS is not offered."""
    strip = "hitps.//portal.example.pe/" + "a" * MAX_PRINTED_URL_CHARS
    assert repair_url(strip, None, EMPTY_HOSTS) is None


@pytest.mark.parametrize(
    ("strip", "expected"),
    [
        ("https: //sv.example.pe/x", "https://sv.example.pe/x"),
        ("https: sv.example.pe/x", "https://sv.example.pe/x"),
        ("https sv.example.pe/x", "https://sv.example.pe/x"),
        ("hub.pe/tienda", "https://hub.pe/tienda"),
        ("hub.pe/otro.com/x", "https://hub.pe/otro.com/x"),
    ],
)
def test_repair_url_keeps_the_host_after_a_broken_scheme(strip: str, expected: str) -> None:
    """D191, BR-F14: a scheme that lost its marks is dropped, and a host starting with h is kept."""
    assert repair_url(strip, None, frozenset()) == expected
