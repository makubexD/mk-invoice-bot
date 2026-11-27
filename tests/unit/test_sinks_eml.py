"""Tests for the outgoing mail builder and its ``.eml`` (R-7.6, R-7.7, D69)."""

from __future__ import annotations

import email
import email.policy
from dataclasses import replace
from email.message import Message

import pytest

from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.eml import build_eml
from invoicebot.sinks.mail_body import html_body

PDF_BYTES = b"%PDF-1.4 synthetic"
XLSX_BYTES = b"PK\x03\x04 synthetic"
XML_BYTES = b"<Invoice/>"

MAIL = OutgoingMail(
    to=("owner@example.com",),
    cc=(),
    subject="Support release 05/11/2026",
    body="Here is the invoice.",
    attachments=(
        Attachment("invoice.pdf", PDF_BYTES, "application/pdf"),
        Attachment(
            "invoice.xlsx",
            XLSX_BYTES,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        Attachment("invoice.xml", XML_BYTES, "application/xml"),
    ),
)


def _parse(data: bytes) -> Message:
    return email.message_from_bytes(data, policy=email.policy.default)


def test_subject_and_date_are_written() -> None:
    """D69: the subject reaches the message exactly as given, with a Date header."""
    message = _parse(build_eml(MAIL))

    assert message["Subject"] == "Support release 05/11/2026"
    assert message["Date"] is not None
    assert message["From"] is None


def test_three_attachments_round_trip_names_and_bytes() -> None:
    """R-7.6: every attachment keeps its file name and its bytes after parsing."""
    message = _parse(build_eml(MAIL))
    attachments = list(message.iter_attachments())

    assert [part.get_filename() for part in attachments] == [
        "invoice.pdf",
        "invoice.xlsx",
        "invoice.xml",
    ]
    assert [part.get_content() for part in attachments] == [
        PDF_BYTES,
        XLSX_BYTES,
        XML_BYTES,
    ]


def test_non_ascii_attachment_name_round_trips() -> None:
    """R-7.6: a file name with ``ñ`` survives the policy's header encoding."""
    attachment = Attachment("factura ñ.pdf", PDF_BYTES, "application/pdf")
    message = _parse(build_eml(replace(MAIL, attachments=(attachment,))))
    (part,) = message.iter_attachments()

    assert part.get_filename() == "factura ñ.pdf"
    assert part.get_content() == PDF_BYTES


def test_empty_cc_adds_no_cc_header() -> None:
    """R-7.6: a mail without Cc recipients carries no Cc header."""
    message = _parse(build_eml(replace(MAIL, cc=())))

    assert message["Cc"] is None


def test_cc_recipients_are_written() -> None:
    """R-7.6: a non-empty cc list becomes a Cc header."""
    message = _parse(build_eml(replace(MAIL, cc=("copy@example.com",))))

    assert message["Cc"] == "copy@example.com"


@pytest.mark.parametrize("subject", ["line\rbreak", "line\nbreak"])
def test_newline_in_subject_raises(subject: str) -> None:
    """R-7.7: a CR or LF in the subject could inject a header, so it is refused."""
    with pytest.raises(ValueError, match="subject"):
        build_eml(replace(MAIL, subject=subject))


def test_newline_in_recipient_raises() -> None:
    """R-7.7: a CR or LF in a To or Cc address is refused without echoing it."""
    injected = "owner@example.com\r\nBcc: evil@example.com"

    with pytest.raises(ValueError, match="recipient address") as excinfo:
        build_eml(replace(MAIL, to=(injected,)))
    assert "evil@example.com" not in str(excinfo.value)

    with pytest.raises(ValueError, match="recipient address"):
        build_eml(replace(MAIL, cc=("copy@example.com\n",)))


def test_newline_in_attachment_name_raises() -> None:
    """R-7.7: a CR or LF in an attachment name is refused."""
    attachment = Attachment("factura.pdf\r\nX-Evil: 1", PDF_BYTES, "application/pdf")

    with pytest.raises(ValueError, match="attachment name"):
        build_eml(replace(MAIL, attachments=(attachment,)))


@pytest.mark.parametrize("mime", ["application", "application/pdf/extra", "", "/pdf"])
def test_mime_without_exactly_one_slash_raises(mime: str) -> None:
    """R-7.6: an attachment needs a MIME type of exactly ``type/subtype``."""
    attachment = Attachment("invoice.pdf", PDF_BYTES, mime)

    with pytest.raises(ValueError, match="MIME"):
        build_eml(replace(MAIL, attachments=(attachment,)))


def test_sender_becomes_from_header() -> None:
    """D205: with a sender the message carries that From address."""
    message = _parse(build_eml(MAIL, sender="bot@example.com"))

    assert message["From"] == "bot@example.com"


def test_newline_in_sender_raises() -> None:
    """D205: a CR or LF in the sender is refused like any other header."""
    with pytest.raises(ValueError, match="sender"):
        build_eml(MAIL, sender="bot@example.com\r\nBcc: evil@example.com")


def test_html_adds_alternative_and_keeps_attachments() -> None:
    """D205: html=True gives mixed(alternative(plain, html), attachments)."""
    message = _parse(build_eml(MAIL, html=True))
    body = message.get_body(preferencelist=("html",))

    assert message.get_content_type() == "multipart/mixed"
    alternative = next(message.iter_parts())
    assert alternative.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in alternative.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert body is not None
    assert body.get_content().strip() == html_body(MAIL.body)
    assert [part.get_filename() for part in message.iter_attachments()] == [
        "invoice.pdf",
        "invoice.xlsx",
        "invoice.xml",
    ]


def test_defaults_have_no_from_and_no_html_part() -> None:
    """D205: without the new arguments the dry-run shape is unchanged."""
    message = _parse(build_eml(MAIL))

    assert message["From"] is None
    assert message.get_body(preferencelist=("html",)) is None


def test_message_id_is_written() -> None:
    """D207: a given message id becomes the Message-ID header."""
    message = _parse(build_eml(MAIL, message_id="<abc@example.com>"))

    assert message["Message-ID"] == "<abc@example.com>"


def test_no_message_id_adds_no_header() -> None:
    """D207: without a message id there is no Message-ID header."""
    assert _parse(build_eml(MAIL))["Message-ID"] is None


def test_newline_in_message_id_raises() -> None:
    """D207: a CR or LF in the message id is refused like any other header."""
    with pytest.raises(ValueError, match="message id"):
        build_eml(MAIL, message_id="<abc@example.com>\r\nBcc: evil@example.com")
