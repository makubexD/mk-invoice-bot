"""Integration tests for the helper's captcha ask mode (D209)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.config.models import Portal
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.captcha_ask import ask_captcha
from invoicebot.fetchers.browser.protocol import CaptchaChallenge

_PAGE = """<!doctype html>
<html><body>
<form id="form" action="/consulta" method="post" onsubmit="return false">
  <input type="hidden" name="token" value="abc" />
  <input type="hidden" name="viewstate" value="xyz" />
  <input id="total" name="total" />
  <input id="file" type="file" name="upload" />
  <input id="code" name="captcha" />
  __IMAGE__
  <button id="submit" type="submit" name="accion" value="Buscar">Buscar</button>
</form>
</body></html>
"""
_SMALL_IMAGE = '<div id="img" style="width: 80px; height: 30px; background: #369"></div>'
_NOISY_IMAGE = """<canvas id="img" width="400" height="400"></canvas>
<script>
const context = document.getElementById("img").getContext("2d");
const pixels = context.createImageData(400, 400);
for (let i = 0; i < pixels.data.length; i++) pixels.data[i] = Math.floor(Math.random() * 256);
context.putImageData(pixels, 0, 0);
</script>"""


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A headless Chromium for this module's tests."""
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch()
        yield launched
        await launched.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """A fresh page with a short timeout."""
    fresh = await browser.new_page()
    fresh.set_default_timeout(1500)
    yield fresh
    await fresh.close()


def _files_in(directory: Path) -> list[Path]:
    """The files left in a directory (run off the event loop)."""
    return list(directory.iterdir())


def _invoice() -> FetchInvoice:
    """The synthetic invoice every test fills in."""
    return FetchInvoice(
        ruc_emisor="20000000001",
        tipo="01",
        serie="F001",
        numero="123",
        fecha=date(2026, 1, 15),
        total=Decimal("118.5"),
    )


def _portal(image: str = "#img") -> Portal:
    """A portal whose captcha the sender solves."""
    return Portal.model_validate(
        {
            "key": "test-portal",
            "adapter": "generic_form",
            "status": "active",
            "url": "https://portal.test/form",
            "fields": {"#total": "{total:.2f}"},
            "captcha": {"image": image, "input": "#code", "solver": ["user"]},
            "submit": "#submit",
        }
    )


async def _serve(page: Page, image: str) -> None:
    """Serve the synthetic captcha form with the given image markup."""

    async def handle(route: Route) -> None:
        if urlparse(route.request.url).path == "/form":
            body = _PAGE.replace("__IMAGE__", image).encode()
            await route.fulfill(status=200, body=body, content_type="text/html")
        else:
            await route.fulfill(status=404, body=b"not found")

    await page.route("https://portal.test/**", handle)


@pytest.mark.asyncio(loop_scope="session")
async def test_ask_saves_the_captcha_and_the_form_without_its_input(page: Page, tmp_path: Path) -> None:
    """D209: a PNG and the form's text fields in order, without the captcha input or files."""
    await _serve(page, _SMALL_IMAGE)

    asked = await ask_captcha(page, _portal(), _invoice(), tmp_path)

    assert isinstance(asked, CaptchaChallenge)
    assert asked.path.read_bytes().startswith(b"\x89PNG")
    assert asked.size == asked.path.stat().st_size
    assert asked.form.fields == (("token", "abc"), ("viewstate", "xyz"), ("total", "118.50"))
    assert asked.form.submit_name == "accion"
    assert asked.form.submit_value == "Buscar"
    assert asked.form.page_url == "https://portal.test/form"
    assert asked.form.action_url == "https://portal.test/consulta"


@pytest.mark.asyncio(loop_scope="session")
async def test_ask_with_a_missing_captcha_image_times_out(page: Page, tmp_path: Path) -> None:
    """D209: a captcha selector that matches nothing is a timeout."""
    await _serve(page, _SMALL_IMAGE)

    asked = await ask_captcha(page, _portal(image="#nope"), _invoice(), tmp_path)

    assert isinstance(asked, FetchResult)
    assert asked.reason is FetchReason.TIMEOUT
    assert _files_in(tmp_path) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_ask_with_an_oversized_captcha_image_is_technical(page: Page, tmp_path: Path) -> None:
    """D209: a captcha image over the cap is technical and leaves no file."""
    await _serve(page, _NOISY_IMAGE)

    asked = await ask_captcha(page, _portal(), _invoice(), tmp_path)

    assert isinstance(asked, FetchResult)
    assert asked.reason is FetchReason.TECHNICAL
    assert _files_in(tmp_path) == []
