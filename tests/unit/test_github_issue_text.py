"""Snapshot and safety tests for the gap issue text (S8.11, R-11.12)."""

from invoicebot.fetchers.base import FetchReason
from invoicebot.github.issue_text import IssueFields, render_issue


def _portal_missing(*, ruc_emisor: int = 20100000001, hint_url: str | None = None) -> IssueFields:
    """Return a fully populated PORTAL_MISSING issue with a fixed fingerprint."""
    return IssueFields(
        kind="PORTAL_MISSING",
        fingerprint="portal_missing:20100000001",
        ruc_emisor=ruc_emisor,
        razon_social="Bodega Ñandú SAC",
        tipo="01",
        reasons=(FetchReason.NO_PORTAL, FetchReason.TIMEOUT),
        portal_key=None,
        hint_url=hint_url,
    )


def test_portal_missing_issue_text() -> None:
    """R-11.12: a PORTAL_MISSING issue lists typed fields and the fenced URL only."""
    title, body = render_issue(_portal_missing(hint_url="https://portal.example.pe/consulta?token=abc#top"))

    assert title == "gap: PORTAL_MISSING RUC 20100000001"
    assert body == (
        "**portal de descarga desconocido**\n"
        "\n"
        "- RUC emisor: 20100000001\n"
        "- Razón social: Bodega Ñandú SAC\n"
        "- Tipo: 01\n"
        "- Razones: no_portal, timeout\n"
        "\n"
        "```\n"
        "https://portal.example.pe/consulta\n"
        "```\n"
        "\n"
        "<!-- gap:portal_missing:20100000001 -->"
    )


def test_portal_broken_issue_text() -> None:
    """R-11.12: a PORTAL_BROKEN issue is titled with the portal key."""
    title, body = render_issue(
        IssueFields(
            kind="PORTAL_BROKEN",
            fingerprint="portal_broken:sunat_sol",
            ruc_emisor=None,
            razon_social="Tienda Example",
            tipo=None,
            reasons=(FetchReason.TECHNICAL,),
            portal_key="sunat_sol",
            hint_url="https://portal.example.pe/x",
        )
    )

    assert title == "gap: PORTAL_BROKEN sunat_sol"
    assert body == (
        "**portal con fallas**\n"
        "\n"
        "- Razón social: Tienda Example\n"
        "- Razones: technical\n"
        "\n"
        "```\n"
        "https://portal.example.pe/x\n"
        "```\n"
        "\n"
        "<!-- gap:portal_broken:sunat_sol -->"
    )


def test_template_invalid_issue_text() -> None:
    """R-11.12: a TEMPLATE_INVALID issue is titled with the flow and sink from its fingerprint."""
    title, body = render_issue(
        IssueFields(
            kind="TEMPLATE_INVALID",
            fingerprint="template_invalid:work:drive",
            ruc_emisor=None,
            razon_social=None,
            tipo=None,
            reasons=(),
            portal_key=None,
            hint_url=None,
        )
    )

    assert title == "gap: TEMPLATE_INVALID work/drive"
    assert body == ("**plantilla inválida**\n\n<!-- gap:template_invalid:work:drive -->")


def test_parser_miss_issue_text() -> None:
    """R-11.12: a PARSER_MISS issue carries the issuer RUC and the reason codes."""
    title, body = render_issue(
        IssueFields(
            kind="PARSER_MISS",
            fingerprint="parser_miss:20100000001",
            ruc_emisor=20100000001,
            razon_social="Ferretería Los Andes",
            tipo="01",
            reasons=(FetchReason.XML_INVALID, FetchReason.XML_MISMATCH),
            portal_key=None,
            hint_url=None,
        )
    )

    assert title == "gap: PARSER_MISS RUC 20100000001"
    assert body == (
        "**no puedo leer los comprobantes de un emisor**\n"
        "\n"
        "- RUC emisor: 20100000001\n"
        "- Razón social: Ferretería Los Andes\n"
        "- Tipo: 01\n"
        "- Razones: xml_invalid, xml_mismatch\n"
        "\n"
        "<!-- gap:parser_miss:20100000001 -->"
    )


def test_unrouted_buyer_issue_text() -> None:
    """R-11.12: an UNROUTED_BUYER issue is titled with the buyer document from its fingerprint."""
    title, body = render_issue(
        IssueFields(
            kind="UNROUTED_BUYER",
            fingerprint="unrouted:20500000002",
            ruc_emisor=None,
            razon_social=None,
            tipo="01",
            reasons=(),
            portal_key=None,
            hint_url=None,
        )
    )

    assert title == "gap: UNROUTED_BUYER RUC 20500000002"
    assert body == ("**RUC comprador sin flujo**\n\n- Tipo: 01\n\n<!-- gap:unrouted:20500000002 -->")


def test_unrouted_buyer_title_drops_a_document_that_is_not_a_ruc() -> None:
    """R-11.12: only an 11-digit buyer RUC reaches the title (D120); a DNI is dropped."""
    title, _ = render_issue(
        IssueFields(
            kind="UNROUTED_BUYER",
            fingerprint="unrouted:12345678",
            ruc_emisor=None,
            razon_social=None,
            tipo=None,
            reasons=(),
            portal_key=None,
            hint_url=None,
        )
    )

    assert title == "gap: UNROUTED_BUYER"


def test_hostile_name_loses_mentions_markdown_bidi_and_newlines() -> None:
    """R-11.12: a name keeps only letters, digits, spaces and .,&-."""
    _, body = render_issue(
        IssueFields(
            kind="PORTAL_MISSING",
            fingerprint="portal_missing:20100000001",
            ruc_emisor=20100000001,
            razon_social="@owner [x](https://evil.example) ‮Bodega\nSAC",
            tipo="01",
            reasons=(FetchReason.NO_PORTAL,),
            portal_key="@owner",
            hint_url=None,
        )
    )

    for hostile in ("@", "[x]", "](", "‮", "https://evil.example", "Bodega\nSAC"):
        assert hostile not in body
    assert "- Razón social: owner xhttpsevil.example BodegaSAC" in body


def test_javascript_url_is_dropped() -> None:
    """R-11.12: a hint URL that is not https is dropped, not rendered."""
    _, body = render_issue(_portal_missing(hint_url="javascript:alert(1)"))

    assert "javascript" not in body
    assert "```" not in body


def test_url_userinfo_query_and_fragment_are_dropped() -> None:
    """R-11.12: an approved URL is reduced to https://host/path inside a code fence."""
    _, body = render_issue(_portal_missing(hint_url="https://user:pass@portal.example.pe/x?token=1#top"))

    assert "user:pass" not in body
    assert "token=1" not in body
    assert "#top" not in body
    assert "```\nhttps://portal.example.pe/x\n```" in body


def test_bad_ruc_is_dropped() -> None:
    """R-11.12: a RUC that is not exactly 11 digits is dropped, not rendered."""
    title, body = render_issue(_portal_missing(ruc_emisor=123456789012))

    assert title == "gap: PORTAL_MISSING"
    assert "RUC" not in body
    assert "123456789012" not in body


def test_marker_is_the_last_line() -> None:
    """R-11.12: the body ends with the hidden fingerprint marker D117 searches for."""
    _, body = render_issue(_portal_missing(hint_url="https://portal.example.pe/consulta"))

    assert body.splitlines()[-1] == "<!-- gap:portal_missing:20100000001 -->"
