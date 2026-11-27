"""Send mail through Outlook.com's SMTP submission with XOAUTH2 (D205).

Graph ``sendMail`` from a personal account bounces with 5.7.501, so flow B
connects to ``smtp-mail.outlook.com:587``, upgrades with STARTTLS (certificate
verified) and authenticates with the ``SMTP.Send`` token. The token never
reaches a log or an exception, and errors carry the SMTP reply code only, never
an address or the server's text (R-9.16).
"""

from __future__ import annotations

import asyncio
import base64
import smtplib
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.auth.providers import SMTP_SEND_SCOPE
from invoicebot.sinks.base import OutgoingMail
from invoicebot.sinks.eml import build_eml

#: The reply code of a successful AUTH.
_AUTH_OK = 235

#: The reply code of a rejected credential: the account needs a new sign-in.
_AUTH_REJECTED = 535


class SmtpError(Exception):
    """An SMTP failure; carries the reply code only, 0 for a connection failure (D205, R-9.16)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"smtp error {code}")
        self.code = code


@dataclass(frozen=True)
class SmtpEndpoint:
    """Where to connect: host, port and the connect and read timeout (D205)."""

    host: str
    port: int
    timeout_s: float


class ScopedTokenSource(Protocol):
    """The token provider's one method the SMTP mailer uses (D205)."""

    async def token_for(self, scopes: Sequence[str]) -> str:
        """Return an access token for ``scopes``."""
        ...


def _xoauth2(sender: str, token: str) -> str:
    """Return the base64 XOAUTH2 initial response for ``sender`` and ``token`` (D205)."""
    return base64.b64encode(f"user={sender}\x01auth=Bearer {token}\x01\x01".encode()).decode("ascii")


def _open(host: str, port: int, timeout_s: float) -> smtplib.SMTP:
    """Open a plain connection; ``smtplib.SMTP`` takes the timeout by keyword only."""
    return smtplib.SMTP(host, port, timeout=timeout_s)


class SmtpMailer:
    """One account's mailbox over SMTP submission and XOAUTH2 (D205).

    Long-lived: one instance per signed-in account. ``connect`` opens the
    connection and is injected so tests need no network.
    """

    def __init__(
        self,
        tokens: ScopedTokenSource,
        endpoint: SmtpEndpoint,
        *,
        sender: str,
        account: str,
        connect: Callable[[str, int, float], smtplib.SMTP] = _open,
    ) -> None:
        self._tokens = tokens
        self._endpoint = endpoint
        self._sender = sender
        self._account = account
        self._connect = connect

    async def send(self, mail: OutgoingMail) -> None:
        """Send ``mail`` and return once the server accepts it (D205).

        The token never reaches a log or an exception.

        Raises:
            AuthRequired: the server rejected the token (535); sign in again.
            SmtpError: any other failure, with its reply code (0: no connection).
        """
        token = await self._tokens.token_for((SMTP_SEND_SCOPE,))
        data = build_eml(mail, sender=self._sender, html=True)
        await asyncio.to_thread(self._deliver, token, data, [*mail.to, *mail.cc])

    def _deliver(self, token: str, data: bytes, recipients: list[str]) -> None:
        """Run one SMTP session: STARTTLS, XOAUTH2, send, quit (D205)."""
        try:
            with self._connect(self._endpoint.host, self._endpoint.port, self._endpoint.timeout_s) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                code, _ = smtp.docmd("AUTH", "XOAUTH2 " + _xoauth2(self._sender, token))
                if code == _AUTH_REJECTED:
                    raise AuthRequired(self._account)
                if code != _AUTH_OK:
                    raise SmtpError(code)
                smtp.sendmail(self._sender, recipients, data)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            raise SmtpError(codes[0] if codes else 0) from exc
        except smtplib.SMTPResponseException as exc:
            raise SmtpError(exc.smtp_code) from exc
        except OSError as exc:  # includes SMTPServerDisconnected and TLS errors
            raise SmtpError(0) from exc
