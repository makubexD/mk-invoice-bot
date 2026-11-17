"""Sign in to SUNAT SOL, typing the credentials only on a SUNAT origin (R-9.10, D134).

The form is filled value by value, and the origin is checked again immediately
before each fill and before the submit click, so a redirect never receives a
credential. Nothing here logs a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from invoicebot.config.models import SolLogin
from invoicebot.sunat_origins import is_sunat_origin


class LoginPage(Protocol):
    """The part of a browser page the SOL sign-in uses (R-9.10)."""

    @property
    def url(self) -> str: ...

    async def fill(self, selector: str, value: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def wait_for_load_state(self) -> None: ...


@dataclass(frozen=True)
class SolCredentials:
    """The SOL sign-in values, whose text form never shows them (R-9.16)."""

    ruc: str = field(repr=False)
    user: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return "SolCredentials(<hidden>)"


async def log_in(page: LoginPage, login: SolLogin, creds: SolCredentials) -> bool:
    """Fill the SOL form and submit it, only while the page stays on SUNAT (R-9.10).

    Returns ``False`` without typing anything, or part way through, as soon as
    ``page.url`` leaves the hard-coded SUNAT origins.
    """
    if not is_sunat_origin(page.url):
        return False
    fields = (
        (login.ruc, creds.ruc),
        (login.user, creds.user),
        (login.password, creds.password),
    )
    for selector, value in fields:
        if not is_sunat_origin(page.url):
            return False
        await page.fill(selector, value)
    if not is_sunat_origin(page.url):
        return False
    await page.click(login.submit)
    await page.wait_for_load_state()
    return True
