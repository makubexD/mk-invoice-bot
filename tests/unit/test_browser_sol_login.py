"""Tests for typing the SOL credentials only on SUNAT (R-9.10, R-9.16)."""

from __future__ import annotations

from invoicebot.config.models import SolLogin
from invoicebot.fetchers.browser.sol_login import SolCredentials, log_in

SUNAT_URL = "https://api-seguridad.sunat.gob.pe/login"
LOOKALIKE_URL = "https://api-seguridad.sunat.gob.pe.evil.test/login"
AWAY_URL = "https://evil.test/login"

RUC_FIELD = "#ruc"
USER_FIELD = "#user"
HIDDEN_FIELD = "#password"
RUC_TEXT = "20000000001"
USER_TEXT = "USUARIO1"
HIDDEN_TEXT = "synthetic-pass"

LOGIN = SolLogin(
    url=SUNAT_URL,
    ruc=RUC_FIELD,
    user=USER_FIELD,
    password=HIDDEN_FIELD,
    submit="#submit",
)
CREDS = SolCredentials(ruc=RUC_TEXT, user=USER_TEXT, password=HIDDEN_TEXT)


class FakePage:
    """A page that records every call and moves to ``redirect_to`` after N fills."""

    def __init__(self, url: str, redirect_to: str | None = None, redirect_after: int = 1) -> None:
        self._url = url
        self._redirect_to = redirect_to
        self._redirect_after = redirect_after
        self._fills = 0
        self.calls: list[tuple[str, ...]] = []

    @property
    def url(self) -> str:
        return self._url

    async def fill(self, selector: str, value: str) -> None:
        self.calls.append(("fill", selector, value))
        self._fills += 1
        if self._redirect_to is not None and self._fills == self._redirect_after:
            self._url = self._redirect_to

    async def click(self, selector: str) -> None:
        self.calls.append(("click", selector))

    async def wait_for_load_state(self) -> None:
        self.calls.append(("wait",))


async def test_a_sunat_page_gets_all_three_fields_and_the_submit_click() -> None:
    """R-9.10: on a SUNAT origin the three fields are filled and submit clicked."""
    page = FakePage(SUNAT_URL)

    assert await log_in(page, LOGIN, CREDS)

    assert page.calls == [
        ("fill", LOGIN.ruc, CREDS.ruc),
        ("fill", LOGIN.user, CREDS.user),
        ("fill", LOGIN.password, CREDS.password),
        ("click", LOGIN.submit),
        ("wait",),
    ]


async def test_a_look_alike_page_gets_no_fill() -> None:
    """R-9.10: a look-alike host receives nothing at all."""
    page = FakePage(LOOKALIKE_URL)

    assert not await log_in(page, LOGIN, CREDS)

    assert page.calls == []


async def test_a_redirect_after_the_first_fill_stops_the_form() -> None:
    """R-9.10: a page that leaves SUNAT after the first fill gets no second fill."""
    page = FakePage(SUNAT_URL, redirect_to=AWAY_URL, redirect_after=1)

    assert not await log_in(page, LOGIN, CREDS)

    assert page.calls == [("fill", LOGIN.ruc, CREDS.ruc)]


async def test_a_redirect_after_the_last_fill_skips_the_submit_click() -> None:
    """R-9.10: a page that leaves SUNAT after the last fill is not submitted."""
    page = FakePage(SUNAT_URL, redirect_to=AWAY_URL, redirect_after=3)

    assert not await log_in(page, LOGIN, CREDS)

    assert page.calls == [
        ("fill", LOGIN.ruc, CREDS.ruc),
        ("fill", LOGIN.user, CREDS.user),
        ("fill", LOGIN.password, CREDS.password),
    ]


def test_the_credentials_repr_and_str_hold_none_of_the_values() -> None:
    """R-9.16: neither repr nor str shows a credential value."""
    text = f"{CREDS!r} {CREDS!s}"

    assert text == "SolCredentials(<hidden>) SolCredentials(<hidden>)"
    for value in (CREDS.ruc, CREDS.user, CREDS.password):
        assert value not in text
