"""Tests for the email inbox fetcher (R-5.2, R-5.6, S11.9)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchInvoice, FetchReason
from invoicebot.fetchers.email_inbox import SEARCH_TOP, EmailInboxFetcher
from invoicebot.fetchers.graph_mail import MailAttachment, MailMessage
from invoicebot.sinks.onedrive import GraphError, GraphTransient

# From tests/synthetic_rucs.txt.
SYNTHETIC_RUC = "20000000001"
SERIE = "F001"
NUMERO = "123"
OTHER_NUMERO = "999"
TOTAL = Decimal("118.50")
FECHA = date(2026, 11, 13)
PDF = b"%PDF-1.4 synthetic"
MEDIA_MAX_BYTES = MediaMaxBytes(
    image=10 * 1024 * 1024,
    pdf=1024,
    xml=2048,
    zip=20 * 1024 * 1024,
)

XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>F001-{numero}</cbc:ID>
  <cbc:IssueDate>2026-11-13</cbc:IssueDate>
  <cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="6">20000000001</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>COMERCIO DE PRUEBA S.A.C.</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">100.42</cbc:LineExtensionAmount>
    <cbc:PayableAmount currencyID="PEN">118.50</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</Invoice>
"""
DOCTYPE = '<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'


class FakeMailbox:
    """An in-memory mailbox that records its searches and downloads."""

    def __init__(
        self,
        messages: list[MailMessage],
        attachments: dict[str, list[MailAttachment]],
        files: dict[str, bytes],
        error: Exception | None = None,
    ) -> None:
        self._messages = messages
        self._attachments = attachments
        self._files = files
        self._error = error
        self.searches: list[tuple[str, int]] = []
        self.downloaded: list[tuple[str, str]] = []

    async def search(self, term: str, top: int) -> list[MailMessage]:
        self.searches.append((term, top))
        if self._error is not None:
            raise self._error
        return self._messages

    async def attachments(self, message_id: str) -> list[MailAttachment]:
        return self._attachments.get(message_id, [])

    async def download(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        self.downloaded.append((message_id, attachment_id))
        return self._files[attachment_id]


def _invoice(numero: str = NUMERO) -> FetchInvoice:
    """Return the invoice the mailbox is searched for, with synthetic values."""
    return FetchInvoice(
        ruc_emisor=SYNTHETIC_RUC,
        tipo="01",
        serie=SERIE,
        numero=numero,
        fecha=FECHA,
        total=TOTAL,
    )


def _xml(numero: str = NUMERO, *, doctype: bool = False) -> bytes:
    """Return a UBL invoice of the configured shape, optionally with a DOCTYPE line."""
    document = XML_TEMPLATE.format(numero=numero)
    if doctype:
        document = document.replace("\n", f"\n{DOCTYPE}\n", 1)
    return document.encode("utf-8")


def _message(message_id: str, received_at: datetime) -> MailMessage:
    return MailMessage(id=message_id, received_at=received_at)


def _attachment(attachment_id: str, name: str, size: int) -> MailAttachment:
    return MailAttachment(id=attachment_id, name=name, size=size)


async def test_fetch_xml_and_pdf_are_written_with_their_digests(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a matching XML and the PDF beside it come back as two files."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    xml_attachment = _attachment("A1", "F001-123.xml", len(xml))
    pdf_attachment = _attachment("A2", "F001-123.pdf", len(PDF))
    mailbox = FakeMailbox(
        [message],
        {"M1": [xml_attachment, pdf_attachment]},
        {"A1": xml, "A2": PDF},
    )

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert result.portal_key is None
    files = {file.kind: file for file in result.files}
    assert set(files) == {"xml", "pdf"}
    assert files["xml"].path == tmp_path / "xml.xml"
    assert files["xml"].sha256 == hashlib.sha256(xml).hexdigest()
    assert files["pdf"].path == tmp_path / "pdf.pdf"
    assert files["pdf"].sha256 == hashlib.sha256(PDF).hexdigest()
    assert mailbox.searches == [(f"{SERIE}-{NUMERO}", SEARCH_TOP)]


async def test_fetch_searches_the_mailbox_for_the_serie_and_numero(tmp_path: Path) -> None:
    """R-5.5: the mailbox search names the invoice as serie-numero."""
    mailbox = FakeMailbox([], {}, {})

    await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert mailbox.searches == [("F001-123", SEARCH_TOP)]


async def test_fetch_matches_a_zero_padded_name(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: F001-00000123 matches numero 123."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-00000123.xml", len(xml))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {"A1": xml})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert mailbox.downloaded == [("M1", "A1")]


async def test_fetch_does_not_match_a_longer_numero(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: F001-1234 does not match numero 123."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-1234.xml", len(xml))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {"A1": xml})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.NOT_FOUND_YET
    assert mailbox.downloaded == []


async def test_fetch_skips_a_message_two_days_before_the_invoice(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a message received before the invoice date minus a day is skipped."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 11, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-123.xml", len(xml))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {"A1": xml})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.NOT_FOUND_YET
    assert mailbox.downloaded == []


async def test_fetch_with_no_matching_mail_is_not_found_yet(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a mailbox without the invoice's mail is retried later."""
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-999.xml", len(_xml(OTHER_NUMERO)))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.NOT_FOUND_YET


async def test_fetch_with_a_mismatching_xml_is_xml_mismatch(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: an XML for another invoice is reported as a mismatch."""
    xml = _xml(OTHER_NUMERO)
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-123.xml", len(xml))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {"A1": xml})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.XML_MISMATCH


async def test_fetch_skips_an_over_cap_xml_without_downloading_it(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: an XML listed past its cap is skipped before any download."""
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-123.xml", MEDIA_MAX_BYTES.xml + 1)
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.NOT_FOUND_YET
    assert mailbox.downloaded == []


async def test_fetch_with_a_sign_in_needed_is_auth_required(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a mailbox that needs a sign-in reports auth_required."""
    mailbox = FakeMailbox([], {}, {}, error=AuthRequired("bot"))

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.AUTH_REQUIRED


async def test_fetch_with_a_transient_graph_error_is_technical(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a transient Graph failure is a technical failure."""
    mailbox = FakeMailbox([], {}, {}, error=GraphTransient(503))

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TECHNICAL


async def test_fetch_with_a_graph_error_is_technical(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a Graph error is a technical failure."""
    mailbox = FakeMailbox([], {}, {}, error=GraphError(403))

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.TECHNICAL


async def test_fetch_with_only_the_xml_is_ok(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a matching XML with no PDF is OK with one file."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    attachment = _attachment("A1", "F001-123.xml", len(xml))
    mailbox = FakeMailbox([message], {"M1": [attachment]}, {"A1": xml})

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    (file,) = result.files
    assert file.kind == "xml"


async def test_fetch_drops_a_pdf_attachment_that_is_not_a_pdf(tmp_path: Path) -> None:
    """R-5.6: an HTML body named like a PDF is dropped, leaving only the matching XML."""
    xml = _xml()
    html = b"<!DOCTYPE html><html><body>not a pdf</body></html>"
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    xml_attachment = _attachment("A1", "F001-123.xml", len(xml))
    pdf_attachment = _attachment("A2", "F001-123.pdf", len(html))
    mailbox = FakeMailbox(
        [message],
        {"M1": [xml_attachment, pdf_attachment]},
        {"A1": xml, "A2": html},
    )

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    (file,) = result.files
    assert file.kind == "xml"


async def test_fetch_skips_a_rejected_xml_and_tries_the_next_message(tmp_path: Path) -> None:
    """R-5.2, R-5.6, S11.9: a rejected XML is skipped and a later matching one is used."""
    rejected = _xml(doctype=True)
    matching = _xml()
    newest = _message("M2", datetime(2026, 11, 14, 10, 0, tzinfo=UTC))
    older = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    first = _attachment("A2", "F001-123.xml", len(rejected))
    second = _attachment("A1", "F001-123.xml", len(matching))
    mailbox = FakeMailbox(
        [older, newest],
        {"M1": [second], "M2": [first]},
        {"A1": matching, "A2": rejected},
    )

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    (file,) = result.files
    assert file.path.read_bytes() == matching


@pytest.mark.parametrize("name", ["F001-123.PDF", "f001-123.pdf"])
async def test_pdf_extension_is_case_insensitive(tmp_path: Path, name: str) -> None:
    """R-5.2, R-5.6, S11.9: the PDF extension is matched case-insensitively."""
    xml = _xml()
    message = _message("M1", datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    xml_attachment = _attachment("A1", "F001-123.xml", len(xml))
    pdf_attachment = _attachment("A2", name, len(PDF))
    mailbox = FakeMailbox(
        [message],
        {"M1": [xml_attachment, pdf_attachment]},
        {"A1": xml, "A2": PDF},
    )

    result = await EmailInboxFetcher(mailbox, MEDIA_MAX_BYTES).fetch(_invoice(), tmp_path)

    assert result.reason is FetchReason.OK
    assert {file.kind for file in result.files} == {"xml", "pdf"}
