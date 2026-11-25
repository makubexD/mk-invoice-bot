"""Tests for the candidate URLs read from a URL strip (BR-F14, D191, D192)."""

from __future__ import annotations

import pytest

from invoicebot.extract.fields import MAX_PRINTED_URL_CHARS
from invoicebot.pipeline.portal_guess import MAX_CANDIDATES, candidate_urls

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
            "https://sy.example.pe/tienda",
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
def test_candidate_urls_returns_the_repaired_portal_url(
    strip: str,
    issuer_name: str | None,
    known_hosts: frozenset[str],
    expected: str,
) -> None:
    """D191: the strip's host, known hosts and issuer name repair it into the offered URL."""
    assert candidate_urls(strip, issuer_name, known_hosts)[0] == expected


@pytest.mark.parametrize(
    "strip",
    [
        "hiips:/is etomi, aio",
        "https://e-consulta.sunat.gob.pe/x",
        "https://xn--prtal-9ta.pe/x",
        "   ",
    ],
)
def test_candidate_urls_returns_nothing_for_a_refused_strip(strip: str) -> None:
    """D191: a top level outside the accepted ones, SUNAT's host, punycode or blank is refused."""
    assert candidate_urls(strip, None, EMPTY_HOSTS) == ()


def test_candidate_urls_returns_nothing_for_a_url_over_the_cap() -> None:
    """D191: a repaired URL longer than MAX_PRINTED_URL_CHARS is not offered."""
    strip = "hitps.//portal.example.pe/" + "a" * MAX_PRINTED_URL_CHARS
    assert candidate_urls(strip, None, EMPTY_HOSTS) == ()


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
def test_candidate_urls_keeps_the_host_after_a_broken_scheme(strip: str, expected: str) -> None:
    """D191, BR-F14: a scheme that lost its marks is dropped, and a host starting with h is kept."""
    assert candidate_urls(strip, None, frozenset())[0] == expected


@pytest.mark.parametrize(
    ("strip", "expected"),
    [
        ("harps: sv.example.pe/x", "https://sv.example.pe/x"),
        ("htips sv.example.pe/x", "https://sv.example.pe/x"),
        ("hotel.example.pe/x", "https://hotel.example.pe/x"),
        ("hotel example.pe/x", "https://hotel.example.pe/x"),
        ("portal.example.corn/x", "https://portal.example.com/x"),
        ("portal.example.corn.pe", "https://portal.example.com.pe"),
        ("portal.example.compe/x", "https://portal.example.com.pe/x"),
        ("wwv.example.pe/x", "https://www.example.pe/x"),
        ("https://portal.example.pe/tienda.", "https://portal.example.pe/tienda"),
        ("sv.example. pe/x", "https://sv.example.pe/x"),
    ],
)
def test_candidate_urls_fixes_more_ocr_slips(strip: str, expected: str) -> None:
    """D192: a garbled scheme, a mangled label or a trailing path period is repaired or refused."""
    assert candidate_urls(strip, None, EMPTY_HOSTS)[0] == expected


def test_candidate_urls_keeps_a_label_that_only_contains_corn() -> None:
    """D192: a label such as popcorn is not rewritten to com."""
    assert candidate_urls("popcorn.example.pe/x", None, EMPTY_HOSTS)[0] == "https://popcorn.example.pe/x"


def test_candidate_urls_puts_the_issuer_repair_then_the_read_path_before_the_known_host() -> None:
    """BR-F14, D192: the issuer-repaired path, the read path, then the known host, in that order."""
    urls = candidate_urls("hitps.//sy.example pe/tiemda", ISSUER, frozenset({"sv.example.pe"}))
    assert urls[:2] == ("https://sy.example.pe/tienda", "https://sy.example.pe/tiemda")
    assert urls.index("https://sv.example.pe/tienda") > 1


@pytest.mark.parametrize(
    "text",
    [
        "https://portal.example.pe/Tienda/ABC",
        "HTTPS://portal.example.pe/Tienda/ABC",
    ],
)
def test_candidate_urls_keeps_the_path_case_then_lowers_it(text: str) -> None:
    """BR-F14, D192: an uppercase scheme is dropped and the path is tried with, then without, its capitals."""
    assert candidate_urls(text, None, EMPTY_HOSTS)[:2] == (
        "https://portal.example.pe/Tienda/ABC",
        "https://portal.example.pe/tienda/abc",
    )


def test_candidate_urls_tries_the_bare_host_then_www() -> None:
    """BR-F14, D192: a strip with no path gives the bare host and then the same host with www."""
    assert candidate_urls("https://portal.example.pe", None, EMPTY_HOSTS)[:2] == (
        "https://portal.example.pe",
        "https://www.portal.example.pe",
    )


def test_candidate_urls_removes_a_leading_www() -> None:
    """BR-F14, D192: a host that already has www is also tried without it."""
    assert candidate_urls("https://www.portal.example.pe/x", None, EMPTY_HOSTS)[1] == (
        "https://portal.example.pe/x"
    )


def test_candidate_urls_changes_one_lookalike() -> None:
    """BR-F14, D192: a digit in the host is also tried as its look-alike letter."""
    assert "https://portal.example.pe/x" in candidate_urls("https://p0rtal.example.pe/x", None, EMPTY_HOSTS)


def test_candidate_urls_tries_the_spaces_removed_after_the_dotted_host() -> None:
    """BR-F14, D192: a host read with a space is tried dotted first, then with the space removed."""
    urls = candidate_urls("por tal.example.pe/x", None, EMPTY_HOSTS)
    assert urls[0] == "https://por.tal.example.pe/x"
    assert urls[1] == "https://portal.example.pe/x"


def test_candidate_urls_caps_at_max_candidates_without_repeats() -> None:
    """BR-F14, D192: a host with many look-alikes stops at eight different URLs."""
    urls = candidate_urls("solol1.mill0.pe/x", None, EMPTY_HOSTS)
    assert len(urls) == MAX_CANDIDATES
    assert len(set(urls)) == MAX_CANDIDATES


@pytest.mark.parametrize(
    "text",
    [
        "hiips:/is etomi, aio",
        "   ",
        "https://e-consulta.sunat.gob.pe/x",
    ],
)
def test_candidate_urls_returns_nothing_for_an_unreadable_or_sunat_strip(text: str) -> None:
    """BR-F14, D192: a strip with no accepted portal host, or a SUNAT host, gives no candidate."""
    assert candidate_urls(text, None, EMPTY_HOSTS) == ()


def test_candidate_urls_never_offers_a_close_known_sunat_host() -> None:
    """BR-F14, D192: a known SUNAT host close to the read one is not a candidate."""
    urls = candidate_urls("https://e-consulta.sunat.gob.com/x", None, frozenset({"e-consulta.sunat.gob.pe"}))
    assert urls[0] == "https://e-consulta.sunat.gob.com/x"
    assert "https://e-consulta.sunat.gob.pe/x" not in urls


def test_candidate_urls_leaves_the_path_alone_when_the_issuer_does_not_match_it() -> None:
    """BR-F14, D192: an issuer with no path word leaves the read path, then its lowercase, to be tried."""
    assert candidate_urls("https://portal.example.pe/Otro", ISSUER, EMPTY_HOSTS)[:2] == (
        "https://portal.example.pe/Otro",
        "https://portal.example.pe/otro",
    )
