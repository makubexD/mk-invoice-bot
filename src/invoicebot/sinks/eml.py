"""Build the ``.eml`` bytes of an outgoing mail (R-7.6, R-7.7, D69).

The message is rendered with :class:`email.message.EmailMessage` and the SMTP
policy, so a non-ASCII file name is encoded as the RFC requires. The sender is
the Graph account, so no ``From`` header is written.
"""

from __future__ import annotations

import email.policy
from email.message import EmailMessage
from email.utils import formatdate

from invoicebot.sinks.base import OutgoingMail

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


def build_eml(mail: OutgoingMail) -> bytes:
    """Render ``mail`` as the bytes of an ``.eml`` message (R-7.6, D69).

    Args:
        mail: the recipients, subject, body and attachments to render.

    Returns:
        The message as SMTP-policy bytes, without a ``From`` header.

    Raises:
        ValueError: the subject, a recipient address or an attachment name
            holds a CR or LF, or an attachment MIME type is not
            ``type/subtype``.
    """
    _refuse_header_newlines(mail.subject, "subject")
    for address in (*mail.to, *mail.cc):
        _refuse_header_newlines(address, "recipient address")
    for attachment in mail.attachments:
        _refuse_header_newlines(attachment.name, "attachment name")

    message = EmailMessage(policy=email.policy.SMTP)
    message["To"] = ", ".join(mail.to)
    if mail.cc:
        message["Cc"] = ", ".join(mail.cc)
    message["Subject"] = mail.subject
    message["Date"] = formatdate(usegmt=True)
    message.set_content(mail.body)
    for attachment in mail.attachments:
        maintype, subtype = _split_mime(attachment.mime)
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    return message.as_bytes()
