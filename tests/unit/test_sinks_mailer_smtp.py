"""Tests for the SMTP mail client (D205)."""

from __future__ import annotations

import base64
import dataclasses
import email
import email.policy
import logging
import smtplib
from collections.abc import Sequence
from typing import Any

import pytest

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.auth.providers import SMTP_SEND_SCOPE
from invoicebot.sinks.base import OutgoingMail
from invoicebot.sinks.mailer_smtp import SmtpEndpoint, SmtpError, SmtpMailer

ACCOUNT = "bot"
SENDER = "bot@example.com"
BEARER = "synthetic-xoauth-token"
MAIL = OutgoingMail(
    to=("owner@example.com",),
    cc=("accounting@example.com",),
    subject="Support release",
    body="Here is the invoice.",
    attachments=(),
)


class FakeTokens:
    def __init__(self) -> None:
        self.scopes: list[Sequence[str]] = []

    async def token_for(self, scopes: Sequence[str]) -> str:
        self.scopes.append(scopes)
        return BEARER


class FakeSmtp:
    """Records the calls; answers AUTH with ``auth_code``."""

    auth_code = 235
    refuse: dict[str, tuple[int, bytes]] | None = None
    connect_error: OSError | None = None
    quit_error: OSError | None = None
    partial: dict[str, tuple[int, bytes]] | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.calls: list[tuple[str, Any]] = [("connect", (host, port, timeout))]
        FakeSmtp.last = self

    last: FakeSmtp

    def quit(self) -> None:
        self.calls.append(("quit", None))
        if self.quit_error is not None:
            raise self.quit_error

    def close(self) -> None:
        self.calls.append(("close", None))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", None))

    def starttls(self, *, context: object) -> None:
        self.calls.append(("starttls", context))

    def docmd(self, cmd: str, args: str) -> tuple[int, bytes]:
        self.calls.append(("docmd", (cmd, args)))
        return self.auth_code, b"reply"

    def sendmail(self, sender: str, recipients: list[str], data: bytes) -> dict[str, tuple[int, bytes]]:
        self.calls.append(("sendmail", (sender, recipients, data)))
        if self.refuse is not None:
            raise smtplib.SMTPRecipientsRefused(self.refuse)
        return self.partial or {}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _fake(**attrs: object) -> type[FakeSmtp]:
    return type("Fake", (FakeSmtp,), attrs)


def _mailer(fake: type[FakeSmtp], tokens: FakeTokens | None = None) -> SmtpMailer:
    return SmtpMailer(
        tokens or FakeTokens(),
        SmtpEndpoint("smtp.example.com", 587, 5.0),
        sender=SENDER,
        account=ACCOUNT,
        connect=fake,  # type: ignore[arg-type]  # the fake mirrors the smtplib.SMTP calls it uses
    )


async def test_send_runs_starttls_before_auth_then_sends_to_all() -> None:
    """D205: STARTTLS precedes AUTH XOAUTH2 for the sender and token; to and cc all get the bytes."""
    fake = _fake()
    tokens = FakeTokens()
    await _mailer(fake, tokens).send(MAIL)
    smtp = fake.last
    assert tokens.scopes == [(SMTP_SEND_SCOPE,)]
    assert smtp.names() == ["connect", "ehlo", "starttls", "ehlo", "docmd", "sendmail", "quit"]
    auth = dict(smtp.calls)["docmd"]
    assert auth[0] == "AUTH"
    method, _, blob = auth[1].partition(" ")
    assert method == "XOAUTH2"
    assert base64.b64decode(blob) == f"user={SENDER}\x01auth=Bearer {BEARER}\x01\x01".encode()
    sender, recipients, _ = dict(smtp.calls)["sendmail"]
    assert sender == SENDER
    assert recipients == ["owner@example.com", "accounting@example.com"]


async def test_sent_bytes_have_sender_and_html_part() -> None:
    """D205: the message is From the sender and carries a text/html part."""
    fake = _fake()
    await _mailer(fake).send(MAIL)
    data = dict(fake.last.calls)["sendmail"][2]
    message = email.message_from_bytes(data, policy=email.policy.SMTP)
    assert SENDER in message["From"]
    assert any(part.get_content_type() == "text/html" for part in message.walk())


async def test_send_returns_the_message_id_written_once_under_the_sender_domain() -> None:
    """D207: the returned id is the one Message-ID header of the sent bytes, on the sender's domain."""
    fake = _fake()
    message_id = await _mailer(fake).send(MAIL)
    data = dict(fake.last.calls)["sendmail"][2]
    message = email.message_from_bytes(data, policy=email.policy.SMTP)
    assert message.get_all("Message-ID") == [message_id]
    assert message_id.endswith("@example.com>")


async def test_two_sends_get_different_message_ids() -> None:
    """D207: every send makes a fresh Message-ID."""
    mailer = _mailer(_fake())
    assert await mailer.send(MAIL) != await mailer.send(MAIL)


async def test_subject_with_newline_is_still_refused_and_sends_nothing() -> None:
    """D207: adding the Message-ID keeps the header-injection guard on the subject."""
    no_connection = _fake(connect_error=OSError("must not connect"))
    bad = dataclasses.replace(MAIL, subject="Support\r\nBcc: evil@example.com")
    with pytest.raises(ValueError, match="subject"):
        await _mailer(no_connection).send(bad)


async def test_auth_535_raises_auth_required_and_sends_nothing() -> None:
    """D205: a 535 answer to AUTH asks for a new sign-in and sends no mail."""
    fake = _fake(auth_code=535)
    with pytest.raises(AuthRequired):
        await _mailer(fake).send(MAIL)
    assert "sendmail" not in fake.last.names()
    assert "quit" in fake.last.names()


async def test_auth_other_code_raises_smtp_error() -> None:
    """D205: any AUTH answer but 235 and 535 is an SmtpError with that code."""
    fake = _fake(auth_code=454)
    with pytest.raises(SmtpError) as info:
        await _mailer(fake).send(MAIL)
    assert info.value.code == 454
    assert "sendmail" not in fake.last.names()


async def test_refused_recipient_raises_smtp_error_without_address() -> None:
    """D205: a refused recipient maps to its code and the message holds no address."""
    fake = _fake(refuse={"owner@example.com": (550, b"no such user")})
    with pytest.raises(SmtpError) as info:
        await _mailer(fake).send(MAIL)
    assert info.value.code == 550
    assert "example.com" not in str(info.value)
    assert "no such user" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


async def test_connection_error_raises_smtp_error_zero() -> None:
    """D205: a connection OSError is SmtpError(0)."""
    fake = _fake(connect_error=ConnectionRefusedError("refused"))
    with pytest.raises(SmtpError) as info:
        await _mailer(fake).send(MAIL)
    assert info.value.code == 0
    assert isinstance(info.value.__cause__, ConnectionRefusedError)


async def test_disconnect_raises_smtp_error_zero() -> None:
    """D205: a dropped connection (SMTPServerDisconnected, an OSError) is SmtpError(0), not an OSError."""
    fake = _fake(connect_error=smtplib.SMTPServerDisconnected("gone"))
    with pytest.raises(SmtpError) as info:
        await _mailer(fake).send(MAIL)
    assert info.value.code == 0


async def test_token_is_in_no_exception_text() -> None:
    """D205: the token appears in no raised exception's text nor its cause chain."""
    for fake in (_fake(auth_code=454), _fake(auth_code=535), _fake(refuse={"a@example.com": (550, b"x")})):
        with pytest.raises((SmtpError, AuthRequired)) as info:
            await _mailer(fake).send(MAIL)
        exc: BaseException | None = info.value
        while exc is not None:
            assert BEARER not in repr(exc)
            assert BEARER not in str(exc)
            exc = exc.__cause__


async def test_failed_quit_after_the_send_still_counts_as_sent() -> None:
    """D205: a QUIT answered with an error after the server took the mail does not fail the send."""
    fake = _fake(quit_error=smtplib.SMTPResponseException(421, b"timeout"))
    await _mailer(fake).send(MAIL)
    assert fake.last.names()[-3:] == ["sendmail", "quit", "close"]


async def test_failed_quit_after_auth_535_keeps_auth_required() -> None:
    """D205: a failed QUIT after a 535 still asks for a new sign-in."""
    fake = _fake(auth_code=535, quit_error=smtplib.SMTPServerDisconnected("gone"))
    with pytest.raises(AuthRequired):
        await _mailer(fake).send(MAIL)


async def test_partly_refused_recipients_are_logged_without_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D205: some recipients refused is not an error (the rest got it); the log holds codes, no address."""
    fake = _fake(partial={"accounting@example.com": (452, b"4.5.3 too many")})
    with caplog.at_level(logging.WARNING):
        await _mailer(fake).send(MAIL)
    [record] = [r for r in caplog.records if r.message == "smtp recipients refused"]
    assert record.__dict__["codes"] == [452]
    assert "example.com" not in repr(record.__dict__)
