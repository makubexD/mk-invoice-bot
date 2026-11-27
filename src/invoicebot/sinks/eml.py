"""Build the ``.eml`` bytes of an outgoing mail (R-7.6, R-7.7, D69, D205).

The message is rendered with :class:`email.message.EmailMessage` and the SMTP
policy, so a non-ASCII file name is encoded as the RFC requires. By default no
``From`` header is written (the Graph account is the sender) and the body is
plain text; the SMTP mailer asks for a ``From`` and the D197 HTML alternative.
"""

from __future__ import annotations

import email.policy
from email.message import EmailMessage
from email.utils import formatdate

from invoicebot.sinks.base import OutgoingMail
from invoicebot.sinks.mailer_graph import html_body

#: Characters that would let a value inject or split a header.
_HEADER_INJECTION_CHARS = ("\r", "\n")


def _refuse_header_newlines(value: str, what: str) -> None:
    """Raise ``ValueError`` when ``value`` holds a CR or LF; the message names no value."""
    if any(char in value for char in _HEADER_INJECTION_CHARS):
        raise ValueError(f"newline in {what}")


def _split_mime(mime: str) -> tuple[str, str]:
    """Split ``type/subtype`` into its two parts, refusing any other shape."""
    parts = mime.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("invalid attachment MIME type")
    return parts[0], parts[1]


def build_eml(mail: OutgoingMail, *, sender: str | None = None, html: bool = False) -> bytes:
    """Render ``mail`` as the bytes of an ``.eml`` message (R-7.6, D69, D205).

    Args:
        mail: the recipients, subject, body and attachments to render.
        sender: the ``From`` address; no ``From`` header when ``None``.
        html: add the D197 HTML body as an alternative to the plain text.

    Returns:
        The message as SMTP-policy bytes.

    Raises:
        ValueError: the subject, sender, a recipient address or an attachment
            name holds a CR or LF, or an attachment MIME type is not
            ``type/subtype``.
    """
    _refuse_header_newlines(mail.subject, "subject")
    if sender is not None:
        _refuse_header_newlines(sender, "sender address")
    for address in (*mail.to, *mail.cc):
        _refuse_header_newlines(address, "recipient address")
    for attachment in mail.attachments:
        _refuse_header_newlines(attachment.name, "attachment name")

    message = EmailMessage(policy=email.policy.SMTP)
    if sender is not None:
        message["From"] = sender
    message["To"] = ", ".join(mail.to)
    if mail.cc:
        message["Cc"] = ", ".join(mail.cc)
    message["Subject"] = mail.subject
    message["Date"] = formatdate(usegmt=True)
    message.set_content(mail.body)
    if html:
        message.add_alternative(html_body(mail.body), subtype="html")
    for attachment in mail.attachments:
        maintype, subtype = _split_mime(attachment.mime)
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    return message.as_bytes()
